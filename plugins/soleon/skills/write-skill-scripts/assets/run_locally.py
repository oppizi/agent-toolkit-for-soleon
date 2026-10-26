#!/usr/bin/env python3
"""Run a Soleon skill script on your machine, against fake tools that keep the platform's rules.

No Soleon, no network, no spend. It checks the run form the agent will send, then runs the
script the way the sandbox does (agent-infra containers/shared/code_execution/runner.py):
`inputs`, `tools` and `execution` are globals, and the script is either plain top-level code
ending in `return`, or defines `async def main(inputs)`.

What the fakes enforce, because the platform does:
  - a tool missing from tool_scope is refused;
  - going past a tool's `max_calls` or `limits.max_tool_calls` ends the WHOLE run: the script
    can't catch it and its return value is lost (ledger.py "Tool call budget reached",
    runtime.py closes dispatch, service.py marks the run "blocked");
  - an ordinary tool failure raises an exception the script can catch;
  - the return value must be JSON and at most 64 KiB (runner.py "Result exceeds the 64 KiB limit").
On top, it flags a return value that carries rows (a list of 20+ dicts or lists): that is
data the model doesn't need.

Usage:
    python3 run_locally.py <script.py> <form.json> <fakes.py> [--inputs '<json>']

form.json is the run form copied from the skill's SKILL.md, with real values in place of the
<...> placeholders. fakes.py defines `async def handle(name, args)` returning what the real tool
would return (raise an exception to simulate a tool error), and may define
`async def save(name, data)` for execution.save.

As a library: `check_form(form)` returns a list of problems; `run(script_path, form, handle)`
returns a Result with .value, .calls, .saved, .aborted and .problems.
"""
import ast
import asyncio
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone

REQUEST_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z/[A-Za-z0-9_.:-]+$")
MAX_RESULT_BYTES = 64 * 1024
ROWS_THRESHOLD = 20


class PlatformAbort(BaseException):
    """The platform ended the run. BaseException, so a script's `except Exception` can't hide it."""


