"""The Per Message Token Budget on a LOCAL run: the agent and every helper it
starts share one allowance, counted and enforced the way Soleon does."""
from __future__ import annotations

import io
import json
import re
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
    # each call: input incl. cache - 90% of reads + 25% of writes, + output
    assert mb.transcript_tokens(p) == (30510 - 27000 + 125 + 100) + (40510 - 36000 + 125 + 100)


def test_cached_tokens_count_like_soleons_accountant(tmp_path):
    """Soleon's proxy counts cache-weighted input (agent-infra #2279): a cache
    read is 10% of an input token, a cache write 125%."""
    p = _transcript(tmp_path / "t.jsonl", [_call("m1", ["text"], inp=0, read=70000, write=4000, out=3)])
    assert mb.transcript_tokens(p) == 74000 - 63000 + 1000 + 3


def test_a_finished_helper_counts_every_call_not_just_its_last():
    """`usage` in a `claude -p` result covers only the LAST call; `modelUsage`
    is the run's total."""
    result = {"usage": {"input_tokens": 1, "output_tokens": 1},
              "modelUsage": {"claude-sonnet-5": {"inputTokens": 900, "outputTokens": 700,
                                                 "cacheReadInputTokens": 90000,
                                                 "cacheCreationInputTokens": 11000}}}
    assert mb.result_tokens(result) == 101900 - 81000 + 2750 + 700


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


#: The message of agent run a1 for prompt p1 (each run of the agent is its own message).
MSG = "prompt:p1:a1"


