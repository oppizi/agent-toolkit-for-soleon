"""bin/soleon_turn_hooks.py — the SubagentStart / SubagentStop hooks.

One run of a pulled agent's subagent = ONE turn on the platform trace:
`start` mints the turn id the tool server adopts; `stop` turns the subagent's
transcript into `record_agent_turn` (prompt, answer, the locally-run tool
calls as steps, every tool name, timing). Other subagents → exit 0, no
network, no files."""
from __future__ import annotations

import io
import json
import re
import sys
import time
from pathlib import Path

from _local_emulation_fixtures import BIN, SERVER_URL, SLUG, make_pulled_dir

sys.path.insert(0, str(BIN))
import soleon_turn_hooks as hooks  # noqa: E402


class FakeClient:
    instances: list = []

    def __init__(self, server_url):
        self.server_url = server_url
        self.calls = []
        self._sleep = lambda seconds: None  # await_control's pacing seam
        self.answer = {"status": 200, "state": "done", "call_id": "lc_" + "d" * 24, "op": "turn_record",
                       "result": {"turn": "x", "stepsWritten": 1, "recorded": True}}
        FakeClient.instances.append(self)

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if isinstance(self.answer, Exception):
            raise self.answer
        return json.loads(json.dumps(self.answer))


# A subagent transcript shaped exactly like Claude Code 2.1.x writes it
# (verified 2026-09-19 against a real run): a str-content user prompt, then
# assistant thinking / tool_use records and user tool_result records.
def _rec(role, content, ts, **extra):
    return {"type": role, "message": {"role": role, "content": content}, "timestamp": ts,
            "agentId": "ae6c9a15e98175395", **extra}


def transcript_lines() -> list:
    big = "x" * 10_000
    return [
        _rec("user", 'The user says: "Scan my email for the most recent newsletter."\n\nReport back.',
             "2026-09-19T16:50:58.375Z"),
        {"type": "attachment", "timestamp": "2026-09-19T16:50:58.473Z"},
        _rec("assistant", [{"type": "thinking", "thinking": "..."}], "2026-09-19T16:51:01.060Z"),
        _rec("assistant", [{"type": "tool_use", "id": "toolu_1", "name": "mcp__soleon-agent-tools__mcp_gmail_search_emails",
                            "input": {"query": "newsletter"}}], "2026-09-19T16:51:01.755Z"),
        _rec("user", [{"type": "tool_result", "tool_use_id": "toolu_1",
                       "content": [{"type": "text", "text": "3 emails"}]}], "2026-09-19T16:51:19.515Z"),
        _rec("assistant", [{"type": "tool_use", "id": "toolu_2", "name": "mcp__soleon-workspace__read_file",
                            "input": {"path": "memory/MEMORY.md"}}], "2026-09-19T16:51:32.410Z"),
        _rec("user", [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "# notes\n" + big}],
             "2026-09-19T16:51:32.538Z"),
        _rec("assistant", [{"type": "tool_use", "id": "toolu_3", "name": "mcp__soleon-workspace__write_file",
                            "input": {"path": "out.md", "content": big}}], "2026-09-19T16:51:35.461Z"),
        _rec("user", [{"type": "tool_result", "tool_use_id": "toolu_3", "content": "permission denied",
                       "is_error": True}], "2026-09-19T16:51:35.482Z"),
        _rec("assistant", [{"type": "text", "text": "I found the TechCrunch newsletter. Summary: ..."}],
             "2026-09-19T16:51:51.844Z"),
    ]


def _write_transcript(tmp_path: Path) -> Path:
    p = tmp_path / "agent-ae6c9a15e98175395.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in transcript_lines()) + "\n", encoding="utf-8")
    return p


def _pulled(tmp_path: Path) -> Path:
    """A pulled agent dir as the skill leaves it: the raw responses PLUS the
    pull.json that materialize writes (the hooks key off it)."""
    agent_dir = make_pulled_dir(tmp_path)
    (agent_dir / "pull.json").write_text(json.dumps(
        {"slug": SLUG, "serverUrl": SERVER_URL, "source": "draft", "draftEtag": "1726560000000"}), encoding="utf-8")
    return agent_dir


