"""The Per Message Token Budget on a LOCAL run: the agent and every helper it
starts share one allowance, counted and enforced the way Soleon does."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from _local_emulation_fixtures import BIN, SLUG

sys.path.insert(0, str(BIN))
import soleon_agent_tools_mcp as shim  # noqa: E402
import soleon_message_budget as mb  # noqa: E402


# --- the budget --------------------------------------------------------------

@pytest.mark.parametrize("config,expected", [
    ({"loop": {"tokenBudget": 500000, "tokenBudgetEnabled": True}}, 500000),
    ({"loop": {"tokenBudget": 120000}}, 120000),                       # no flag = on
    ({"loop": {"tokenBudget": 0}}, 500000),                            # legacy zero = the default
    ({"loop": {"tokenBudget": 120000, "tokenBudgetEnabled": False}}, 0),
    ({}, 500000),
    ({"tokenBudget": 90000}, 90000),                                   # flat shape
])
def test_the_budget_resolves_like_the_platforms(config, expected):
    assert mb.message_budget(config) == expected


# --- counting -----------------------------------------------------------------

def _call(msg_id, blocks, inp=10, read=30000, write=500, out=100):
    usage = {"input_tokens": inp, "cache_read_input_tokens": read,
             "cache_creation_input_tokens": write, "output_tokens": out}
    return [{"message": {"id": msg_id, "role": "assistant", "usage": usage,
                         "content": [{"type": b}]}} for b in blocks]


def _transcript(path: Path, calls):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for call in calls:
            for rec in call:
                fh.write(json.dumps(rec) + "\n")
    return str(path)


def test_a_model_call_counts_once_though_claude_code_writes_it_per_block(tmp_path):
    """Claude Code writes one line per content block (thinking, tool_use),
    each carrying the whole call's usage — summing lines doubled the spend."""
    p = _transcript(tmp_path / "t.jsonl", [_call("m1", ["thinking", "tool_use"]),
                                           _call("m2", ["text"], read=40000)])
    assert mb.transcript_tokens(p) == (10 + 30000 + 500 + 100) + (10 + 40000 + 500 + 100)


def test_cached_tokens_count_like_soleons_accountant(tmp_path):
    """Soleon's proxy counts cached reads and writes as input tokens."""
    p = _transcript(tmp_path / "t.jsonl", [_call("m1", ["text"], inp=0, read=7, write=5, out=3)])
    assert mb.transcript_tokens(p) == 15


def test_a_finished_helper_counts_every_call_not_just_its_last():
    """`usage` in a `claude -p` result covers only the LAST call; `modelUsage`
    is the run's total."""
    result = {"usage": {"input_tokens": 1, "output_tokens": 1},
              "modelUsage": {"claude-sonnet-5": {"inputTokens": 900, "outputTokens": 700,
                                                 "cacheReadInputTokens": 90000,
                                                 "cacheCreationInputTokens": 11000}}}
    assert mb.result_tokens(result) == 102600


# --- the agent's hook -----------------------------------------------------------

def _pulled(tmp_path, budget=100000, enabled=None):
    agent_dir = tmp_path / ".soleon" / "agents" / SLUG
    agent_dir.mkdir(parents=True)
    (agent_dir / "pull.json").write_text(json.dumps({"slug": SLUG}))
    loop = {"tokenBudget": budget}
    if enabled is not None:
        loop["tokenBudgetEnabled"] = enabled
    (agent_dir / "config.json").write_text(json.dumps({"loop": loop}))
    (agent_dir / ".local-turn.json").write_text(json.dumps({"turn": "t1", "agentId": "a1"}))
    return agent_dir


def _hook(tmp_path, agent_id="a1", agent_type=SLUG, tool="mcp__soleon-agent-tools__mcp_gmail_read"):
    session = tmp_path / "sessions" / "s1.jsonl"
    return {"session_id": "s1", "transcript_path": str(session), "cwd": str(tmp_path),
            "agent_id": agent_id, "agent_type": agent_type, "hook_event_name": "PreToolUse",
            "tool_name": tool}


def _agent_spend(tmp_path, tokens_per_call, calls=1, agent_id="a1"):
    path = tmp_path / "sessions" / "s1" / "subagents" / "agent-{}.jsonl".format(agent_id)
    _transcript(path, [_call("m%d" % i, ["tool_use"], inp=tokens_per_call, read=0, write=0, out=0)
                       for i in range(calls)])


def _run_agent_hook(hook, capsys):
    rc = mb.main(["agent-pretool"], stream=io.StringIO(json.dumps(hook)))
    return rc, capsys.readouterr().out.strip()


def test_under_the_wind_down_point_the_agent_keeps_its_tools(tmp_path, capsys):
    _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 84000)
    rc, out = _run_agent_hook(_hook(tmp_path), capsys)
    assert rc == 0 and out == ""