def _hook(tmp_path, agent_id="a1", agent_type=SLUG, tool="mcp__soleon-agent-tools__mcp_gmail_read",
          prompt_id="p1", tool_use_id=None, mode=None, event="PreToolUse"):
    session = tmp_path / "sessions" / "s1.jsonl"
    hook = {"session_id": "s1", "transcript_path": str(session), "cwd": str(tmp_path),
            "agent_id": agent_id, "agent_type": agent_type, "hook_event_name": event,
            "tool_name": tool, "prompt_id": prompt_id}
    if tool_use_id:
        hook["tool_use_id"] = tool_use_id
    if mode:
        hook["permission_mode"] = mode
    return hook


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
        ledger.note_helper(MSG, "r%d" % run, 50000)
    rc, out = _run_agent_hook(_hook(tmp_path), capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "used=540000 budget=500000" in out


def test_a_new_message_starts_from_zero(tmp_path, capsys):
    agent_dir = _pulled(tmp_path, budget=100000)
    mb.Ledger(agent_dir).note_helper("prompt:an-earlier-prompt", "r1", 99999)
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
    ledger.note_run(MSG, "a1", mb.subagent_transcript(str(tmp_path / "sessions" / "s1.jsonl"), "a1"))
    ledger.note_helper(MSG, "r1", 70000)
    hook = {"tool_name": "mcp__soleon-agent-tools__search_emails"}
    rc = mb.main(["helper-pretool", "--agent-dir", str(agent_dir), "--message", MSG, "--run", "r1"],
                 stream=io.StringIO(json.dumps(hook)))
    decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert rc == 0 and decision["permissionDecision"] == "deny"
    assert "used=90000" in decision["permissionDecisionReason"]
    assert "this helper must answer now" in decision["permissionDecisionReason"]


def test_a_stream_event_books_its_calls_input_and_output():
    event = {"type": "assistant", "message": {"id": "m1", "usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 1748, "cache_read_input_tokens": 27536,
        "output_tokens": 4}}}
    assert mb.stream_call_tokens(event) == ("m1", round(29294 - 0.9 * 27536 + 0.25 * 1748) + 4)
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
    cmd, opts = calls[0]
    assert opts["encoding"] == "utf-8"  # never the locale's code page (cp1252 mangled every em dash)
    flags = dict(zip(cmd, cmd[1:]))
    assert flags["--output-format"] == "stream-json" and "--verbose" in cmd
    assert "--no-session-persistence" in cmd                       # nothing left behind
    hook_cmd = json.loads(flags["--settings"])["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "helper-pretool" in hook_cmd and "--message turn:t1" in hook_cmd   # no agent call touched the ledger
    assert flags["--setting-sources"] == ""                        # still no plugin hooks inside it
    args = json.loads(flags["--mcp-config"])["mcpServers"]["soleon-agent-tools"]["args"]
    assert args[args.index("--turn") + 1] == "t1"                  # its platform calls join the agent's turn


def test_a_spawn_worker_keeps_its_event_stream_and_stops_at_its_own_cap(tmp_path):
    """A local worker that went silent held its agent for 20 minutes with no
    record of where it stopped (2026-11-02). spawn_worker's runs keep their raw
    stream under .local-workers/, and stop after SPAWN_WORKER_TIMEOUT_S."""
    agent_dir = _pulled(tmp_path)
    (agent_dir / "tools.json").write_text(json.dumps({"state": "done", "result": {"tools": []}}))
    calls = []

    def fake_popen(cmd, **opts):
        calls.append(cmd)
        return _Popen([_assistant("m1", 30000), FINAL], cmd, calls, agent_dir / mb.LEDGER_NAME)
    worker = shim.LocalSpawnWorker(slug=SLUG, tools_path=str(agent_dir / "tools.json"),
                                   server_url="https://mcp-dev.oppizi.com/mcp", model="sonnet", binary="/opt/claude",
                                   popen=fake_popen, timeout_s=shim.SPAWN_WORKER_TIMEOUT_S)
    out = worker.spawn("review the pictures", turn="t1")
    assert json.loads(out["content"][0]["text"]) == {"workerResult": "3 unread"}
    logs = list((agent_dir / ".local-workers").glob("*.jsonl"))
    assert len(logs) == 1 and [json.loads(l)["type"] for l in logs[0].read_text().splitlines()] == ["assistant", "result"]
    assert shim.SPAWN_WORKER_TIMEOUT_S < shim.LOCAL_WORKER_TIMEOUT_S


def test_a_helpers_calls_are_booked_as_they_stream_and_settled_from_its_total(tmp_path):
    """While it runs: each call once (a call streams once per content block).
    When it ends: the run's `modelUsage`, which carries the final output counts."""
    agent_dir = _pulled(tmp_path)
    seen = []
    orig = mb.Ledger.note_helper

    def spy(self, message, run, tokens):
        seen.append(tokens)
        return orig(self, message, run, tokens)
    mb.Ledger.note_helper = spy
    try:
        runner, _ = _runner(agent_dir)
        runner.run(WRAPPER, "x", approved=False, turn="t1")
    finally:
        mb.Ledger.note_helper = orig
    assert seen == [30000, 30000, 65000, 64500 - 54000 + 875 + 500]  # settled: FINAL, cache-weighted
    state = json.loads((agent_dir / mb.LEDGER_NAME).read_text())
    assert list(state["helpers"].values()) == [11875]
    # The ledger names the helper that spent it.
    assert re.fullmatch(re.escape(WRAPPER["name"]) + r"-[0-9a-f]{12}", list(state["helpers"])[0])


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
    args = json.loads(flags["--mcp-config"])["mcpServers"]["soleon-agent-tools"]["args"]
    assert args[args.index("--turn") + 1] == "t9"


def test_a_helper_joins_the_message_of_the_call_that_started_it(tmp_path, capsys):
    """The wrapper call's own PreToolUse (the agent's, or a configured
    subagent's) touches the ledger just before the call reaches the runner."""
    agent_dir = _pulled(tmp_path)
    _agent_spend(tmp_path, 1000)
    _run_agent_hook(_hook(tmp_path), capsys)                          # touches MSG
    runner, calls = _runner(agent_dir)
    runner.run(WRAPPER, "x", approved=False, turn="t1")
    hook_cmd = json.loads(dict(zip(calls[0][0], calls[0][0][1:]))["--settings"])["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--message prompt:p1" in hook_cmd
    assert json.loads((agent_dir / mb.LEDGER_NAME).read_text())["helpers"]


def test_a_stale_message_is_not_joined(tmp_path):
    agent_dir = _pulled(tmp_path)
    clock = [1000.0]
    mb.Ledger(agent_dir, now=lambda: clock[0]).note_run("prompt:old", "a1", None)
    clock[0] += mb.JOIN_WINDOW_S + 1
    assert mb.Ledger(agent_dir, now=lambda: clock[0]).message_for_helper("turn:t2") == "turn:t2"


# --- configured subagents -----------------------------------------------------------

def test_a_configured_subagents_spend_counts_against_the_agents_message(tmp_path, capsys):
    """Locally a configured subagent (`<slug>--<id>`) runs outside the agent's
    own run — the conversation runs it for the agent — but on Soleon its
    tokens are the agent's message's. Same prompt, same message."""
    _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 50000, agent_id="a1")
    _run_agent_hook(_hook(tmp_path), capsys)
    _agent_spend(tmp_path, 40000, agent_id="s1")
    rc, out = _run_agent_hook(_hook(tmp_path, agent_id="s1", agent_type=SLUG + "--subagent_res1"), capsys)
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny" and "used=90000" in decision["permissionDecisionReason"]
    assert "this subagent must answer now" in decision["permissionDecisionReason"]
    # ...and the agent, run again for the same prompt, is refused too
    _, out = _run_agent_hook(_hook(tmp_path), capsys)
    assert "used=90000" in out


def test_the_next_prompt_is_a_new_message(tmp_path, capsys):
    _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 90000)
    _, out = _run_agent_hook(_hook(tmp_path), capsys)
    assert "deny" in out
    _agent_spend(tmp_path, 1000, agent_id="a2")
    _, out = _run_agent_hook(_hook(tmp_path, agent_id="a2", prompt_id="p2"), capsys)
    assert out == ""


