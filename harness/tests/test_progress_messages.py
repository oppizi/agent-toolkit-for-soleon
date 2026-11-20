"""bin/soleon_progress.py — progress messages and delivered files for a pulled
agent that runs locally.

On Soleon an agent with Progress messages on sends the person kept messages
while it works, decided by the agent's decision model and written by the
agent's own model, and a file the agent handed over goes with a message when
the decision picks it. Locally the same decision is asked through
`check_agent_progress` after each tool call; the message is shown in the
terminal and its files are saved under `delivered/`. Files must work: an
`attach_file` on a file in the LOCAL workspace is a real handover, and the
end of the turn delivers every file handed over."""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import pytest

from _local_emulation_fixtures import BIN, PLUGIN, SERVER_URL, SLUG, make_pulled_dir

sys.path.insert(0, str(BIN))
import soleon_agent_tools_mcp as tools_mcp  # noqa: E402
import soleon_progress as progress  # noqa: E402
import soleon_turn_hooks as hooks  # noqa: E402

AGENT_ID = "ae6c9a15e98175395"
TURN = "hookturn0001"
CALL = "lc_" + "a" * 24
MARKER = "[ATTACHMENT:appenv/dev/demo-agent/soleon_draft_x/_outbound/0123456789abcdef/deck.pptx]"


def _pulled(tmp_path: Path, frequency="verbose") -> Path:
    agent_dir = make_pulled_dir(tmp_path)
    (agent_dir / "pull.json").write_text(json.dumps(
        {"slug": SLUG, "serverUrl": SERVER_URL, "displayName": "Demo Agent"}), encoding="utf-8")
    (agent_dir / "SOUL.md").write_text("You are Demo.", encoding="utf-8")
    loop = {"progressMessages": {"frequency": frequency, "guidance": "after each file"}} if frequency else {}
    (agent_dir / "config.json").write_text(json.dumps({"loop": loop}), encoding="utf-8")
    (agent_dir / "workspace").mkdir(exist_ok=True)
    (agent_dir / ".local-turn.json").write_text(json.dumps(
        {"turn": TURN, "agentId": AGENT_ID, "startedAt": time.time() - 30, "source": "hook"}), encoding="utf-8")
    (agent_dir / ".local-conversation.json").write_text(json.dumps(
        {"conversation": "c" * 24, "lastCallAt": time.time() - 5}), encoding="utf-8")
    return agent_dir


def _rec(role, content, ts="2026-11-19T20:00:00.000Z", **extra):
    return {"type": role, "message": {"role": role, "content": content}, "timestamp": ts, "agentId": AGENT_ID, **extra}


def _transcript(tmp_path: Path, records) -> str:
    """The session transcript path whose `subagents/agent-<id>.jsonl` holds the run."""
    session = tmp_path / "session.jsonl"
    session.write_text("", encoding="utf-8")
    sub = tmp_path / "session" / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "agent-{}.jsonl".format(AGENT_ID)).write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(session)


RUN = [
    _rec("user", "Build the report and send me the draft."),
    _rec("assistant", [{"type": "text", "text": "I will write the draft first."},
                       {"type": "tool_use", "id": "toolu_1", "name": "mcp__soleon-workspace__write_file",
                        "input": {"path": "draft.csv", "content": "a,b"}}], uuid="u1"),
    _rec("user", [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "wrote draft.csv"}]),
    _rec("assistant", [{"type": "tool_use", "id": "toolu_2", "name": "mcp__soleon-agent-tools__web_search",
                        "input": {"query": "q"}}], uuid="u2"),
]


def _hook(agent_dir: Path, transcript: str, **extra) -> dict:
    base = {"session_id": "s1", "cwd": str(agent_dir.parents[2]), "hook_event_name": "PostToolUse",
            "agent_id": AGENT_ID, "agent_type": SLUG, "transcript_path": transcript,
            "tool_name": "mcp__soleon-agent-tools__web_search", "tool_use_id": "toolu_2",
            "tool_input": {"query": "q"}, "tool_response": {"content": [{"type": "text", "text": "3 results"}]}}
    base.update(extra)
    return base