def test_at_the_wind_down_point_a_tool_call_is_refused_and_says_why(tmp_path, capsys):
    """Soleon stops the agent's tool use at 85% of the budget; the answer
    itself needs no tool, so it is never blocked."""
    _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 85000)
    rc, out = _run_agent_hook(_hook(tmp_path), capsys)
    decision = json.loads(out)["hookSpecificOutput"]
    assert rc == 0 and decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert reason.startswith("per-message token budget exhausted: used=85000 budget=100000")
    assert "85,000 of its 100,000-token Per Message Token Budget" in reason
    assert "answer now" in reason


def test_helpers_spend_counts_against_the_agents_message(tmp_path, capsys):
    """The inbox run's failure: the agent itself spent little, its helpers
    spent the message's budget. Their spend must stop the agent too."""
    agent_dir = _pulled(tmp_path, budget=500000)
    _agent_spend(tmp_path, 40000)
    ledger = mb.Ledger(agent_dir)
    for run in range(10):
        ledger.note_helper("t1", "r%d" % run, 50000)
    rc, out = _run_agent_hook(_hook(tmp_path), capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "used=540000 budget=500000" in out


def test_a_new_message_starts_from_zero(tmp_path, capsys):
    agent_dir = _pulled(tmp_path, budget=100000)
    mb.Ledger(agent_dir).note_helper("an-earlier-turn", "r1", 99999)
    _agent_spend(tmp_path, 1000)
    _, out = _run_agent_hook(_hook(tmp_path), capsys)
    assert out == ""


def test_a_switched_off_budget_refuses_nothing(tmp_path, capsys):
    _pulled(tmp_path, budget=100000, enabled=False)
    _agent_spend(tmp_path, 10_000_000)
    _, out = _run_agent_hook(_hook(tmp_path), capsys)
    assert out == ""


@pytest.mark.parametrize("change", [
    {"agent_id": None},                  # the main conversation
    {"agent_type": "some-other-agent"},  # a subagent that is not a pulled Soleon agent
])
def test_everything_that_is_not_a_pulled_agent_run_is_untouched(tmp_path, capsys, change):
    _pulled(tmp_path, budget=1000)
    _agent_spend(tmp_path, 10_000_000)
    hook = _hook(tmp_path)
    hook.update(change)
    _, out = _run_agent_hook(hook, capsys)
    assert out == ""


# --- a helper's own hook ----------------------------------------------------------

def test_a_running_helper_is_stopped_once_the_message_is_spent(tmp_path, capsys):
    """Soleon refuses a helper's model calls mid-run once the turn is spent.
    Locally the runner books the helper's calls in the ledger as they stream
    in, and the helper's own hook decides from the whole message's spend."""
    agent_dir = _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 20000)
    ledger = mb.Ledger(agent_dir)
    ledger.note_agent("t1", mb.subagent_transcript(str(tmp_path / "sessions" / "s1.jsonl"), "a1"))
    ledger.note_helper("t1", "r1", 70000)
    hook = {"tool_name": "mcp__soleon-agent-tools__search_emails"}
    rc = mb.main(["helper-pretool", "--agent-dir", str(agent_dir), "--turn", "t1", "--run", "r1"],
                 stream=io.StringIO(json.dumps(hook)))
    decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert rc == 0 and decision["permissionDecision"] == "deny"
    assert "used=90000" in decision["permissionDecisionReason"]
    assert "this helper must answer now" in decision["permissionDecisionReason"]


def test_a_stream_event_books_its_calls_input_and_output():
    event = {"type": "assistant", "message": {"id": "m1", "usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 1748, "cache_read_input_tokens": 27536,
        "output_tokens": 4}}}
    assert mb.stream_call_tokens(event) == ("m1", 29298)
    assert mb.stream_call_tokens({"type": "user"}) is None


# --- the runner -------------------------------------------------------------------

WRAPPER = {"name": "mcp_gmail_read", "subagentPair": True, "approval": False,
           "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
           "localWorker": {"members": ["mcp_gmail_read::search_emails"], "prompt": "You read Gmail.",
                           "maxIterations": 12, "role": "read"}}

FINAL = {"type": "result", "subtype": "success", "is_error": False, "result": "3 unread",
         "modelUsage": {"m": {"inputTokens": 1000, "outputTokens": 500,
                              "cacheReadInputTokens": 60000, "cacheCreationInputTokens": 3500}}}


def _assistant(msg_id, tokens):
    return {"type": "assistant", "message": {"id": msg_id, "usage": {"input_tokens": tokens}}}