def test_two_runs_of_the_agent_for_one_prompt_are_two_messages(tmp_path, capsys):
    """An eval suite runs the agent once per eval for ONE prompt of the person.
    On Soleon each eval is its own message; 2026-11-17 four such runs shared one
    allowance and each was refused after ~98k tokens."""
    _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 60000, agent_id="a1")
    _, out = _run_agent_hook(_hook(tmp_path, agent_id="a1"), capsys)
    assert out == ""
    _agent_spend(tmp_path, 60000, agent_id="a2")
    _, out = _run_agent_hook(_hook(tmp_path, agent_id="a2"), capsys)
    assert out == ""                                    # 60k of its own, not 120k
    # ...while a resumed run keeps its message and its spend
    _agent_spend(tmp_path, 90000, agent_id="a2")
    _, out = _run_agent_hook(_hook(tmp_path, agent_id="a2"), capsys)
    assert "deny" in out


def test_a_subagent_of_something_that_is_not_pulled_is_untouched(tmp_path, capsys):
    _pulled(tmp_path, budget=1000)
    _agent_spend(tmp_path, 10_000_000, agent_id="x")
    _, out = _run_agent_hook(_hook(tmp_path, agent_id="x", agent_type="other--helper"), capsys)
    assert out == ""


def test_a_worker_server_told_its_turn_never_mints_its_own(tmp_path):
    """A worker's server starts minutes into the agent's run, when the hook's
    turn file already reads as stale; minting would split the message."""
    path = tmp_path / ".local-turn.json"
    path.write_text(json.dumps({"turn": "hook-turn", "agentId": "a1", "startedAt": 0}))
    assert shim.TurnTracker(str(path), fixed="t1").current() == "t1"
    assert json.loads(path.read_text())["turn"] == "hook-turn"     # the agent's file is not overwritten