class FakeClient:
    """Scripted answers per tool name; records every call."""

    def __init__(self, answers):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls = []
        self._sleep = lambda seconds: None  # await_control's pacing seam

    def __call__(self, server_url):
        self.server_url = server_url
        return self

    def call_tool(self, name, arguments):
        self.calls.append((name, json.loads(json.dumps(arguments))))
        return self.answers[name].pop(0)


PENDING = {"status": 202, "state": "pending", "call_id": CALL, "op": "progress_check"}


def _done(**result):
    return {"status": 200, "state": "done", "call_id": CALL, "op": "progress_check", "result": result}


# ---------------------------------------------------------------------------
# post-tool
# ---------------------------------------------------------------------------

def test_a_tool_call_sends_a_check_with_what_happened_and_does_not_wait(tmp_path):
    agent_dir = _pulled(tmp_path)
    client = FakeClient({"check_agent_progress": [PENDING]})
    out = progress.post_tool(_hook(agent_dir, _transcript(tmp_path, RUN)), client_factory=client, now=lambda: 1000.0)
    assert out is None                                    # nothing to show yet, and nothing was waited for
    (name, args), = client.calls
    assert name == "check_agent_progress" and client.server_url == SERVER_URL
    assert args["slug"] == SLUG and args["turn"] == TURN and args["conversation"] == "c" * 24
    assert args["wait"] is False and args["app_env"] == "dev"
    assert args["prompt"] == "Build the report and send me the draft." and args["identity"] == "You are Demo."
    assert [(s["kind"], s["name"], s["result"]) for s in args["steps"]] == [
        ("llm", "", "I will write the draft first."),
        ("tool", "write_file", "wrote draft.csv"),
        ("tool", "web_search", "3 results"),              # the call the hook fired for, not yet in the transcript
    ]
    assert args["steps_so_far"] == 3 and args["since_last_s"] == -1 and args["sent"] == []
    state = json.loads((agent_dir / progress.STATE_FILE).read_text())
    assert state["pending"] == {"callId": CALL, "at": 1000.0} and "toolu_2" in state["covered"]