def _hook(agent_dir: Path, **extra) -> dict:
    base = {"session_id": "s1", "cwd": str(agent_dir.parents[2]), "hook_event_name": "SubagentStart",
            "agent_id": "ae6c9a15e98175395", "agent_type": SLUG}
    base.update(extra)
    return base


def test_turn_from_transcript_extracts_prompt_answer_steps_and_tools(tmp_path):
    turn = hooks.turn_from_transcript(hooks.load_transcript(str(_write_transcript(tmp_path))),
                                      "I found the TechCrunch newsletter. Summary: ...")
    assert turn["prompt"].startswith('The user says: "Scan my email')
    assert turn["response"] == "I found the TechCrunch newsletter. Summary: ..."
    assert turn["status"] == "completed"
    assert turn["started_at"] == "2026-09-19T16:50:58.375000+00:00"
    assert turn["completed_at"] == "2026-09-19T16:51:51.844000+00:00"
    # every tool the run called, platform + local, by its bare name
    assert turn["tools_used"] == ["mcp_gmail_search_emails", "read_file", "write_file"]
    # only the LOCALLY-run calls become steps (the platform one already is one)
    assert [s["name"] for s in turn["steps"]] == ["read_file", "write_file"]
    read, write = turn["steps"]
    assert read["args"] == {"path": "memory/MEMORY.md"} and read["status"] == "success"
    assert read["result"].startswith("# notes") and len(read["result"]) == hooks.STEP_RESULT_MAX
    assert read["started_at"] == "2026-09-19T16:51:32.410000+00:00"
    assert read["completed_at"] == "2026-09-19T16:51:32.538000+00:00"
    assert write["status"] == "error" and write["error"] == "permission denied"
    assert "_truncated" in write["args"] and len(write["args"]["_truncated"]) == hooks.STEP_ARGS_MAX


def test_turn_from_transcript_without_an_answer_is_failed_and_unanswered_calls_are_errors(tmp_path):
    lines = transcript_lines()[:6]  # cut after the read_file tool_use, before its result
    turn = hooks.turn_from_transcript([r for r in lines if "message" in r], None)
    assert turn["status"] == "failed" and turn["response"] == ""
    assert turn["steps"][0]["name"] == "read_file" and turn["steps"][0]["status"] == "error"
    assert "no result recorded" in turn["steps"][0]["error"]


def test_start_writes_the_turn_file_only_for_a_pulled_agent(tmp_path):
    agent_dir = _pulled(tmp_path)
    assert hooks.main(["start"], stream=io.StringIO(json.dumps(_hook(agent_dir)))) == 0
    data = json.loads((agent_dir / ".local-turn.json").read_text())
    assert re.fullmatch(r"[0-9a-f]{12}", data["turn"]) and data["agentId"] == "ae6c9a15e98175395"
    assert data["source"] == "hook" and abs(data["startedAt"] - time.time()) < 5
    # another subagent in the same project: nothing written anywhere
    assert hooks.main(["start"], stream=io.StringIO(json.dumps(_hook(agent_dir, agent_type="code-reviewer")))) == 0
    assert not list(tmp_path.rglob("code-reviewer")) and json.loads((agent_dir / ".local-turn.json").read_text()) == data
    assert hooks.main(["start"], stream=io.StringIO("")) == 0
    assert hooks.main(["bogus"], stream=io.StringIO("{}")) == 2


