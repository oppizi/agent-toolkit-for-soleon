"""AHP-1834 — background calls in a pulled agent's local run.

As on Soleon: every configured subagent, workflow, spawn_worker and
integration helper call answers at once with an id, the agent keeps working,
and the finished result reaches it (locally: on its next tool result); it can
wait for, stop, or start any other tool in the background; its SubagentStop
hook holds the answer while a call is open; nothing outlives the server.
"""
from __future__ import annotations

import json
import sys
import threading
import time

from _local_emulation_fixtures import BIN, SLUG

sys.path.insert(0, str(BIN))
import soleon_agent_tools_mcp as shim  # noqa: E402
import soleon_background as bgm  # noqa: E402

DELEGATES = {
    "subagent_research": {"kind": "subagent", "name": "Researcher", "offered": True,
                          "description": "Researcher.", "system": "Search the web.", "model": "sonnet",
                          "external": []},
}

RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "3 checked sources"})


class _Proc:
    """A headless helper run that finishes when released (or is killed)."""

    def __init__(self, gate, started):
        self.gate, self.killed, self.returncode = gate, False, None
        started.append(self)

    def communicate(self, timeout=None):
        deadline = time.monotonic() + (timeout or 30)
        while not self.gate.is_set() and not self.killed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.returncode = -9 if self.killed else 0
        return ("" if self.killed else RESULT), ""

    def kill(self):
        self.killed = True


def _server(tmp_path, *, gate=None, started=None, tools=()):
    (tmp_path / "config.json").write_text(json.dumps({"loop": {"tokenBudgetEnabled": False}}))
    (tmp_path / "delegates.json").write_text(json.dumps({"schemaVersion": 1, "delegates": DELEGATES}))
    gate = gate or threading.Event()
    started = [] if started is None else started
    runner = shim.LocalDelegateRunner(
        delegates=shim.load_delegates(str(tmp_path)), slug=SLUG, tools_path=str(tmp_path / "tools.json"),
        server_url="https://mcp-dev.oppizi.com/mcp", model="sonnet", binary="/opt/claude",
        popen=lambda cmd, **kw: _Proc(gate, started))
    background = bgm.BackgroundCalls(str(tmp_path))
    server = shim.AgentToolsServer(SLUG, list(tools), client=None, delegate_runner=runner,
                                   delegate_ids=["subagent_research"], background=background,
                                   agent_dir=str(tmp_path))
    return server, background, gate, started


def _text(out):
    return "\n".join(p.get("text", "") for p in out.get("content") or [])


def _until(cond, timeout=5):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    return cond()


def test_a_helper_answers_at_once_and_its_result_rides_on_the_next_tool_call(tmp_path):
    server, bg, gate, started = _server(tmp_path)
    out = server.call("subagent_research", {"task": "find sources"})
    assert _text(out).startswith("Started in the background as bg_1")
    assert _until(lambda: len(started) == 1)
    # The agent keeps working: another call answers while the helper runs.
    busy = server.call(bgm.WAIT_TOOL, {"timeout_s": 0.05})
    assert "Still running after" in _text(busy) and "3 checked sources" not in _text(busy)
    gate.set()
    assert _until(lambda: bg._calls["bg_1"].done)
    after = server.call(bgm.WAIT_TOOL, {})
    assert "[Background call bg_1 (subagent_research) finished" in _text(after)
    assert "3 checked sources" in _text(after)
    bg.close()


def test_wait_returns_the_result_when_the_call_finishes(tmp_path):
    server, bg, gate, _ = _server(tmp_path)
    server.call("subagent_research", {"task": "x"})
    threading.Timer(0.1, gate.set).start()
    out = server.call(bgm.WAIT_TOOL, {"ids": ["bg_1"]})
    assert "Finished" in _text(out) and "3 checked sources" in _text(out)
    bg.close()


def test_cancel_ends_the_helper_process_and_nothing_comes_back(tmp_path):
    server, bg, gate, started = _server(tmp_path)
    server.call("subagent_research", {"task": "x"})
    assert _until(lambda: len(started) == 1)
    out = server.call(bgm.STOP_TOOL, {"id": "bg_1", "mode": "cancel"})
    assert "is cancelled" in _text(out) and started[0].killed
    assert _until(lambda: bg._calls["bg_1"].done)
    assert "3 checked sources" not in _text(server.call(bgm.WAIT_TOOL, {}))
    bg.close()


def test_interrupt_ends_a_local_helper_and_says_so(tmp_path):
    server, bg, gate, started = _server(tmp_path)
    server.call("subagent_research", {"task": "x"})
    assert _until(lambda: len(started) == 1)
    out = server.call(bgm.STOP_TOOL, {"id": "bg_1", "mode": "interrupt"})
    assert started[0].killed
    assert "cannot hand back what it had so far" in _text(out)
    bg.close()


