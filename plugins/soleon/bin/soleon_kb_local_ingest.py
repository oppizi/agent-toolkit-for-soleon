#!/usr/bin/env python3
"""Drive a LOCAL knowledge-base ingestion from this machine.

Soleon runs the ingestion — convert, pages, links, facts, versions, embeddings
— and hands every model call that decides a source's pages to the caller
(agent-infra `knowledge_ingestor/local_model.py`; owner decision 2026-11-17:
local testing never calls Bedrock for the agent's reasoning). Start the run
with `ingest_kb_sources(local=true)`, then:

  run                 Poll `local_kb_ingest`, answer each waiting model call
                      with headless Claude Code (`soleon_local_model.py`), and
                      report progress until every source is done. An mcp
                      source's extraction job is announced on a line starting
                      `EXTRACTION_JOB_WAITING`; the skill runs the pulled
                      kb-extractor for it and finishes it (`finish`).
  prepare-extractor   Point the pulled kb-extractor at one job: its tools are
                      re-listed under the job (the integration's tools and
                      emit_document) and `.local-extraction.json` makes every
                      call it makes run under the job.
  release-extractor   Undo `prepare-extractor` (the agent's own tools again).
  finish              Hand a job back once the kb-extractor has emitted its
                      documents; Soleon turns them into sources and ingests
                      them, locally too. --incomplete when the agent stopped
                      before covering everything: an update then reads the
                      same changes again next time.

Stdlib only, Python 3.9-compatible, like every script the plugin ships.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from soleon_local_model import LocalModelAnswerer  # noqa: E402
from soleon_mcp_client import (  # noqa: E402
    DEFAULT_SERVER_URL,
    SoleonClientError,
    SoleonMcpClient,
    await_control,
    describe_error,
    is_platform_error,
)

#: The file the agent-tools server and the turn hooks read (soleon_agent_tools_mcp.EXTRACTION_FILE).
EXTRACTION_FILE = ".local-extraction.json"
#: soleon_message_budget.WAIVER_ENV — set by a daily refresh task installed with
#: --waive-budget, for its headless runs.
WAIVER_ENV = "SOLEON_KB_BUDGET_WAIVED"
KB_EXTRACTOR = "kb-extractor"
#: Answers sent per call: each completion can be large, and the wire has a size cap.
ANSWERS_PER_CALL = 4
POLL_INTERVAL_S = 3.0
#: Model calls answered at once. Soleon ingests a job's sources together, and
#: each call is one headless Claude Code process.
ANSWER_WORKERS = 12
#: How long the loop keeps retrying a transient platform error (5xx gateway
#: answers, "no capacity", a dropped connection) before it gives up: under the
#: 12 minutes after which Soleon fails a model call nobody answered.
TRANSIENT_RETRY_S = 600.0
TRANSIENT_FIRST_DELAY_S = 5.0
TRANSIENT_MAX_DELAY_S = 60.0
_TRANSIENT_MARKERS = ("HTTP 502", "HTTP 503", "HTTP 504", "no capacity", "unreachable", "timed out", "Connection reset",
                      "Connection aborted", "Connection refused", "Temporary failure", "Remote end closed")


def _is_transient(exc: Exception) -> bool:
    """A platform error worth retrying: the gateway or the server was briefly
    unavailable, not a refusal of the request itself."""
    text = str(exc)
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _out(line: str, stream=None) -> None:
    """Print one status line. Source titles come from documents and can hold any
    character; on Windows a redirected stdout is cp1252, and an arrow in a title
    killed the answering loop mid-run (2026-11-22, the loop then stops answering
    and every waiting model call fails 12 minutes later). Characters the stream
    cannot encode are replaced, never fatal."""
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    stream.write(line.encode(encoding, "replace").decode(encoding) + "\n")
    stream.flush()


def _checked(envelope: Any, what: str) -> Dict[str, Any]:
    if not isinstance(envelope, dict) or is_platform_error(envelope):
        raise SoleonClientError("{}: {}".format(
            what, describe_error(envelope) if isinstance(envelope, dict) else envelope))
    return envelope


def _body(envelope: Any, what: str) -> Dict[str, Any]:
    """A forward tool's answer, `{status, body}`: the body of a 2xx, else an error."""
    envelope = _checked(envelope, what)
    status = envelope.get("status")
    if not isinstance(status, int) or not 200 <= status < 300 or not isinstance(envelope.get("body"), dict):
        raise SoleonClientError("{}: {}".format(what, json.dumps(envelope, default=str)[:600]))
    return envelope["body"]