def test_stop_records_the_turn_under_the_hooks_turn_id_and_conversation(tmp_path, capsys):
    agent_dir = _pulled(tmp_path)
    (agent_dir / ".local-turn.json").write_text(json.dumps(
        {"turn": "hookturn0001", "agentId": "ae6c9a15e98175395", "startedAt": time.time() - 30, "source": "hook"}))
    (agent_dir / ".local-conversation.json").write_text(json.dumps({"conversation": "c" * 24, "lastCallAt": time.time() - 20}))
    transcript = _write_transcript(tmp_path)
    FakeClient.instances.clear()
    hook = _hook(agent_dir, hook_event_name="SubagentStop", agent_transcript_path=str(transcript),
                 last_assistant_message="I found the TechCrunch newsletter. Summary: ...")
    assert hooks.main(["stop"], stream=io.StringIO(json.dumps(hook)), client_factory=FakeClient) == 0
    (client,) = FakeClient.instances
    assert client.server_url == SERVER_URL  # pull.json's serverUrl
    (name, args), = client.calls
    assert name == "record_agent_turn"
    assert args["slug"] == SLUG and args["app_env"] == "dev"
    assert args["turn"] == "hookturn0001" and args["conversation"] == "c" * 24  # the ids the tool calls used
    assert args["prompt"].startswith("The user says") and args["response"].startswith("I found")
    assert args["status"] == "completed" and args["started_at"] == "2026-09-19T16:50:58.375000+00:00"
    assert [s["name"] for s in args["steps"]] == ["read_file", "write_file"]
    assert args["tools_used"] == ["mcp_gmail_search_emails", "read_file", "write_file"]
    assert not (agent_dir / ".local-turn.json").exists()  # consumed
    assert capsys.readouterr().out == ""  # success is silent


def test_stop_mints_a_turn_when_no_tool_call_left_one(tmp_path):
    agent_dir = _pulled(tmp_path)
    transcript = _write_transcript(tmp_path)
    FakeClient.instances.clear()
    hook = _hook(agent_dir, hook_event_name="SubagentStop", agent_transcript_path=str(transcript))
    assert hooks.main(["stop"], stream=io.StringIO(json.dumps(hook)), client_factory=FakeClient) == 0
    (name, args), = FakeClient.instances[0].calls
    assert re.fullmatch(r"[0-9a-f]{12}", args["turn"]) and re.fullmatch(r"[0-9a-f]{24}", args["conversation"])
    # a stale file from an EARLIER run is not this turn's
    (agent_dir / ".local-turn.json").write_text(json.dumps(
        {"turn": "oldturn00001", "agentId": "other", "startedAt": time.time() - 3600, "source": "hook"}))
    FakeClient.instances.clear()
    assert hooks.main(["stop"], stream=io.StringIO(json.dumps(hook)), client_factory=FakeClient) == 0
    assert FakeClient.instances[0].calls[0][1]["turn"] != "oldturn00001"


def test_stop_failure_tells_the_user_and_never_blocks(tmp_path, capsys):
    agent_dir = _pulled(tmp_path)
    transcript = _write_transcript(tmp_path)

    class Failing(FakeClient):
        def __init__(self, server_url):
            super().__init__(server_url)
            self.answer = {"status": 409, "error": "dev_only", "detail": "drafts exist on dev only",
                           "next_action": "use app_env dev"}

    hook = _hook(agent_dir, hook_event_name="SubagentStop", agent_transcript_path=str(transcript))
    assert hooks.main(["stop"], stream=io.StringIO(json.dumps(hook)), client_factory=Failing) == 0
    out = capsys.readouterr()
    assert "NOT recorded" in json.loads(out.out)["systemMessage"] and "NOT recorded" in out.err
    # no transcript path → nothing sent, exit 0
    FakeClient.instances.clear()
    hook.pop("agent_transcript_path")
    assert hooks.main(["stop"], stream=io.StringIO(json.dumps(hook)), client_factory=FakeClient) == 0
    assert FakeClient.instances == []
    # not a pulled agent → nothing at all
    hook["agent_type"] = "code-reviewer"
    assert hooks.main(["stop"], stream=io.StringIO(json.dumps(hook)), client_factory=FakeClient) == 0
    assert FakeClient.instances == []