def test_the_next_tool_call_shows_the_message_saves_its_files_and_tells_the_agent(tmp_path):
    agent_dir = _pulled(tmp_path)
    (agent_dir / "workspace" / "draft.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    handled, text = progress.stage_local_file(agent_dir, TURN, "draft.csv")
    assert handled and text.startswith("Delivered draft.csv to the user")
    state = progress.load_state(agent_dir, TURN)
    state["pending"] = {"callId": CALL, "at": 990.0}
    state["covered"] = ["say:u1", "toolu_1", "toolu_2"]
    progress.save_state(agent_dir, state)
    downloads = []

    def opener(url):
        downloads.append(url)
        return io.BytesIO(b"deck-bytes")

    client = FakeClient({"get_agent_tool_result": [_done(
        enabled=True, send=True, text="The draft is written.",
        files=[{"name": "draft.csv", "source": "local", "id": "local_1"},
               {"name": "deck.pptx", "source": "platform", "marker": MARKER, "url": "https://signed/deck"}])]})
    out = progress.post_tool(_hook(agent_dir, _transcript(tmp_path, RUN)), client_factory=client,
                             now=lambda: 1000.0, opener=opener)
    delivered = agent_dir / "delivered"
    assert (delivered / "draft.csv").read_text() == "a,b\n1,2\n"
    assert (delivered / "deck.pptx").read_bytes() == b"deck-bytes" and downloads == ["https://signed/deck"]
    assert out["systemMessage"].splitlines() == [
        "Progress message from Demo Agent: The draft is written.",
        "  File: {}".format(delivered / "draft.csv"),
        "  File: {}".format(delivered / "deck.pptx"),
    ]
    told = out["hookSpecificOutput"]
    assert told["hookEventName"] == "PostToolUse"
    assert "The draft is written." in told["additionalContext"] and "Do not repeat it" in told["additionalContext"]
    assert client.calls == [("get_agent_tool_result", {"slug": SLUG, "app_env": "dev", "call_id": CALL})]
    state = json.loads((agent_dir / progress.STATE_FILE).read_text())
    assert state["sent"] == ["The draft is written."] and state["lastAt"] == 1000.0 and state["pending"] is None
    assert state["sentFiles"] == ["draft.csv", MARKER]     # never offered early again


def test_a_later_check_reports_what_was_sent_and_only_the_new_steps(tmp_path):
    agent_dir = _pulled(tmp_path)
    state = progress.load_state(agent_dir, TURN)
    state.update(covered=["say:u1", "toolu_1"], stepsSoFar=2, sent=["The draft is written."], lastAt=940.0,
                 sentFiles=["draft.csv"], handed=[{"id": "local_1", "name": "draft.csv", "path": "x"}])
    progress.save_state(agent_dir, state)
    client = FakeClient({"check_agent_progress": [_done(enabled=True, send=False, files=[])]})
    out = progress.post_tool(_hook(agent_dir, _transcript(tmp_path, RUN)), client_factory=client, now=lambda: 1000.0)
    assert out is None
    args = client.calls[0][1]
    assert [s["name"] for s in args["steps"]] == ["web_search"] and args["steps_so_far"] == 3
    assert args["sent"] == ["The draft is written."] and args["since_last_s"] == 60.0
    assert args["files"] == [{"id": "local_1", "name": "draft.csv"}] and args["sent_files"] == ["draft.csv"]


def test_a_check_still_being_decided_is_not_asked_again_and_a_fresh_one_is_not_read(tmp_path):
    agent_dir = _pulled(tmp_path)
    state = progress.load_state(agent_dir, TURN)
    state["pending"] = {"callId": CALL, "at": 999.5}
    progress.save_state(agent_dir, state)
    client = FakeClient({})
    hook = _hook(agent_dir, _transcript(tmp_path, RUN))
    assert progress.post_tool(hook, client_factory=client, now=lambda: 1000.0) is None and client.calls == []
    client = FakeClient({"get_agent_tool_result": [PENDING]})
    assert progress.post_tool(hook, client_factory=client, now=lambda: 1010.0) is None
    assert [c[0] for c in client.calls] == ["get_agent_tool_result"]          # no second check in flight


@pytest.mark.parametrize("change", ["off", "other_agent", "helper", "main_thread", "stale_turn"])
def test_nothing_runs_outside_a_pulled_agents_own_run_with_the_setting_on(tmp_path, change):
    agent_dir = _pulled(tmp_path, frequency=None if change == "off" else "verbose")
    hook = _hook(agent_dir, _transcript(tmp_path, RUN))
    if change == "other_agent":
        hook["agent_type"] = "some-other-agent"
    elif change == "helper":
        hook["agent_type"] = SLUG + "--subagent_res1"
    elif change == "main_thread":
        hook.pop("agent_id")
    elif change == "stale_turn":
        (agent_dir / ".local-turn.json").write_text(json.dumps({"turn": TURN, "agentId": "another-run"}))
    client = FakeClient({})
    assert progress.post_tool(hook, client_factory=client) is None
    assert client.calls == [] and not (agent_dir / progress.STATE_FILE).exists()


def test_a_platform_failure_never_fails_the_agents_tool_call(tmp_path, capsys):
    from soleon_mcp_client import TransportError
    agent_dir = _pulled(tmp_path)

    class Down(FakeClient):
        def call_tool(self, name, arguments):
            raise TransportError("unreachable")

    assert progress.post_tool(_hook(agent_dir, _transcript(tmp_path, RUN)), client_factory=Down({})) is None
    assert "progress check failed" in capsys.readouterr().err
    assert progress.main(["post-tool"], stream=io.StringIO("not json")) == 0


# ---------------------------------------------------------------------------
# attach_file on a local file
# ---------------------------------------------------------------------------

def test_attach_file_keeps_the_file_as_it_is_now(tmp_path):
    """The handed-over version is the one delivered, even when the agent
    changes the file afterwards (a later attach is a newer version)."""
    agent_dir = _pulled(tmp_path)
    source = agent_dir / "workspace" / "out" / "draft.csv"
    source.parent.mkdir()
    source.write_text("v1", encoding="utf-8")
    assert progress.stage_local_file(agent_dir, TURN, "out/draft.csv")[0] is True
    source.write_text("v2", encoding="utf-8")
    state = progress.load_state(agent_dir, TURN)
    (entry,) = state["handed"]
    assert entry["id"] == "local_1" and entry["name"] == "draft.csv" and Path(entry["path"]).read_text() == "v1"
    assert progress.deliver(agent_dir, state, [{"source": "local", "id": "local_1", "name": "draft.csv"}]) == [
        str(agent_dir / "delivered" / "draft.csv")]
    assert (agent_dir / "delivered" / "draft.csv").read_text() == "v1"


def test_attach_file_leaves_a_file_that_is_not_local_to_the_platform(tmp_path):
    agent_dir = _pulled(tmp_path)
    assert progress.stage_local_file(agent_dir, TURN, "made_by_a_platform_tool.pptx") == (False, "")
    handled, text = progress.stage_local_file(agent_dir, TURN, "../SOUL.md")
    assert handled and text.startswith("Error:") and "outside the workspace" in text
    assert progress.load_state(agent_dir, TURN)["handed"] == []


def test_the_tool_server_hands_a_local_file_over_without_calling_the_platform(tmp_path):
    agent_dir = _pulled(tmp_path)
    (agent_dir / "workspace" / "draft.csv").write_text("a,b", encoding="utf-8")
    calls = []

    class Client:
        server_url = SERVER_URL
        _sleep = staticmethod(lambda seconds: None)

        def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return {"status": 200, "state": "done", "call_id": CALL, "op": "tool_call",
                    "result": {"tool": "attach_file", "result": "Delivered deck.pptx to the user as a downloadable attachment."}}

    tool = {"name": "attach_file", "kind": "external", "description": "attach",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}}
    server = tools_mcp.AgentToolsServer(SLUG, [tool], Client(), turn=tools_mcp.TurnTracker(None, fixed=TURN),
                                        agent_dir=str(agent_dir))
    out = server.call("attach_file", {"path": "draft.csv"})
    assert "Delivered draft.csv to the user" in out["content"][0]["text"] and calls == []
    assert [h["name"] for h in progress.load_state(agent_dir, TURN)["handed"]] == ["draft.csv"]
    # A file the local workspace does not have is the platform's to attach (a platform tool made it).
    out = server.call("attach_file", {"path": "deck.pptx"})
    assert "Delivered deck.pptx" in out["content"][0]["text"]
    assert [c[0] for c in calls] == ["call_agent_tool"] and calls[0][1]["name"] == "attach_file"


