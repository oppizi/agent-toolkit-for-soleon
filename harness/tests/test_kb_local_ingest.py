"""A LOCAL knowledge-base build (2026-11-17): Soleon runs the ingestion and
hands every model call that decides a source's pages to this machine.

Covers bin/soleon_kb_local_ingest.py (poll, answer, report; pointing the pulled
kb-extractor at an extraction job), the several-tool answers the distill's
lookup loop needs (bin/soleon_local_model.py), and the extraction-job file the
agent-tools server and the turn hook read."""
from __future__ import annotations

import json
import sys
import threading
import time
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


def _branches(schema):
    assert "anyOf" not in schema, "claude -p refuses anyOf at a schema's top level"
    assert schema["required"] == [lm.CHOICE_KEY]
    return {b["properties"]["tool"]["enum"][0]: b["properties"]["arguments"]
            for b in schema["properties"][lm.CHOICE_KEY]["anyOf"]}


def test_several_tools_ask_for_one_named_choice():
    cmd = lm.command("claude", "sonnet", LOOKUP)
    flags = dict(zip(cmd[1:], cmd[2:]))
    schema = json.loads(flags["--json-schema"])
    assert list(_branches(schema)) == ["emit_distill_plan", "search_wiki", "read_page"]
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


def _chose(tool, arguments):
    return {"structured_output": {lm.CHOICE_KEY: {"tool": tool, "arguments": arguments}}}


def test_a_choice_answer_becomes_that_tools_call():
    out = lm.completion(LOOKUP, _chose("read_page", {"path": "concepts/fees.md"}))
    call, = out["choices"][0]["message"]["tool_calls"]
    assert call["function"]["name"] == "read_page"
    assert json.loads(call["function"]["arguments"]) == {"path": "concepts/fees.md"}
    assert out["choices"][0]["finish_reason"] == "tool_calls"


PLAN_SCHEMA = {"type": "object", "properties": {"source_page": {"type": "object"},
                                                "entity_touches": {"type": "array"}},
               "required": ["source_page", "entity_touches"]}


def test_each_choice_carries_its_tools_own_input_schema():
    """2026-11-21: with `arguments` checked only as an object, the model called
    emit_distill_plan without source_page and two sources failed."""
    body = {**LOOKUP, "tools": [_fn("emit_distill_plan", PLAN_SCHEMA), _fn("search_wiki")]}
    schema, form, _ = lm._schema(body)
    assert form == "choose"
    assert _branches(schema) == {"emit_distill_plan": PLAN_SCHEMA, "search_wiki": {"type": "object"}}


def test_a_self_referencing_tool_schema_is_checked_as_an_object():
    ref = {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"type": "string"}}}
    body = {**LOOKUP, "tools": [_fn("emit_distill_plan", ref), _fn("search_wiki")]}
    assert _branches(lm._schema(body)[0])["emit_distill_plan"] == {"type": "object"}


@pytest.mark.parametrize("value, needle", [
    ({"tool": "read_page", "arguments": {}}, "names no tool"),            # the old, unwrapped shape
    ({lm.CHOICE_KEY: {"tool": "read_page", "arguments": "{}"}}, "not an object"),
    ({lm.CHOICE_KEY: {"tool": "delete_wiki", "arguments": {}}}, "did not offer"),
    ({lm.CHOICE_KEY: {"tool": lm.REPLY_CHOICE, "arguments": {"text": "x"}}}, "did not offer"),
])
def test_a_malformed_choice_is_an_error_never_an_empty_call(value, needle):
    with pytest.raises(ValueError, match=needle):
        lm.completion(LOOKUP, {"structured_output": value})


def test_auto_choice_may_reply_in_words():
    body = {**LOOKUP, "tool_choice": "auto"}
    schema, form, _ = lm._schema(body)
    assert _branches(schema)[lm.REPLY_CHOICE]["required"] == ["text"]
    out = lm.completion(body, _chose(lm.REPLY_CHOICE, {"text": "{}"}))
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