class LocalIngestRun:
    """One run: poll, answer, report. ``answer`` is the local model."""

    def __init__(self, client, kb_id: str, call_id: str, answer: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
                 *, interval_s: float = POLL_INTERVAL_S, sleep=time.sleep, out=_out,
                 workers: int = ANSWER_WORKERS):
        self.client, self.kb_id, self.call_id = client, kb_id, call_id
        self.answer, self.interval_s, self.sleep, self.out = answer, interval_s, sleep, out
        self._seen_status: Dict[str, Any] = {}
        self._announced_jobs: set = set()
        #: request id -> its running answer; ids already handed back.
        self._inflight: Dict[str, Future] = {}
        self._answered: set = set()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="local-answer")

    def poll(self, **extra) -> Dict[str, Any]:
        """One `local_kb_ingest` call, retried while the platform answers a
        TRANSIENT error. A single `HTTP 503: no capacity` killed the loop twice
        in one run (2026-11-23), and with the loop gone every waiting model
        call fails 12 minutes later. Re-sending answers is safe: one the
        platform already took is reported stale. Any other error, or a
        transient one that outlasts TRANSIENT_RETRY_S, still ends the run."""
        waited, delay = 0.0, TRANSIENT_FIRST_DELAY_S
        while True:
            try:
                return _body(self.client.call_tool("local_kb_ingest", {
                    "kb_id": self.kb_id, "call_id": self.call_id, **extra}), "local_kb_ingest")
            except SoleonClientError as exc:
                if not _is_transient(exc) or waited >= TRANSIENT_RETRY_S:
                    raise
                self.out("platform busy ({}); retrying in {:.0f}s".format(str(exc)[:160], delay))
                self.sleep(delay)
                waited += delay
                delay = min(delay * 2, TRANSIENT_MAX_DELAY_S)

    def report(self, state: Dict[str, Any]) -> None:
        for source in state.get("sources") or []:
            key = source.get("sourceId")
            status = (source.get("status"), source.get("failureCode"))
            if self._seen_status.get(key) != status:
                self._seen_status[key] = status
                detail = ""
                if source.get("status") == "failed":
                    detail = " — {}: {}".format(source.get("failureCode") or "failed",
                                                source.get("failureMessage") or "")
                elif source.get("pagesContributed") is not None:
                    detail = " ({} pages)".format(source["pagesContributed"])
                self.out("SOURCE {} {} {}{}".format(key, source.get("status"), source.get("title") or "", detail))
        for job in state.get("extraction_jobs") or []:
            if job.get("status") == "waiting" and job.get("job_id") not in self._announced_jobs:
                self._announced_jobs.add(job.get("job_id"))
                self.out("EXTRACTION_JOB_WAITING job={} source={} integration={}".format(
                    job.get("job_id"), job.get("source_id"), job.get("integration_id")))

    def _one(self, request: Dict[str, Any]) -> Dict[str, Any]:
        try:
            answers = self.answer([request])
        except Exception as exc:  # noqa: BLE001 — reported to the platform as this call's error
            return {"request_id": request.get("request_id"),
                    "error": "the local answerer failed: {}: {}".format(type(exc).__name__, exc)}
        if len(answers) == 1 and isinstance(answers[0], dict):
            return answers[0]
        return {"request_id": request.get("request_id"), "error": "the local answerer returned no answer"}

    def _finished(self) -> List[Dict[str, Any]]:
        done = [rid for rid, future in self._inflight.items() if future.done()]
        answers = [self._inflight.pop(rid).result() for rid in done]
        self._answered.update(done)
        return answers

    def step(self) -> Dict[str, Any]:
        """One poll. Hands back every answer that is ready and starts every
        call that newly waits, WITHOUT waiting for the calls still running: a
        source asks its calls one after another (the distill's lookups, then
        its plan), so holding a finished answer until the slowest call of a
        batch returns made each source wait on the others' largest documents
        and ran it past Soleon's ~13-minute limit (2026-11-21: two sources
        failed `local_model_unanswered`). Returns the latest state."""
        answers = self._finished()
        if not answers:
            state = self.poll()
        for i in range(0, len(answers), ANSWERS_PER_CALL):
            state = self.poll(answers=answers[i:i + ANSWERS_PER_CALL])
        for a in answers:
            if a.get("error"):
                self.out("model call {} failed locally: {}".format(a.get("request_id"), a["error"]))
        self.report(state)
        new = [r for r in state.get("model_requests") or []
               if r.get("request_id") not in self._inflight and r.get("request_id") not in self._answered]
        if new:
            self.out("answering {} model call(s) locally ({} already running)".format(
                len(new), len(self._inflight)))
        for request in new:
            self._inflight[request.get("request_id")] = self._pool.submit(self._one, request)
        return state

    def run(self) -> int:
        try:
            while True:
                state = self.step()
                if state.get("done") and not self._inflight:
                    failed = [s for s in state.get("sources") or [] if s.get("status") == "failed"]
                    self.out("DONE {} source(s), {} failed".format(len(state.get("sources") or []), len(failed)))
                    return 1 if failed else 0
                if not any(f.done() for f in self._inflight.values()):
                    self.sleep(self.interval_s)
        finally:
            self._pool.shutdown(wait=False)