def test_an_extraction_jobs_save_tool_always_passes_and_its_reads_ask(tmp_path, capsys):
    """An extraction job works to its own budget, the agent's spend included
    (the platform counts it). Past 85% its reads go to the person for a waiver,
    but emit_document always passes: holding it back would save nothing."""
    agent_dir = _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 90000)
    (agent_dir / mb.EXTRACTION_FILE).write_text(json.dumps({"callId": "lc_x", "jobId": "ext_y"}))
    save = _hook(tmp_path, tool="mcp__soleon-agent-tools__emit_document", tool_use_id="t-save")
    assert _run_agent_hook(save, capsys) == (0, "")
    rc, out = _run_agent_hook(_hook(tmp_path, tool_use_id="t-read"), capsys)
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "extraction job's token budget" in decision["permissionDecisionReason"]
    assert "rest of this extraction job" in decision["permissionDecisionReason"]
    (agent_dir / mb.EXTRACTION_FILE).unlink()
    _, out = _run_agent_hook(_hook(tmp_path, tool="mcp__soleon-agent-tools__emit_document",
                                   tool_use_id="t-save2"), capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask", \
        "outside an extraction job emit_document is an ordinary tool"


def test_the_extraction_file_name_matches_the_tools_server():
    import soleon_agent_tools_mcp as shim
    assert mb.EXTRACTION_FILE == shim.EXTRACTION_FILE


def test_an_extraction_job_works_to_its_own_budget_not_the_message_budget(tmp_path):
    """2026-11-21: on the platform an extraction runs to its source's
    tokenBudgetMax; locally two refreshes stopped at the agent's 500,000
    per-message default while their source allowed 2,000,000."""
    (tmp_path / "config.json").write_text(json.dumps({"agents": {}}), encoding="utf-8")
    assert mb.budget_of(tmp_path) == mb.DEFAULT_LIMIT
    (tmp_path / mb.EXTRACTION_FILE).write_text(
        json.dumps({"callId": "lc_x", "jobId": "ext_y", "tokenBudget": 2_000_000}), encoding="utf-8")
    assert mb.budget_of(tmp_path) == 2_000_000
    (tmp_path / mb.EXTRACTION_FILE).write_text(json.dumps({"callId": "lc_x", "jobId": "ext_y"}),
                                               encoding="utf-8")
    assert mb.budget_of(tmp_path) == mb.DEFAULT_LIMIT, "a job file without a budget changes nothing"


def test_the_budget_and_the_extraction_scripts_share_one_file_name():
    assert mb.EXTRACTION_FILE == shim.EXTRACTION_FILE


# --- waiving the budget, with the person's approval --------------------------------

def _run_post_hook(hook, capsys):
    rc = mb.main(["agent-posttool"], stream=io.StringIO(json.dumps(hook)))
    return rc, capsys.readouterr().out.strip()


def _helper_decision(agent_dir, capsys, message=MSG):
    mb.main(["helper-pretool", "--agent-dir", str(agent_dir), "--message", message, "--run", "r1"],
            stream=io.StringIO(json.dumps({"tool_name": "mcp__soleon-agent-tools__search_emails"})))
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None


@pytest.mark.parametrize("mode", ["default", "bypassPermissions", "acceptEdits", None])
def test_at_the_wind_down_point_the_person_is_asked_to_waive_the_budget(tmp_path, capsys, mode):
    """Dan 2026-11-22: a local run may go past the budget, but only with a
    person's approval. Claude Code's own prompt asks; nothing else answers it
    (bypass mode, allow rules and --allowedTools were tested not to)."""
    agent_dir = _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 85000)
    rc, out = _run_agent_hook(_hook(tmp_path, tool_use_id="t1", mode=mode), capsys)
    decision = json.loads(out)["hookSpecificOutput"]
    assert rc == 0 and decision["permissionDecision"] == "ask"
    reason = decision["permissionDecisionReason"]
    assert "85,000 of its 100,000-token Per Message Token Budget" in reason
    assert "approve this call and the budget is off for the rest of this message" in reason
    assert "do not call any more tools" in reason and "answer now" in reason, \
        "a -p run denies the ask and the model reads this reason: it must say what to do"
    assert mb.Ledger(agent_dir)._load()["asked"] == ["t1"]


def test_with_no_one_to_ask_the_call_is_refused_outright(tmp_path, capsys):
    """dontAsk denies an ask without the reason, so it gets the plain refusal."""
    agent_dir = _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 85000)
    _, out = _run_agent_hook(_hook(tmp_path, tool_use_id="t1", mode="dontAsk"), capsys)
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"].startswith("per-message token budget exhausted")
    assert not mb.Ledger(agent_dir)._load().get("asked")


def test_an_approved_call_waives_the_budget_for_the_rest_of_the_message(tmp_path, capsys):
    """The asked call RAN (PostToolUse fires only for a call that ran), so the
    person approved it: the agent and its helpers go on with no budget."""
    agent_dir = _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 85000)
    _run_agent_hook(_hook(tmp_path, tool_use_id="t1", mode="default"), capsys)
    assert _helper_decision(agent_dir, capsys) == "deny", "before the approval a helper is held"
    rc, out = _run_post_hook(_hook(tmp_path, tool_use_id="t1", mode="default", event="PostToolUse"), capsys)
    assert rc == 0 and "off for the rest of this run" in json.loads(out)["systemMessage"]
    _agent_spend(tmp_path, 900000)
    assert _run_agent_hook(_hook(tmp_path, tool_use_id="t2", mode="default"), capsys) == (0, "")
    assert _helper_decision(agent_dir, capsys) is None


