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

A local knowledge-base build (the `build-knowledge-base` skill) sends the same
shape for its distill calls, plus two things a code run never does: SEVERAL
tools in one call (the distill may look pages up before it writes its plan)
and a conversation that carries earlier tool calls and their results. With
several tools the model answers one JSON object naming the tool it calls and
its arguments (`tool_choice` "auto" also offers a plain reply), which comes
back as that tool's call.

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
#: The platform stores an answer beside its request in one DynamoDB item and
#: refuses a larger one (agent-infra local_call_verbs._MODEL_ANSWER_MAX_CHARS).
MAX_ANSWER_CHARS = 180_000
UTF8 = {"encoding": "utf-8", "errors": "replace"}


#: The answer to a several-tool call that chose to reply in plain text.
REPLY_CHOICE = "reply"


def _turn_text(m: Dict[str, Any]) -> str:
    """One conversation turn as text: its words, its tool calls, or a tool result."""
    if m.get("role") == "tool":
        return "[result of tool call {}]\n{}".format(m.get("tool_call_id") or "", m.get("content") or "")
    parts = [m["content"]] if isinstance(m.get("content"), str) and m["content"] else []
    for call in m.get("tool_calls") or []:
        fn = (call or {}).get("function") or {}
        parts.append("(called {} with {}, call id {})".format(
            fn.get("name"), fn.get("arguments") or "{}", (call or {}).get("id") or ""))
    return "[{}]\n{}".format(m.get("role"), "\n".join(parts))


def _prompt(messages: List[Dict[str, Any]]) -> "tuple[str, str]":
    """(system prompt, prompt) for one `claude -p` call."""
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    turns = [m for m in messages if m.get("role") in ("user", "assistant", "tool")]
    if len(turns) == 1 and turns[0].get("role") == "user":
        return system, turns[0]["content"]
    lines = ["The conversation so far, oldest first:"]
    lines.extend(_turn_text(m) for m in turns)
    lines.append("Continue from the last tool result." if turns[-1].get("role") == "tool"
                 else "Reply to the last user message.")
    return system, "\n\n".join(lines)


def _offered(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tools this call may answer with: all of them, or the one
    `tool_choice` names."""
    tools = [((t or {}).get("function") or {}) for t in body.get("tools") or []]
    tools = [fn for fn in tools if fn.get("name")]
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        named = ((choice.get("function") or {}).get("name"))
        return [fn for fn in tools if fn["name"] == named] or tools
    return tools


def _choice_schema(tools: List[Dict[str, Any]], allow_reply: bool) -> dict:
    names = [fn["name"] for fn in tools] + ([REPLY_CHOICE] if allow_reply else [])
    return {"type": "object", "properties": {
        "tool": {"type": "string", "enum": names},
        "arguments": {"type": "object"}}, "required": ["tool", "arguments"]}


def _tools_brief(tools: List[Dict[str, Any]], allow_reply: bool) -> str:
    lines = ["Answer with ONE JSON object: {\"tool\": <name>, \"arguments\": <that tool's input>}. "
             "Call exactly one of these tools:"]
    for fn in tools:
        lines.append("- {}: {}\n  input schema: {}".format(
            fn["name"], fn.get("description") or "", json.dumps(fn.get("parameters") or {})))
    if allow_reply:
        lines.append('- {}: answer in words instead; arguments {{"text": "<your answer>"}}'.format(REPLY_CHOICE))
    return "\n".join(lines)


def _schema(body: Dict[str, Any]) -> "tuple[Optional[dict], str, str]":
    """(JSON schema, form, result tool name): form is `native`, `tool`,
    `choose` or `text`. A structured call offers exactly one tool, the
    sandbox's result tool; the answer calls it by the name the request gave
    it. Several tools are `choose`: the answer names the one it calls."""
    response_format = body.get("response_format") or {}
    if response_format.get("type") == "json_schema" and isinstance(response_format.get("schema"), dict):
        return response_format["schema"], "native", ""
    tools = _offered(body)
    if len(tools) == 1 and isinstance(tools[0].get("parameters"), dict):
        return tools[0]["parameters"], "tool", str(tools[0]["name"])
    if len(tools) > 1:
        return _choice_schema(tools, body.get("tool_choice") == "auto"), "choose", ""
    return None, "text", ""


def prompt_of(body: Dict[str, Any]) -> str:
    """The prompt of one call, sent on STDIN: a distill prompt runs to tens
    of thousands of characters, and Windows caps a whole command line at
    32,767 (2026-11-17: "[WinError 206] The filename or extension is too
    long" failed the first local knowledge-base build)."""
    return _prompt(body.get("messages") or [])[1]


def command(binary: str, model: str, body: Dict[str, Any]) -> List[str]:
    """The `claude -p` command line for one call; its prompt goes on stdin (`prompt_of`)."""
    system, _prompt_text = _prompt(body.get("messages") or [])
    schema, form, _name = _schema(body)
    if form == "choose":
        brief = _tools_brief(_offered(body), body.get("tool_choice") == "auto")
        system = "{}\n\n{}".format(system, brief) if system else brief
    cmd = [binary, "-p",
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
        elif form == "choose":
            if not isinstance(value, dict) or not isinstance(value.get("tool"), str):
                raise ValueError("the answer names no tool")
            arguments = value.get("arguments") if isinstance(value.get("arguments"), dict) else {}
            if value["tool"] == REPLY_CHOICE:
                message = {"role": "assistant", "content": str(arguments.get("text") or "")}
                finish = "stop"
            else:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_local", "type": "function",
                    "function": {"name": value["tool"], "arguments": json.dumps(arguments)}}]}
                finish = "tool_calls"
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
                done = self._run(command(self.binary, self.model, body), input=prompt_of(body),
                                 capture_output=True, timeout=self.timeout_s, cwd=cwd, **UTF8)
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
            reply = completion(body, out)
            if len(json.dumps(reply, separators=(",", ":"))) > MAX_ANSWER_CHARS:
                return {"request_id": request_id, "error": (
                    "the local model's reply is larger than the {} characters one answer may carry; "
                    "ask for less per call".format(MAX_ANSWER_CHARS))}
            return {"request_id": request_id, "completion": reply}
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