def check_form(form):
    """Problems with a run form, worded the way you would fix them. [] means it would be accepted."""
    problems = []
    rid = form.get("request_id")
    if not isinstance(rid, str) or not REQUEST_ID.match(rid) or len(rid) > 200:
        problems.append("request_id must be '<UTC time like 2026-10-16T14:05:00Z>/<short-name>'")
    else:
        made = datetime.strptime(rid.split("/", 1)[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if not (now - timedelta(days=30) < made <= now + timedelta(seconds=300)):
            problems.append("request_id time must be now (UTC); older than 30 days or in the future is refused")
    file = form.get("file")
    if not isinstance(file, str) or not file.endswith(".py") or "/" not in file:
        problems.append("file must be the script's path inside the skill, e.g. my-skill/scripts/run.py")
    if "code" in form:
        problems.append("run_code_file takes file, not code")
    limits = form.get("limits") or {}
    cap = limits.get("max_tool_calls")
    if not isinstance(cap, int) or not 0 <= cap <= 1000:
        problems.append("limits.max_tool_calls must be a whole number from 0 to 1000")
        cap = 1000
    scope = form.get("tool_scope")
    if not isinstance(scope, list):
        problems.append("tool_scope must be a list")
        scope = []
    for entry in scope:
        name = entry.get("tool") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            problems.append("every tool_scope entry needs a tool name")
            continue
        if not isinstance(entry.get("constraints"), dict):
            problems.append(f'tool_scope {name}: add "constraints": {{}} (required, even when empty)')
        calls = entry.get("max_calls")
        if not isinstance(calls, int) or not 1 <= calls <= 1000:
            problems.append(f"tool_scope {name}: max_calls must be a whole number from 1 to 1000")
        elif calls > cap:
            problems.append(f"tool_scope {name}: max_calls {calls} is above limits.max_tool_calls {cap}")
    out = form.get("output_scope")
    if not isinstance(out, dict):
        problems.append("output_scope is required (workspace_prefix and max_files)")
    else:
        for key in ("workspace_prefix", "max_files"):
            if key not in out:
                problems.append(f"output_scope.{key} is required")
        if out.get("deliver", True) is not False and out.get("max_files", 0):
            problems.append("output_scope.deliver is not false: every saved file will be attached to the chat")
    if not isinstance(form.get("inputs"), dict):
        problems.append("inputs must be an object")
    return problems


class Result:
    def __init__(self):
        self.value, self.calls, self.saved, self.aborted, self.problems = None, [], [], None, []


class _Tools:
    def __init__(self, form, handle, result):
        self.scope = {e["tool"]: e.get("max_calls", 0) for e in form.get("tool_scope") or [] if isinstance(e, dict)}
        self.cap = (form.get("limits") or {}).get("max_tool_calls", 0)
        self.handle, self.result, self.per_tool = handle, result, {}

    async def call(self, name, arguments=None):
        if name not in self.scope:
            raise RuntimeError(f"Tool {name!r} is not in this run's tool_scope")
        used = len(self.result.calls)
        if used >= self.cap:
            raise PlatformAbort(f"Tool call budget reached: limits.max_tool_calls={self.cap}")
        if self.per_tool.get(name, 0) >= self.scope[name]:
            raise PlatformAbort(f"Tool call budget reached: tool_scope {name} max_calls={self.scope[name]}")
        self.per_tool[name] = self.per_tool.get(name, 0) + 1
        self.result.calls.append((name, arguments or {}))
        return await self.handle(name, arguments or {})


class _Execution:
    def __init__(self, result, save):
        self.result, self._save = result, save

    async def save(self, name, data):
        self.result.saved.append(name)
        if self._save:
            return await self._save(name, data)
        return {"workspace_path": "outputs/" + name}

    async def checkpoint(self, value):
        return None


class _Returned(Exception):
    def __init__(self, value):
        self.value = value


class _TopLevelReturns(ast.NodeTransformer):
    """`return x` at top level becomes `raise _Returned(x)`, as the sandbox's runner does."""

    def visit_FunctionDef(self, node):
        return node

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Return(self, node):
        value = node.value or ast.Constant(value=None)
        call = ast.Call(func=ast.Name(id="_Returned", ctx=ast.Load()), args=[value], keywords=[])
        return ast.copy_location(ast.Raise(exc=call, cause=None), node)


def _looks_like_rows(value, depth=0):
    if depth > 4:
        return False
    if isinstance(value, list):
        if len(value) >= ROWS_THRESHOLD and all(isinstance(v, (dict, list)) for v in value):
            return True
        return any(_looks_like_rows(v, depth + 1) for v in value[:50])
    if isinstance(value, dict):
        return any(_looks_like_rows(v, depth + 1) for v in value.values())
    return False


async def _run_source(source, inputs, namespace):
    tree = ast.parse(source)
    defines_main = any(isinstance(n, ast.AsyncFunctionDef) and n.name == "main" for n in tree.body)
    top_level_await = any(isinstance(n, (ast.Await, ast.Return, ast.AsyncFor, ast.AsyncWith))
                          for stmt in tree.body
                          if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                          for n in ast.walk(stmt))
    namespace["inputs"] = inputs
    if defines_main and not top_level_await:
        exec(compile(tree, "<script>", "exec"), namespace)
        return await namespace["main"](inputs)
    tree = ast.fix_missing_locations(_TopLevelReturns().visit(tree))
    namespace["_Returned"] = _Returned
    code = compile(tree, "<script>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    try:
        pending = eval(code, namespace)
        if asyncio.iscoroutine(pending):
            await pending
    except _Returned as returned:
        return returned.value
    return None


def run(script_path, form, handle, save=None):
    """Run the script once with form["inputs"]. Never raises for script or platform failures."""
    result = Result()
    result.problems = check_form(form)
    with open(script_path, encoding="utf-8") as f:
        source = f.read()
    namespace = {"__name__": "__main__", "tools": _Tools(form, handle, result),
                 "execution": _Execution(result, save), "llm": None}
    try:
        value = asyncio.run(_run_source(source, json.loads(json.dumps(form.get("inputs") or {})), namespace))
    except PlatformAbort as exc:
        result.aborted = str(exc) + " (run stopped as 'blocked': the return value is lost; files saved before it stay)"
        return result
    except Exception as exc:  # the run fails and the model sees only the error
        result.aborted = f"script raised {type(exc).__name__}: {exc}"
        return result
    try:
        size = len(json.dumps(value, allow_nan=False).encode())
    except (TypeError, ValueError) as exc:
        result.problems.append(f"return value is not plain JSON: {exc}")
        size = 0
    if size > MAX_RESULT_BYTES:
        result.aborted = f"Result exceeds the 64 KiB limit ({size} bytes)"
        return result
    if _looks_like_rows(value):
        result.problems.append("return value carries rows of data: write them to the destination and return counts")
    result.value = value
    return result


def _main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    script, form_path, fakes_path = argv[0], argv[1], argv[2]
    with open(form_path, encoding="utf-8") as f:
        form = json.load(f)
    if "--inputs" in argv:
        form["inputs"] = json.loads(argv[argv.index("--inputs") + 1])
    spec = importlib.util.spec_from_file_location("fakes", fakes_path)
    fakes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fakes)
    res = run(script, form, fakes.handle, getattr(fakes, "save", None))
    counts = {}
    for name, _ in res.calls:
        counts[name] = counts.get(name, 0) + 1
    print(json.dumps({"form_problems": [p for p in res.problems if "return value" not in p],
                      "aborted": res.aborted, "tool_calls": counts, "saved": res.saved,
                      "return_problems": [p for p in res.problems if "return value" in p],
                      "returned": res.value}, indent=2, default=str)[:4000])
    return 1 if res.problems or res.aborted else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