def test_a_refused_ask_waives_nothing_and_the_next_call_asks_again(tmp_path, capsys):
    agent_dir = _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 85000)
    _run_agent_hook(_hook(tmp_path, tool_use_id="t1", mode="default"), capsys)
    # refused: the call never ran, so no PostToolUse for t1
    _, out = _run_agent_hook(_hook(tmp_path, tool_use_id="t2", mode="default"), capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert not mb.Ledger(agent_dir)._load()["waived"]


def test_a_call_that_was_never_asked_about_waives_nothing(tmp_path, capsys):
    """Calls under the line run without a prompt; their PostToolUse is not an approval."""
    agent_dir = _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 1000)
    _run_agent_hook(_hook(tmp_path, tool_use_id="t1", mode="default"), capsys)
    assert _run_post_hook(_hook(tmp_path, tool_use_id="t1", event="PostToolUse"), capsys) == (0, "")
    assert not mb.Ledger(agent_dir)._load()["waived"]
    _agent_spend(tmp_path, 85000)
    _, out = _run_agent_hook(_hook(tmp_path, tool_use_id="t2", mode="default"), capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_a_waiver_ends_with_its_message(tmp_path, capsys):
    _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 85000)
    _run_agent_hook(_hook(tmp_path, tool_use_id="t1", mode="default"), capsys)
    _run_post_hook(_hook(tmp_path, tool_use_id="t1", event="PostToolUse"), capsys)
    _, out = _run_agent_hook(_hook(tmp_path, tool_use_id="t2", prompt_id="p2", mode="default"), capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_another_messages_approval_leaves_the_ledger_alone(tmp_path):
    agent_dir = _pulled(tmp_path, budget=100000)
    ledger = mb.Ledger(agent_dir)
    ledger.note_helper(MSG, "r1", 70000)
    ledger.note_ask(MSG, "t1")
    assert ledger.confirm_ask("prompt:other:a9", "t1") is False
    state = ledger._load()
    assert state["message"] == MSG and state["helpers"] == {"r1": 70000} and not state["waived"]


def test_the_plugin_runs_the_approval_hook_after_every_tool_call():
    hooks = json.loads((BIN.parent / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    post = [h["command"] for e in hooks["PostToolUse"] if not e.get("matcher") for h in e["hooks"]]
    assert any('soleon_message_budget.py" agent-posttool' in c for c in post)
    pre = [h["command"] for e in hooks["PreToolUse"] if not e.get("matcher") for h in e["hooks"]]
    assert any('soleon_message_budget.py" agent-pretool' in c for c in pre)


def _result_in_transcript(tmp_path, tool_use_id, is_error, agent_id="a1"):
    path = tmp_path / "sessions" / "s1" / "subagents" / "agent-{}.jsonl".format(agent_id)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error,
             "content": "the person refused" if is_error else "(Bash completed with no output)"}]}}) + "\n")


def test_an_approval_is_seen_in_the_transcript_without_the_posttool_hook(tmp_path, capsys):
    """Claude Code reads a plugin's hook list only when a conversation starts:
    a conversation begun before agent-posttool existed never runs it. The asked
    call's SUCCESSFUL result in the agent's transcript is the approval too."""
    agent_dir = _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 85000)
    _run_agent_hook(_hook(tmp_path, tool_use_id="t1", mode="default"), capsys)
    _result_in_transcript(tmp_path, "t1", is_error=False)
    assert _run_agent_hook(_hook(tmp_path, tool_use_id="t2", mode="default"), capsys) == (0, "")
    assert mb.Ledger(agent_dir)._load()["waived"] is True
    assert _helper_decision(agent_dir, capsys) is None


def test_a_refusal_in_the_transcript_waives_nothing(tmp_path, capsys):
    agent_dir = _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 85000)
    _run_agent_hook(_hook(tmp_path, tool_use_id="t1", mode="default"), capsys)
    _result_in_transcript(tmp_path, "t1", is_error=True)
    _, out = _run_agent_hook(_hook(tmp_path, tool_use_id="t2", mode="default"), capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert not mb.Ledger(agent_dir)._load()["waived"]


def test_a_successful_call_that_was_never_asked_about_waives_nothing(tmp_path, capsys):
    agent_dir = _pulled(tmp_path, budget=100000)
    _agent_spend(tmp_path, 85000)
    _result_in_transcript(tmp_path, "t0", is_error=False)
    transcript = mb.subagent_transcript(str(tmp_path / "sessions" / "s1.jsonl"), "a1")
    assert mb.approved_in_transcripts([transcript], ["t1"]) is None
    assert mb.approved_in_transcripts([transcript], ["t0"]) == "t0"
    _run_agent_hook(_hook(tmp_path, tool_use_id="t1", mode="default"), capsys)
    _, out = _run_agent_hook(_hook(tmp_path, tool_use_id="t2", mode="default"), capsys)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert not mb.Ledger(agent_dir)._load()["waived"]
