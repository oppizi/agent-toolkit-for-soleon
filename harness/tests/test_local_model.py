"""A pulled agent's code-run model calls are answered LOCALLY (headless Claude
Code), never by the platform model — USER 2026-11-17: "Testing locally should
never call Bedrock, ever." Covers bin/soleon_local_model.py and the poll loop
that hands the platform's `model_requests` to it (soleon_mcp_client.await_control)."""
from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from _local_emulation_fixtures import BIN, SLUG, TOOLS

sys.path.insert(0, str(BIN))
import soleon_agent_tools_mcp as shim  # noqa: E402
import soleon_local_model as lm  # noqa: E402
from soleon_mcp_client import await_control  # noqa: E402

SCHEMA = {"type": "object", "properties": {"sentiment": {"type": "string", "enum": ["Positive", "Negative"]}},
          "required": ["sentiment"], "additionalProperties": False}
TEXT = {"messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Say OK."}],
        "max_tokens": 20, "temperature": 0}
NATIVE = {**TEXT, "response_format": {"type": "json_schema", "schema": SCHEMA}}
TOOL = {**TEXT, "tools": [{"type": "function", "function": {"name": "sandbox_structured_result",
                                                         "parameters": SCHEMA}}],
        "tool_choice": "required"}
USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 7}


def _flags(cmd):
    return dict(zip(cmd[1:], cmd[2:]))


def test_a_text_call_is_one_tool_less_settings_less_claude_call_on_the_agents_model():
    cmd = lm.command("claude", "opus", TEXT)
    assert cmd[:3] == ["claude", "-p", "Say OK."]
    flags = _flags(cmd)
    assert flags["--model"] == "opus" and flags["--system-prompt"] == "Be brief."
    assert flags["--tools"] == "" and flags["--setting-sources"] == ""
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert flags["--output-format"] == "json" and "--json-schema" not in cmd


@pytest.mark.parametrize("body", [NATIVE, TOOL])
def test_a_structured_call_passes_its_schema(body):
    assert json.loads(_flags(lm.command("claude", "opus", body))["--json-schema"]) == SCHEMA


def test_a_repaired_call_carries_the_whole_conversation():
    body = {**TEXT, "messages": TEXT["messages"] + [
        {"role": "assistant", "content": "{\"sentiment\": \"Meh\"}"},
        {"role": "user", "content": "Meh is not allowed."}]}
    prompt = lm.command("claude", "opus", body)[2]
    assert "[user]\nSay OK." in prompt and "[assistant]" in prompt and prompt.endswith("Reply to the last user message.")


def test_completions_take_the_shape_the_sandbox_settles():
    text = lm.completion(TEXT, {"result": "OK", "usage": USAGE})
    assert text["choices"][0] == {"index": 0, "finish_reason": "stop",
                                  "message": {"role": "assistant", "content": "OK"}}
    assert text["usage"] == {"prompt_tokens": 152, "completion_tokens": 7, "total_tokens": 159}
    native = lm.completion(NATIVE, {"structured_output": {"sentiment": "Positive"}, "usage": USAGE})
    assert json.loads(native["choices"][0]["message"]["content"]) == {"sentiment": "Positive"}
    tool = lm.completion(TOOL, {"structured_output": {"sentiment": "Negative"}, "usage": USAGE})
    call, = tool["choices"][0]["message"]["tool_calls"]
    assert tool["choices"][0]["finish_reason"] == "tool_calls"
    assert call["function"]["name"] == "sandbox_structured_result" and json.loads(call["function"]["arguments"]) == {"sentiment": "Negative"}


def _answerer(stdout="", returncode=0, *, binary="claude", raises=None, seen=None):
    def run(cmd, **kwargs):
        if seen is not None:
            seen.append((cmd, kwargs))
        if raises:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="boom")
    return lm.LocalModelAnswerer("haiku", lambda: binary, run=run)