def _agent_dir(project: Path) -> Path:
    return project / ".soleon" / "agents" / KB_EXTRACTOR


def _plugin_root() -> Path:
    return Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parents[1])


def _materialize(agent_dir: Path, runner=subprocess.run) -> None:
    """Re-write the kb-extractor's subagent file from the saved tools.json,
    the way the refresh hook does (soleon_pull_refresh._materialize)."""
    pull = json.loads((agent_dir / "pull.json").read_text(encoding="utf-8"))
    root = _plugin_root()
    cmd = [sys.executable, str(root / "skills" / "pull-agent" / "assets" / "pull_agent.py"), "materialize",
           "--slug", KB_EXTRACTOR, "--dir", str(agent_dir),
           "--server-url", str(pull.get("serverUrl") or DEFAULT_SERVER_URL),
           "--model", str(pull.get("model") or "sonnet"), "--plugin-root", str(root), "--keep-workspace"]
    proc = runner(cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SoleonClientError("materialize failed: {}".format((proc.stderr or proc.stdout or "").strip()[-800:]))


def _list_tools(client, extraction: Optional[Dict[str, str]]) -> Dict[str, Any]:
    args: Dict[str, Any] = {"slug": KB_EXTRACTOR, "app_env": "dev"}
    if extraction:
        args.update(extraction_job_id=extraction["jobId"], extraction_call_id=extraction["callId"])
    envelope = _checked(client.call_tool("list_agent_tools", args), "list_agent_tools")
    envelope = await_control(client, KB_EXTRACTOR, envelope, app_env="dev")
    if not isinstance(envelope, dict) or envelope.get("state") != "done":
        raise SoleonClientError("list_agent_tools: {}".format(
            describe_error(envelope) if isinstance(envelope, dict) else envelope))
    return envelope


def job_budget(client, kb_id: str, call_id: str, job_id: str) -> Optional[int]:
    """The token budget the platform gives this extraction job: its source's
    ``tokenBudgetMax`` (the sidecar proxy enforces it on a platform run). The
    kb-extractor's own Per Message Token Budget is for a chat message, not an
    extraction (2026-11-21: two local refreshes stopped at that 500,000 default
    while the job's source allowed 2,000,000)."""
    state = _body(client.call_tool("local_kb_ingest", {"kb_id": kb_id, "call_id": call_id}),
                  "local_kb_ingest")
    job = next((j for j in state.get("extraction_jobs") or [] if j.get("job_id") == job_id), None)
    if not job or not job.get("source_id"):
        return None
    source = _body(client.call_tool("get_kb_source", {"kb_id": kb_id, "source_id": job["source_id"]}),
                   "get_kb_source")
    source = source.get("source", source) if isinstance(source, dict) else {}
    raw = source.get("tokenBudgetMax")
    return int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0 else None


def budget_waived(flag: bool, kb_id: Optional[str], environ=None, take_ask=None, standing=None,
                  job_id: Optional[str] = None) -> bool:
    """The person waived this job's budget, either way:
      * `--waive-budget` — Claude Code asked them first
        (soleon_message_budget.waiver_gate). Refused unless the gate's record of
        that prompt is there: a conversation that started before the gate
        existed never shows it, and must not waive silently;
      * a daily refresh task they installed with --waive-budget (approved the
        same way), which hands its headless runs WAIVER_ENV; honoured only while
        that task's waiver record exists."""
    import soleon_message_budget as mb
    env = os.environ if environ is None else environ
    take_ask = take_ask or mb.take_waiver_ask
    standing = standing or mb.standing_waiver
    if flag:
        if not take_ask("prepare-extractor", naming=[job_id] if job_id else ()):
            raise SoleonClientError(
                "--waive-budget was not approved: Claude Code never showed the approval prompt for it in "
                "this conversation, so the budget stays. Run prepare-extractor without --waive-budget.")
        return True
    if env.get(WAIVER_ENV) == "1":
        if kb_id and standing(kb_id).is_file():
            return True
        raise SoleonClientError("{} is set, but no daily refresh task of this knowledge base was installed "
                                "with --waive-budget: the budget stays.".format(WAIVER_ENV))
    return False


def prepare_extractor(client, project: Path, call_id: str, job_id: str, runner=subprocess.run,
                      kb_id: Optional[str] = None, waive_budget: bool = False) -> Path:
    agent_dir = _agent_dir(project)
    if not (agent_dir / "pull.json").is_file():
        raise SoleonClientError("kb-extractor is not pulled into this project: run the pull-agent "
                                "skill for kb-extractor first")
    extraction: Dict[str, Any] = {"callId": call_id, "jobId": job_id}
    if waive_budget:
        # Read by soleon_message_budget.budget_of: no budget for the agent or its helpers.
        extraction["budgetWaived"] = True
    elif kb_id:
        budget = job_budget(client, kb_id, call_id, job_id)
        if budget:
            # Read by soleon_message_budget.budget_of while the agent runs the job.
            extraction["tokenBudget"] = budget
    tools = _list_tools(client, extraction)
    (agent_dir / "tools.json").write_text(json.dumps(tools, indent=2), encoding="utf-8")
    (agent_dir / EXTRACTION_FILE).write_text(json.dumps(extraction), encoding="utf-8")
    _materialize(agent_dir, runner)
    return agent_dir


def release_extractor(client, project: Path, runner=subprocess.run) -> Path:
    agent_dir = _agent_dir(project)
    try:
        (agent_dir / EXTRACTION_FILE).unlink()
    except FileNotFoundError:
        pass
    tools = _list_tools(client, None)
    (agent_dir / "tools.json").write_text(json.dumps(tools, indent=2), encoding="utf-8")
    _materialize(agent_dir, runner)
    return agent_dir


def _binary() -> Optional[str]:
    from soleon_agent_tools_mcp import claude_binary
    return claude_binary()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--kb", required=True)
    run.add_argument("--call", required=True)
    run.add_argument("--model", default="sonnet", help="the local model that answers (a claude --model value)")
    run.add_argument("--interval", type=float, default=POLL_INTERVAL_S)
    prep = sub.add_parser("prepare-extractor")
    prep.add_argument("--call", required=True)
    prep.add_argument("--job", required=True)
    prep.add_argument("--kb", help="the knowledge base: the agent then works to the job's own token budget")
    prep.add_argument("--project", default=".")
    prep.add_argument("--waive-budget", action="store_true",
                      help="run the job with no token budget (Claude Code asks the person first)")
    rel = sub.add_parser("release-extractor")
    rel.add_argument("--project", default=".")
    fin = sub.add_parser("finish")
    fin.add_argument("--kb", required=True)
    fin.add_argument("--call", required=True)
    fin.add_argument("--job", required=True)
    fin.add_argument("--incomplete", action="store_true",
                     help="the kb-extractor stopped before it covered everything (its budget ran out): "
                          "keep its documents, but read the same changes again next time")
    args = parser.parse_args(argv)
    client = SoleonMcpClient(args.server_url)
    try:
        if args.cmd == "run":
            answerer = LocalModelAnswerer(args.model, _binary, log=lambda m: print(m, file=sys.stderr))
            return LocalIngestRun(client, args.kb, args.call, answerer.answer, interval_s=args.interval).run()
        if args.cmd == "prepare-extractor":
            waived = budget_waived(args.waive_budget, args.kb, job_id=args.job)
            path = prepare_extractor(client, Path(args.project).resolve(), args.call, args.job, kb_id=args.kb,
                                     waive_budget=waived)
            _out("kb-extractor at {} now runs under job {}{}".format(
                path, args.job, ", with no token budget (waived)" if waived else ""))
            return 0
        if args.cmd == "release-extractor":
            path = release_extractor(client, Path(args.project).resolve())
            _out("kb-extractor at {} runs its own tools again".format(path))
            return 0
        finish = {"kb_id": args.kb, "call_id": args.call, "finish_job_id": args.job}
        if args.incomplete:
            finish["incomplete"] = True
        state = _body(client.call_tool("local_kb_ingest", finish), "local_kb_ingest")
        _out("job {} handed back{}; {} source(s) in the run".format(
            args.job, " as incomplete" if args.incomplete else "", len(state.get("sources") or [])))
        return 0
    except SoleonClientError as exc:
        print("soleon: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
