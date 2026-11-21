"""A LOCAL knowledge-base build (2026-11-17): Soleon runs the ingestion and
hands every model call that decides a source's pages to this machine.

Covers bin/soleon_kb_local_ingest.py (poll, answer, report; pointing the pulled
kb-extractor at an extraction job), the several-tool answers the distill's
lookup loop needs (bin/soleon_local_model.py), and the extraction-job file the
agent-tools server and the turn hook read."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from _local_emulation_fixtures import BIN

sys.path.insert(0, str(BIN))
import soleon_agent_tools_mcp as shim  # noqa: E402
import soleon_kb_local_ingest as kli  # noqa: E402
import soleon_local_model as lm  # noqa: E402

CALL = "lc_" + "1" * 24
JOB = "ext_" + "2" * 32 + "_"


# --- several tools ---------------------------------------------------------

def _fn(name, schema=None):
    return {"type": "function", "function": {"name": name, "description": name + " it",
                                             "parameters": schema or {"type": "object"}}}


LOOKUP = {"messages": [
    {"role": "system", "content": "You distill."},
    {"role": "user", "content": "the source"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function",
        "function": {"name": "search_wiki", "arguments": "{\"query\": \"fees\"}"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "found: concepts/fees.md"},
], "tools": [_fn("emit_distill_plan"), _fn("search_wiki"), _fn("read_page")], "tool_choice": "required"}


def test_several_tools_ask_for_one_named_choice():
    cmd = lm.command("claude", "sonnet", LOOKUP)
    flags = dict(zip(cmd[1:], cmd[2:]))
    schema = json.loads(flags["--json-schema"])
    assert schema["properties"]["tool"]["enum"] == ["emit_distill_plan", "search_wiki", "read_page"]
    assert "emit_distill_plan: emit_distill_plan it" in flags["--system-prompt"]
    assert flags["--system-prompt"].startswith("You distill.")
    prompt = lm.prompt_of(LOOKUP)
    assert "(called search_wiki with {\"query\": \"fees\"}, call id t1)" in prompt
    assert "[result of tool call t1]\nfound: concepts/fees.md" in prompt
    assert prompt.endswith("Continue from the last tool result.")


def test_a_named_tool_choice_offers_only_that_tool():
    body = {**LOOKUP, "tool_choice": {"type": "function", "function": {"name": "emit_distill_plan"}}}
    schema, form, name = lm._schema(body)
    assert (form, name) == ("tool", "emit_distill_plan")


def test_a_choice_answer_becomes_that_tools_call():
    out = lm.completion(LOOKUP, {"structured_output": {"tool": "read_page",
                                                        "arguments": {"path": "concepts/fees.md"}}})
    call, = out["choices"][0]["message"]["tool_calls"]
    assert call["function"]["name"] == "read_page"
    assert json.loads(call["function"]["arguments"]) == {"path": "concepts/fees.md"}
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_auto_choice_may_reply_in_words():
    body = {**LOOKUP, "tool_choice": "auto"}
    schema, form, _ = lm._schema(body)
    assert lm.REPLY_CHOICE in schema["properties"]["tool"]["enum"]
    out = lm.completion(body, {"structured_output": {"tool": lm.REPLY_CHOICE, "arguments": {"text": "{}"}}})
    assert out["choices"][0]["message"]["content"] == "{}"


# --- the run loop --------------------------------------------------------

class FakeClient:
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def call_tool(self, name, args):
        self.calls.append((name, json.loads(json.dumps(args))))
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        # local_kb_ingest is a forward tool: the wire answers {status, body}
        # (a fake that returned the bare body hid exactly that on 2026-11-17).
        return {"status": 200, "body": state} if name == "local_kb_ingest" else state


def _state(*, requests=(), sources=(), jobs=(), done=False):
    return {"call_id": CALL, "done": done, "model_requests": list(requests),
            "sources": list(sources), "extraction_jobs": list(jobs)}


def test_a_run_answers_every_waiting_request_and_stops_when_done():
    reqs = [{"request_id": "%032x" % i, "model": "m", "body": {"messages": []}} for i in range(6)]
    client = FakeClient([
        _state(requests=reqs, sources=[{"sourceId": "s1", "status": "ingesting", "title": "Spain"}]),
        _state(), _state(),
        _state(sources=[{"sourceId": "s1", "status": "ready", "title": "Spain", "pagesContributed": 9}],
               done=True),
    ])
    lines, answered = [], []

    def answer(requests):
        answered.extend(r["request_id"] for r in requests)
        return [{"request_id": r["request_id"], "completion": {"choices": []}} for r in requests]

    code = kli.LocalIngestRun(client, "kb1", CALL, answer, sleep=lambda s: None, out=lines.append).run()
    assert code == 0
    assert answered == [r["request_id"] for r in reqs]
    sent = [len(args["answers"]) for name, args in client.calls if "answers" in args]
    assert sent == [kli.ANSWERS_PER_CALL, 6 - kli.ANSWERS_PER_CALL], "answers go in small batches"
    assert "SOURCE s1 ready Spain (9 pages)" in lines and lines[-1].startswith("DONE 1 source")


def test_a_waiting_job_is_announced_once_and_a_failed_source_fails_the_run():
    job = {"job_id": JOB, "source_id": "src_mcp", "integration_id": "confluence", "status": "waiting"}
    failed = {"sourceId": "s1", "status": "failed", "failureCode": "local_model_unanswered",
              "failureMessage": "no answer"}
    client = FakeClient([_state(jobs=[job]), _state(jobs=[job]), _state(sources=[failed], done=True)])
    lines = []
    code = kli.LocalIngestRun(client, "kb1", CALL, lambda r: [], sleep=lambda s: None, out=lines.append).run()
    assert code == 1
    assert [l for l in lines if l.startswith("EXTRACTION_JOB_WAITING")] == [
        "EXTRACTION_JOB_WAITING job={} source=src_mcp integration=confluence".format(JOB)]
    assert any("local_model_unanswered: no answer" in l for l in lines)


# --- pointing the kb-extractor at a job ---------------------------------

def _pulled_extractor(project: Path) -> Path:
    agent_dir = project / ".soleon" / "agents" / "kb-extractor"
    agent_dir.mkdir(parents=True)
    (agent_dir / "pull.json").write_text(json.dumps({"slug": "kb-extractor", "model": "sonnet"}))
    return agent_dir


def test_prepare_lists_the_jobs_tools_and_writes_the_extraction_file(tmp_path, monkeypatch):
    agent_dir = _pulled_extractor(tmp_path)
    done = {"state": "done", "call_id": CALL, "result": {"tools": [{"name": "emit_document"}]}}
    client = FakeClient([done])
    ran = []
    monkeypatch.setattr(kli, "await_control", lambda c, slug, env, **kw: env)
    kli.prepare_extractor(client, tmp_path, CALL, JOB,
                          runner=lambda cmd, **kw: ran.append(cmd) or SimpleNamespace(returncode=0))
    (name, args), = client.calls
    assert name == "list_agent_tools"
    assert args == {"slug": "kb-extractor", "app_env": "dev", "extraction_job_id": JOB,
                    "extraction_call_id": CALL}
    assert json.loads((agent_dir / "tools.json").read_text()) == done
    assert json.loads((agent_dir / kli.EXTRACTION_FILE).read_text()) == {"callId": CALL, "jobId": JOB}
    assert ran and "materialize" in ran[0]
    kli.release_extractor(client, tmp_path,
                          runner=lambda cmd, **kw: SimpleNamespace(returncode=0))
    assert not (agent_dir / kli.EXTRACTION_FILE).exists()
    assert "extraction_job_id" not in client.calls[-1][1]


def test_prepare_refuses_when_the_extractor_is_not_pulled(tmp_path):
    with pytest.raises(kli.SoleonClientError, match="not pulled"):
        kli.prepare_extractor(FakeClient([{}]), tmp_path, CALL, JOB)


def test_the_agent_tools_server_and_the_scripts_share_one_file_name():
    assert shim.EXTRACTION_FILE == kli.EXTRACTION_FILE


def test_the_extraction_file_adds_the_job_to_every_call(tmp_path):
    path = tmp_path / shim.EXTRACTION_FILE
    assert shim.extraction_args(str(path)) == {}
    path.write_text(json.dumps({"callId": CALL, "jobId": JOB}))
    assert shim.extraction_args(str(path)) == {"extraction_job_id": JOB, "extraction_call_id": CALL}
    path.write_text("not json")
    assert shim.extraction_args(str(path)) == {}


def test_a_long_prompt_rides_stdin_never_the_command_line():
    """Windows caps a command line at 32,767 characters; a distill prompt is
    tens of thousands (2026-11-17, WinError 206)."""
    seen = []
    big = {"messages": [{"role": "user", "content": "x" * 60_000}]}

    def run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "ok", "usage": {}}), stderr="")

    answerer = lm.LocalModelAnswerer("sonnet", lambda: "claude", run=run)
    (answer,) = answerer.answer([{"request_id": "r1", "body": big}])
    assert answer["completion"]["choices"][0]["message"]["content"] == "ok"
    cmd, kwargs = seen[0]
    assert sum(len(a) for a in cmd) < 2_000
    assert kwargs["input"] == "x" * 60_000


@pytest.mark.parametrize("extra, flagged", [([], False), (["--incomplete"], True)])
def test_finish_hands_the_job_back_and_says_when_it_is_incomplete(monkeypatch, capsys, extra, flagged):
    """2026-11-21: a kb-extractor that stopped short finishes with
    --incomplete, so a refresh reads the same changes again next time."""
    client = FakeClient([{"sources": [{"sourceId": "s"}]}])
    monkeypatch.setattr(kli, "SoleonMcpClient", lambda _url: client)
    assert kli.main(["finish", "--kb", "kb1", "--call", CALL, "--job", JOB] + extra) == 0
    (name, args), = client.calls
    assert name == "local_kb_ingest"
    assert args == {"kb_id": "kb1", "call_id": CALL, "finish_job_id": JOB,
                    **({"incomplete": True} if flagged else {})}
    assert ("as incomplete" in capsys.readouterr().out) is flagged