def test_the_answerer_returns_each_completion_by_request_id_and_runs_outside_the_project():
    seen = []
    out = json.dumps({"result": "OK", "usage": USAGE, "is_error": False})
    answers = _answerer(out, seen=seen).answer([{"request_id": "a" * 32, "body": TEXT},
                                                {"request_id": "b" * 32, "body": TEXT}])
    assert [a["request_id"] for a in answers] == ["a" * 32, "b" * 32]
    assert all(a["completion"]["choices"][0]["message"]["content"] == "OK" for a in answers)
    (cmd, kwargs), _ = seen
    assert "soleon-local-model-" in kwargs["cwd"] and kwargs["encoding"] == "utf-8"


@pytest.mark.parametrize("answerer, says", [
    (lambda: _answerer(binary=None), "no Claude Code binary"),
    (lambda: _answerer("not json", 1), "the local model failed"),
    (lambda: _answerer(json.dumps({"is_error": True, "result": "rate limited"})), "rate limited"),
    (lambda: _answerer(raises=subprocess.TimeoutExpired("claude", 600)), "did not answer within"),
    (lambda: _answerer(json.dumps({"result": "not json", "usage": USAGE})), "not the requested JSON"),
])
def test_a_failed_local_call_is_an_error_answer_never_a_silent_one(answerer, says):
    body = NATIVE if "JSON" in says else TEXT
    answer, = answerer().answer([{"request_id": "a" * 32, "body": body}])
    assert "completion" not in answer and says in answer["error"]


class _Platform:
    """get_agent_tool_result: first hands over a model call, then finishes once answered."""

    def __init__(self):
        self.calls = []

    @staticmethod
    def _sleep(_s):
        pass

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if args.get("model_answers"):
            return {"state": "done", "call_id": args["call_id"], "result": {"result": "ran"}}
        return {"state": "pending", "call_id": args["call_id"]}


WAITING = {"state": "pending", "call_id": "lc_x",
           "model_requests": [{"request_id": "a" * 32, "model": "us.anthropic.claude-opus-5-5", "body": TEXT}]}


def test_the_poll_answers_the_waiting_model_calls_and_sends_them_back():
    platform = _Platform()
    answered = []

    def answer(requests):
        answered.extend(requests)
        return [{"request_id": r["request_id"], "completion": {"choices": []}} for r in requests]

    done = await_control(platform, SLUG, WAITING, answer_models=answer)
    assert done["state"] == "done" and [r["request_id"] for r in answered] == ["a" * 32]
    name, args = platform.calls[0]
    assert name == "get_agent_tool_result"
    assert args["model_answers"] == [{"request_id": "a" * 32, "completion": {"choices": []}}]


def test_without_an_answerer_the_calls_fail_fast_instead_of_waiting():
    platform = _Platform()
    await_control(platform, SLUG, WAITING)
    assert platform.calls[0][1]["model_answers"][0]["error"] == "this local caller cannot answer model calls"


def test_the_tool_server_answers_with_its_local_model():
    platform = _Platform()
    seen = []

    class _Answerer:
        def answer(self, requests):
            seen.extend(requests)
            return [{"request_id": r["request_id"], "error": "x"} for r in requests]

    def first(name, args):
        platform.calls.append((name, args))
        return WAITING if name == "call_agent_tool" else _Platform.call_tool(platform, name, args)

    platform.call_tool = first
    server = shim.AgentToolsServer(SLUG, TOOLS,
                                   client=platform, local_model=_Answerer())
    name = server.published[0]["name"]
    server.call(name, {})
    assert seen and seen[0]["request_id"] == "a" * 32


def test_a_reply_too_large_for_the_platform_is_sent_as_an_error():
    big = json.dumps({"result": "x" * (lm.MAX_ANSWER_CHARS + 1), "usage": USAGE})
    answer, = _answerer(big).answer([{"request_id": "a" * 32, "body": TEXT}])
    assert "completion" not in answer and "larger than" in answer["error"]


def test_answers_lost_to_a_network_drop_are_sent_once_more():
    from soleon_mcp_client import TransportError
    platform = _Platform()
    real = platform.call_tool
    drops = []

    def flaky(name, args):
        if args.get("model_answers") and not drops:
            drops.append(1)
            raise TransportError("connection reset")
        return real(name, args)
    platform.call_tool = flaky
    done = await_control(platform, SLUG, WAITING, answer_models=lambda rs: [
        {"request_id": r["request_id"], "error": "x"} for r in rs])
    assert done["state"] == "done" and drops == [1]