# ---------------------------------------------------------------------------
# The end of the turn
# ---------------------------------------------------------------------------

def test_the_end_of_the_turn_delivers_every_file_and_shows_a_message_still_in_flight(tmp_path):
    agent_dir = _pulled(tmp_path)
    (agent_dir / "workspace" / "draft.csv").write_text("v1", encoding="utf-8")
    progress.stage_local_file(agent_dir, TURN, "draft.csv")
    (agent_dir / "workspace" / "draft.csv").write_text("v2", encoding="utf-8")
    progress.stage_local_file(agent_dir, TURN, "draft.csv")          # a newer version of the same name
    state = progress.load_state(agent_dir, TURN)
    state["pending"] = {"callId": CALL, "at": 990.0}
    state["sentFiles"] = ["draft.csv"]                                # an earlier message carried v1
    progress.save_state(agent_dir, state)
    client = FakeClient({
        "get_agent_tool_result": [PENDING, _done(enabled=True, send=True, text="Almost done.", files=[])],
        "check_agent_progress": [_done(enabled=True, final=True, files=[
            {"name": "deck.pptx", "source": "platform", "marker": MARKER, "url": "https://signed/deck"}])],
    })
    lines = progress.finish(agent_dir, TURN, "c" * 24, SLUG, SERVER_URL, platform_tools_used=True,
                            client_factory=client, now=lambda: 1000.0, sleep=lambda s: None,
                            opener=lambda url: io.BytesIO(b"deck"))
    delivered = agent_dir / "delivered"
    assert lines == [
        "Progress message from Demo Agent: Almost done.",
        "Files delivered with Demo Agent's answer:\n  File: {}\n  File: {}".format(
            delivered / "draft.csv", delivered / "deck.pptx"),
    ]
    # The answer carries the newest version, although a progress message carried an older one.
    assert (delivered / "draft.csv").read_text() == "v2" and (delivered / "deck.pptx").read_bytes() == b"deck"
    final = [a for n, a in client.calls if n == "check_agent_progress"]
    assert final == [{"slug": SLUG, "app_env": "dev", "conversation": "c" * 24, "turn": TURN, "final": True}]
    assert not (agent_dir / progress.STATE_FILE).exists() and not (agent_dir / progress.OUTBOX_DIR / TURN).exists()