class _Popen:
    """A helper process streaming `events`; `on_line` sees the ledger after
    each line is consumed (what the helper's own hook would read then)."""

    def __init__(self, events, cmd, seen, ledger_path, returncode=0):
        self.cmd, self._seen, self._ledger_path = cmd, seen, ledger_path
        self.returncode = returncode
        self._lines = [json.dumps(e) + "\n" for e in events]
        self.stderr = io.StringIO("")

    @property
    def stdout(self):
        for line in self._lines:
            yield line

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def _runner(agent_dir, events=None, calls=None):
    calls = calls if calls is not None else []
    events = events if events is not None else [_assistant("m1", 30000), _assistant("m1", 30000),
                                                 _assistant("m2", 35000), FINAL]

    def fake_popen(cmd, **opts):
        calls.append((cmd, opts))
        return _Popen(events, cmd, calls, agent_dir / mb.LEDGER_NAME)

    def fake_run(cmd, **opts):
        calls.append((cmd, opts))

        class P:
            stdout, stderr, returncode = json.dumps(FINAL), "", 0
        return P()
    return shim.LocalWorkerRunner(slug=SLUG, tools_path=str(agent_dir / "tools.json"),
                                  server_url="https://mcp-dev.oppizi.com/mcp", model="sonnet",
                                  binary="/opt/claude", runner=fake_run, popen=fake_popen), calls


def test_a_helper_runs_under_the_budget_hook_in_the_agents_turn(tmp_path):
    agent_dir = _pulled(tmp_path)
    runner, calls = _runner(agent_dir)
    out = runner.run(WRAPPER, "what came in?", approved=False, turn="t1")
    assert out["content"][0]["text"] == "3 unread" and not out.get("isError")
    cmd, _ = calls[0]
    flags = dict(zip(cmd, cmd[1:]))
    assert flags["--output-format"] == "stream-json" and "--verbose" in cmd
    assert "--no-session-persistence" in cmd                       # nothing left behind
    hook_cmd = json.loads(flags["--settings"])["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "helper-pretool" in hook_cmd and "--turn t1" in hook_cmd
    assert flags["--setting-sources"] == ""                        # still no plugin hooks inside it
    args = json.loads(flags["--mcp-config"])["mcpServers"]["soleon-agent-tools"]["args"]
    assert args[args.index("--turn") + 1] == "t1"                  # its platform calls join the agent's turn


def test_a_helpers_calls_are_booked_as_they_stream_and_settled_from_its_total(tmp_path):
    """While it runs: each call once (a call streams once per content block).
    When it ends: the run's `modelUsage`, which carries the final output counts."""
    agent_dir = _pulled(tmp_path)
    seen = []
    orig = mb.Ledger.note_helper

    def spy(self, turn, run, tokens):
        seen.append(tokens)
        return orig(self, turn, run, tokens)
    mb.Ledger.note_helper = spy
    try:
        runner, _ = _runner(agent_dir)
        runner.run(WRAPPER, "x", approved=False, turn="t1")
    finally:
        mb.Ledger.note_helper = orig
    assert seen == [30000, 30000, 65000, 65000]
    state = json.loads((agent_dir / mb.LEDGER_NAME).read_text())
    assert list(state["helpers"].values()) == [65000]


def test_a_helper_that_dies_keeps_the_spend_it_streamed(tmp_path):
    agent_dir = _pulled(tmp_path)
    runner, _ = _runner(agent_dir, events=[_assistant("m1", 42000)])
    out = runner.run(WRAPPER, "x", approved=False, turn="t1")
    assert out.get("isError")
    assert list(json.loads((agent_dir / mb.LEDGER_NAME).read_text())["helpers"].values()) == [42000]


def test_with_the_budget_off_a_helper_runs_as_before(tmp_path):
    agent_dir = _pulled(tmp_path, enabled=False)
    runner, calls = _runner(agent_dir)
    runner.run(WRAPPER, "x", approved=False, turn="t1")
    cmd, _ = calls[0]
    flags = dict(zip(cmd, cmd[1:]))
    assert flags["--output-format"] == "json" and "--settings" not in cmd
    assert not (agent_dir / mb.LEDGER_NAME).exists()


def test_the_tools_server_hands_its_turn_to_the_helper(tmp_path):
    agent_dir = _pulled(tmp_path)
    runner, calls = _runner(agent_dir)
    server = shim.AgentToolsServer(SLUG, [WRAPPER], client=None, local_workers=runner,
                                   turn=shim.TurnTracker(None, fixed="t9"))
    server.call("mcp_gmail_read", {"prompt": "x"})
    flags = dict(zip(calls[0][0], calls[0][0][1:]))
    assert "--turn t9" in json.loads(flags["--settings"])["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_a_worker_server_told_its_turn_never_mints_its_own(tmp_path):
    """A worker's server starts minutes into the agent's run, when the hook's
    turn file already reads as stale; minting would split the message."""
    path = tmp_path / ".local-turn.json"
    path.write_text(json.dumps({"turn": "hook-turn", "agentId": "a1", "startedAt": 0}))
    assert shim.TurnTracker(str(path), fixed="t1").current() == "t1"
    assert json.loads(path.read_text())["turn"] == "hook-turn"     # the agent's file is not overwritten
