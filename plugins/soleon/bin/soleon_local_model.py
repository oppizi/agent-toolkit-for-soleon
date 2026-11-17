#!/usr/bin/env python3
"""Answer a pulled agent's sandbox model calls with the LOCAL model.

When a pulled agent's code run (`write_and_run_code` / `run_code_file`, run on
Soleon) calls `llm.generate` or `llm.map`, the platform does not use the
agent's model: it hands each call back to this caller in the poll's
`model_requests` (agent-infra `containers/shared/code_execution/local_models.py`).
USER 2026-11-17: "Testing locally should never call Bedrock, ever."

Each request is the OpenAI-shaped chat body the sandbox would have sent to its
proxy: `messages` (system first, then user/assistant turns), `max_tokens`, and
for a structured call either `response_format: {type: json_schema, schema}` or
one result tool in `tools`. It runs as ONE headless Claude Code call
(`claude -p`) on the agent's local model, with no tools, no settings and no
session, and comes back as the completion object the sandbox reads: a text
reply, a native JSON reply, or a result-tool call, with `usage`.

Stdlib only, Python 3.9-compatible, like every script the plugin ships.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional

#: One model call ends well inside this; the ceiling stops a wedged process.
CALL_TIMEOUT_S = 600
#: Calls of one batch (`llm.map`) run together, as they do on the platform.
MAX_PARALLEL = 8
DEFAULT_SYSTEM = "You answer the request exactly as asked."
UTF8 = {"encoding": "utf-8", "errors": "replace"}


def _prompt(messages: List[Dict[str, Any]]) -> "tuple[str, str]":
    """(system prompt, prompt) for one `claude -p` call."""
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    turns = [m for m in messages if m.get("role") in ("user", "assistant")]
    if len(turns) == 1:
        return system, turns[0]["content"]
    lines = ["The conversation so far, oldest first:"]
    for m in turns:
        lines.append("[{}]\n{}".format(m["role"], m["content"]))
    lines.append("Reply to the last user message.")
    return system, "\n\n".join(lines)


def _schema(body: Dict[str, Any]) -> "tuple[Optional[dict], str, str]":
    """(JSON schema, form, result tool name): form is `native`, `tool` or `text`.
    A structured call offers exactly one tool, the sandbox's result tool; the
    answer calls it by the name the request gave it."""
    response_format = body.get("response_format") or {}
    if response_format.get("type") == "json_schema" and isinstance(response_format.get("schema"), dict):
        return response_format["schema"], "native", ""
    tools = body.get("tools") or []
    if len(tools) == 1:
        fn = (tools[0] or {}).get("function") or {}
        if fn.get("name") and isinstance(fn.get("parameters"), dict):
            return fn["parameters"], "tool", str(fn["name"])
    return None, "text", ""


def command(binary: str, model: str, body: Dict[str, Any]) -> List[str]:
    system, prompt = _prompt(body.get("messages") or [])
    schema, _form, _name = _schema(body)
    cmd = [binary, "-p", prompt,
           "--model", model,
           "--system-prompt", system or DEFAULT_SYSTEM,
           "--setting-sources", "",
           "--tools", "",
           "--strict-mcp-config",
           "--no-session-persistence",
           "--output-format", "json"]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    return cmd


def completion(body: Dict[str, Any], out: Dict[str, Any]) -> Dict[str, Any]:
    """The completion object the sandbox settles, from `claude -p` JSON output."""
    usage = out.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0) \
        + int(usage.get("cache_read_input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    _schema_obj, form, tool_name = _schema(body)
    if form == "text":
        message = {"role": "assistant", "content": str(out.get("result") or "")}
        finish = "stop"
    else:
        value = out.get("structured_output")
        if value is None:
            value = json.loads(str(out.get("result") or ""))
        if form == "native":
            message = {"role": "assistant", "content": json.dumps(value)}
            finish = "stop"
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_local", "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(value)}}]}
            finish = "tool_calls"
    return {"object": "chat.completion", "model": out.get("model") or "local",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens}}


class LocalModelAnswerer:
    """Answers `model_requests` with headless Claude Code on the agent's model."""

    def __init__(self, model: str, binary_finder: Callable[[], Optional[str]],
                 run: Callable[..., Any] = subprocess.run, timeout_s: int = CALL_TIMEOUT_S,
                 log: Callable[[str], None] = lambda _m: None):
        self.model = model
        self._find = binary_finder
        self.binary: Optional[str] = None
        self._run = run
        self.timeout_s = timeout_s
        self._log = log

    def _one(self, request: Dict[str, Any]) -> Dict[str, Any]:
        request_id = request.get("request_id")
        body = request.get("body")
        if not isinstance(body, dict):
            return {"request_id": request_id, "error": "the model call carried no request body"}
        if not self.binary:
            self.binary = self._find()
        if not self.binary:
            return {"request_id": request_id, "error": (
                "no Claude Code binary was found to answer this model call locally "
                "(CLAUDE_CODE_EXECPATH is unset and `claude` is not on PATH)")}
        try:
            # An empty working directory: no project CLAUDE.md reaches the call.
            with tempfile.TemporaryDirectory(prefix="soleon-local-model-") as cwd:
                done = self._run(command(self.binary, self.model, body), capture_output=True,
                                 timeout=self.timeout_s, cwd=cwd, **UTF8)
        except subprocess.TimeoutExpired:
            return {"request_id": request_id,
                    "error": "the local model did not answer within {} s".format(self.timeout_s)}
        except OSError as exc:
            return {"request_id": request_id, "error": "could not start Claude Code: {}".format(exc)}
        try:
            out = json.loads(done.stdout or "")
        except ValueError:
            out = None
        if done.returncode != 0 or not isinstance(out, dict) or out.get("is_error"):
            detail = (out or {}).get("result") if isinstance(out, dict) else (done.stderr or done.stdout)
            return {"request_id": request_id,
                    "error": "the local model failed: {}".format(str(detail or "no output")[:500])}
        try:
            return {"request_id": request_id, "completion": completion(body, out)}
        except (ValueError, TypeError) as exc:
            return {"request_id": request_id,
                    "error": "the local model's reply was not the requested JSON: {}".format(exc)}

    def answer(self, requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self._log("answering {} sandbox model call(s) locally on {}".format(len(requests), self.model))
        results: List[Optional[Dict[str, Any]]] = [None] * len(requests)
        slots = threading.BoundedSemaphore(MAX_PARALLEL)

        def work(index: int, request: Dict[str, Any]) -> None:
            with slots:
                results[index] = self._one(request)

        threads = [threading.Thread(target=work, args=(i, r), daemon=True) for i, r in enumerate(requests)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return [r if r is not None else {"request_id": requests[i].get("request_id"),
                                         "error": "the local model call did not finish"}
                for i, r in enumerate(results)]