def test_the_end_of_the_turn_delivers_local_files_with_the_setting_off_and_asks_nothing_without_platform_tools(tmp_path):
    agent_dir = _pulled(tmp_path, frequency=None)
    (agent_dir / "workspace" / "notes.txt").write_text("n", encoding="utf-8")
    progress.stage_local_file(agent_dir, TURN, "notes.txt")
    client = FakeClient({})
    lines = progress.finish(agent_dir, TURN, "c" * 24, SLUG, SERVER_URL, platform_tools_used=False,
                            client_factory=client)
    assert client.calls == [] and (agent_dir / "delivered" / "notes.txt").read_text() == "n"
    assert lines[0].startswith("Files delivered with Demo Agent's answer:")
    assert progress.finish(agent_dir, TURN, "c" * 24, SLUG, SERVER_URL, platform_tools_used=False,
                           client_factory=client) == []


def test_the_stop_hook_shows_the_files_and_still_records_the_turn(tmp_path, capsys):
    agent_dir = _pulled(tmp_path)
    (agent_dir / "workspace" / "draft.csv").write_text("a,b", encoding="utf-8")
    progress.stage_local_file(agent_dir, TURN, "draft.csv")
    sub = tmp_path / "agent.jsonl"
    sub.write_text("\n".join(json.dumps(r) for r in RUN[:3] + [_rec("assistant", "Here is the draft.")]) + "\n")
    record = {"status": 200, "state": "done", "call_id": CALL, "op": "turn_record", "result": {"recorded": True}}
    client = FakeClient({"record_agent_turn": [record]})
    hook = {"session_id": "s1", "cwd": str(agent_dir.parents[2]), "hook_event_name": "SubagentStop",
            "agent_id": AGENT_ID, "agent_type": SLUG, "agent_transcript_path": str(sub),
            "last_assistant_message": "Here is the draft."}
    assert hooks.main(["stop"], stream=io.StringIO(json.dumps(hook)), client_factory=client) == 0
    assert [c[0] for c in client.calls] == ["record_agent_turn"] and client.calls[0][1]["turn"] == TURN
    shown = json.loads(capsys.readouterr().out)["systemMessage"]
    assert shown.splitlines() == ["Files delivered with Demo Agent's answer:",
                                  "  File: {}".format(agent_dir / "delivered" / "draft.csv")]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_hooks_json_runs_the_check_after_every_tool_call_of_a_subagent_only():
    entries = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    (entry,) = [e for e in entries if any("soleon_progress.py" in h["command"] for h in e["hooks"])]
    assert "matcher" not in entry
    (cmd,) = entry["hooks"]
    assert cmd["command"].startswith('i=$(cat); case "$i" in *\'"agent_id"\'*) ;; *) exit 0 ;; esac; ')
    assert cmd["command"].endswith('soleon_progress.py" post-tool') and cmd["timeout"] >= 30