class PlatformDouble:
    """The poll as the platform answers it: a request waits until an answer
    for it arrives, and the run is done once every request was answered."""

    def __init__(self, requests, on_answer=lambda rid: None):
        self.waiting = {r["request_id"]: r for r in requests}
        self.batches, self.on_answer = [], on_answer
        self.lock = threading.Lock()

    def call_tool(self, name, args):
        with self.lock:
            for a in args.get("answers") or []:
                self.waiting.pop(a["request_id"], None)
                self.on_answer(a["request_id"])
            if args.get("answers"):
                self.batches.append([a["request_id"] for a in args["answers"]])
            done = not self.waiting
            source = {"sourceId": "s1", "status": "ready" if done else "ingesting", "title": "Spain"}
            if done:
                source["pagesContributed"] = 9
            return {"status": 200, "body": _state(requests=list(self.waiting.values()),
                                                  sources=[source], done=done)}


def _req(i):
    return {"request_id": "%032x" % i, "model": "m", "body": {"messages": []}}


def test_a_run_answers_every_waiting_request_once_and_stops_when_done():
    reqs = [_req(i) for i in range(6)]
    client, lines, answered = PlatformDouble(reqs), [], []

    def answer(requests):
        answered.extend(r["request_id"] for r in requests)
        return [{"request_id": r["request_id"], "completion": {"choices": []}} for r in requests]

    code = kli.LocalIngestRun(client, "kb1", CALL, answer, sleep=lambda s: time.sleep(0.01),
                              out=lines.append).run()
    assert code == 0
    assert sorted(answered) == sorted(r["request_id"] for r in reqs), "each call answered exactly once"
    assert all(len(b) <= kli.ANSWERS_PER_CALL for b in client.batches), "answers go in small batches"
    assert sorted(sum(client.batches, [])) == sorted(answered)
    assert "SOURCE s1 ready Spain (9 pages)" in lines and lines[-1].startswith("DONE 1 source")


def test_a_slow_call_never_holds_back_an_answer_that_is_ready():
    """2026-11-21: answers went back only once the slowest call of a batch
    returned, so a source's next call waited on other sources' largest
    documents and two sources ran out of time."""
    slow, fast = _req(1), _req(2)
    fast_handed_back = threading.Event()
    client = PlatformDouble([slow, fast],
                            on_answer=lambda rid: rid == fast["request_id"] and fast_handed_back.set())

    def answer(requests):
        r, = requests
        if r["request_id"] == slow["request_id"]:
            assert fast_handed_back.wait(5), "the fast answer waited for the slow call"
        return [{"request_id": r["request_id"], "completion": {"choices": []}}]

    code = kli.LocalIngestRun(client, "kb1", CALL, answer, sleep=lambda s: time.sleep(0.01),
                              out=lambda line: None).run()
    assert code == 0
    assert client.batches[0] == [fast["request_id"]]


def test_an_answerer_that_raises_becomes_that_calls_error():
    client, lines = PlatformDouble([_req(3)]), []
    posted = []
    client.on_answer = posted.append

    def answer(requests):
        raise RuntimeError("boom")

    kli.LocalIngestRun(client, "kb1", CALL, answer, sleep=lambda s: time.sleep(0.01), out=lines.append).run()
    assert posted == [_req(3)["request_id"]]
    assert any("the local answerer failed: RuntimeError: boom" in l for l in lines)


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


def test_prepare_with_the_kb_gives_the_agent_the_jobs_own_budget(tmp_path, monkeypatch):
    agent_dir = _pulled_extractor(tmp_path)
    state = {"extraction_jobs": [{"job_id": JOB, "source_id": "src_mcp_gh"}]}
    source = {"status": 200, "body": {"sourceId": "src_mcp_gh", "tokenBudgetMax": 2_000_000}}
    done = {"state": "done", "call_id": CALL, "result": {"tools": [{"name": "emit_document"}]}}

    class Client(FakeClient):
        def call_tool(self, name, args):
            self.calls.append((name, json.loads(json.dumps(args))))
            return {"local_kb_ingest": {"status": 200, "body": state},
                    "get_kb_source": source}.get(name, done)

    client = Client([done])
    monkeypatch.setattr(kli, "await_control", lambda c, slug, env, **kw: env)
    kli.prepare_extractor(client, tmp_path, CALL, JOB, kb_id="kb1",
                          runner=lambda cmd, **kw: SimpleNamespace(returncode=0))
    assert [n for n, _ in client.calls] == ["local_kb_ingest", "get_kb_source", "list_agent_tools"]
    assert client.calls[1][1] == {"kb_id": "kb1", "source_id": "src_mcp_gh"}
    assert json.loads((agent_dir / kli.EXTRACTION_FILE).read_text()) == {
        "callId": CALL, "jobId": JOB, "tokenBudget": 2_000_000}