def test_at_most_eight_run_at_once(tmp_path):
    server, bg, gate, started = _server(tmp_path)
    for i in range(9):
        assert "Started in the background" in _text(server.call("subagent_research", {"task": str(i)}))
    assert _until(lambda: len(started) == 8)
    time.sleep(0.1)
    assert len(started) == 8  # the ninth waits for a place
    gate.set()
    assert _until(lambda: len(started) == 9)
    bg.close()


def test_run_in_background_refuses_an_unknown_tool(tmp_path):
    server, bg, _, _ = _server(tmp_path)
    out = server.call(bgm.RUN_TOOL, {"tool": "delete_everything", "arguments": {}})
    assert out.get("isError") and "is not one of your tools" in _text(out)
    bg.close()


def test_run_in_background_takes_the_prefixed_name_claude_code_calls_the_tool_by(tmp_path):
    """The routing section tells the agent this server's tools are `mcp__soleon-agent-tools__<name>`, and
    run_in_background's own description says "exactly as you would call it". Refusing the prefixed name made
    the agent write its first background build out twice (flyer-builder r42/r43, 2026-11-25)."""
    code_tool = {"name": "write_and_run_code", "description": "Run code.", "kind": "external",
                 "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}}}
    server, bg, _, _ = _server(tmp_path, tools=[code_tool])
    ran = []
    server._call = lambda n, a: (ran.append((n, a)), {"content": [{"type": "text", "text": "built"}]})[1]
    out = server.call(bgm.RUN_TOOL, {"tool": "mcp__soleon-agent-tools__write_and_run_code",
                                     "arguments": {"code": "print(1)"}})
    assert not out.get("isError") and _text(out).startswith("Started in the background as bg_1")
    assert _until(lambda: bg._calls["bg_1"].done)
    assert ran == [("write_and_run_code", {"code": "print(1)"})]
    # Another server's prefix is not this server's tool.
    other = server.call(bgm.RUN_TOOL, {"tool": "mcp__soleon-workspace__read_file", "arguments": {}})
    assert other.get("isError") and "is not one of your tools" in _text(other)
    bg.close()


def test_the_open_calls_are_written_for_the_stop_hook_and_removed_on_close(tmp_path):
    server, bg, gate, _ = _server(tmp_path)
    server.call("subagent_research", {"task": "x"})
    state = json.loads((tmp_path / bgm.OPEN_FILE).read_text())
    assert [c["id"] for c in state["open"]] == ["bg_1"]
    bg.close()
    assert not (tmp_path / bgm.OPEN_FILE).exists()


def test_the_stop_hook_holds_the_answer_twice_then_asks_for_a_cancel(tmp_path):
    (tmp_path / bgm.OPEN_FILE).write_text(json.dumps({"open": [{"id": "bg_1", "name": "subagent_research",
                                                                 "done": False, "seconds": 12}]}))
    first = bgm.hold_answer(tmp_path)
    assert first and "bg_1 (subagent_research, running for 12 s)" in first
    assert bgm.hold_answer(tmp_path)
    assert bgm.hold_answer(tmp_path) is None
    assert (tmp_path / bgm.CANCEL_FILE).exists()
    assert bgm.hold_answer(tmp_path / "nothing-here") is None


def test_the_cancel_request_from_the_hook_cancels_every_call(tmp_path):
    server, bg, gate, started = _server(tmp_path)
    server.call("subagent_research", {"task": "x"})
    assert _until(lambda: len(started) == 1)
    (tmp_path / bgm.CANCEL_FILE).write_text("cancel")
    assert _until(lambda: started[0].killed, timeout=5)
    bg.close()


def test_the_subagent_stop_hook_blocks_while_a_call_is_open(tmp_path, monkeypatch, capsys):
    import soleon_turn_hooks as hooks
    agent_dir = tmp_path / ".soleon" / "agents" / SLUG
    agent_dir.mkdir(parents=True)
    (agent_dir / bgm.OPEN_FILE).write_text(json.dumps({"open": [{"id": "bg_1", "name": "x", "done": True,
                                                                  "seconds": 3}]}))
    monkeypatch.setattr(hooks, "agent_dir_of", lambda hook: agent_dir)
    assert hooks.stop({"agent_type": SLUG}) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and "bg_1 (x, finished)" in out["reason"]


def test_a_helpers_own_server_has_no_background_calls(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    server = shim.AgentToolsServer(SLUG, [], client=None, agent_dir=str(tmp_path))
    assert not any(t["name"] in bgm.TOOL_NAMES for t in server.published)
