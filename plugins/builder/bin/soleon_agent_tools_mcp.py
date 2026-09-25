#!/usr/bin/env python3
"""`soleon-agent-tools` — a stdio MCP server that publishes a Soleon agent's
EXTERNAL tools under their platform names and schemas, and runs each one on
the platform through `call_agent_tool` (spec D2, D7, D14).

    python3 soleon_agent_tools_mcp.py --slug <slug> --tools <tools.json> \
        --server-url <soleon-mcp-url> [--credentials ~/.claude/.credentials.json]

The local model therefore sees `gmail_send_email` with the REAL input schema,
exactly as the platform model does, while the Soleon MCP stays generic (one
`call_agent_tool(slug, name, args)`). Every call runs in the caller's own
draft test-chat session on the platform with the caller's credentials, the
agent's tool policy and its approval gating — nothing executes locally.

Approval (D8): a tool whose `approval` flag is true gets an optional boolean
`approved` property and a note in its description; the model must ask the
person BEFORE calling and then pass `approved=true`. The server refuses an
un-approved call with `approval_required` server-side, which is relayed as an
error result worded so the model asks and retries.

Waiting (D14): `call_agent_tool` answers `pending + call_id` when the platform
is still working at the wire's time limit; this shim polls
`get_agent_tool_result` every 1–2 s until the op is terminal, with NO timeout
— the platform waits for the tool, so does the local caller.

Token: Claude Code's own MCP OAuth token for the configured server (see
`soleon_mcp_client.py`). Logs go to stderr only; tokens are never logged.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soleon_message_budget as message_budget  # noqa: E402
from soleon_mcp_client import (  # noqa: E402
    AuthError,
    MissingTokenError,
    SoleonMcpClient,
    ToolError,
    TransportError,
    await_control,
    describe_error,
    is_platform_error,
)

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "soleon-agent-tools", "version": "0.4.15"}
APPROVAL_NOTE = "Requires human approval: ask the person first, then call with approved=true."
POLL_INTERVAL_S = 1.5

#: A parameter whose schema default is an email address names the account the
#: PLATFORM connection is authorized for — the platform filled that default in
#: from the stored credential. A LOCAL model has an unrelated address in front
#: of it (Claude Code tells every session "the user's email address is …",
#: which is the local login, not the connected account), and substituting it
#: fails silently: the upstream server looks the address up in its credential
#: store, finds nothing, and answers with a fresh OAuth consent link instead
#: of doing the work. Receipt: fund-raising-agent, 2026-09-21 — the agent
#: called ``create_spreadsheet`` with ``user_google_email`` set to the local
#: login while the connection held ``dan@oppizi.com``; the person was handed a
#: localhost OAuth URL for a connection that was never broken, and read it as
#: the approval flow misfiring.
IDENTITY_DEFAULT_NOTE = (
    "This default is the account the platform connection is authorized for. "
    "Leave the parameter out and let the default stand, unless the person names "
    "a different account themselves — never substitute your local login."
)


#: An upstream MCP tool that answers ``isError`` does NOT reach us as an error
#: envelope: ``mcp_proxy`` renders it as ``"(MCP tool error: …)"`` and returns
#: it as a perfectly successful ``state: done`` result (the container owns that
#: exact prefix — `containers/shared/tools/mcp_proxy.py`). So "did this call
#: fail" cannot be read off ``isError`` alone, and a hint gated on that flag
#: would never fire for the very failure it was written for.
PLATFORM_TOOL_ERROR_PREFIX = "(MCP tool error:"


def _looks_failed(out: Dict[str, Any]) -> bool:
    if out.get("isError"):
        return True
    for block in out.get("content") or []:
        if PLATFORM_TOOL_ERROR_PREFIX in str(block.get("text") or ""):
            return True
    return False


def _is_email(value: Any) -> bool:
    """An email-SHAPED string. Deliberately loose: it only decides whether to
    add a note or a hint, never whether a call runs."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    return "@" in v and " " not in v and not v.startswith("@") and not v.endswith("@")


def _log(msg: str) -> None:
    sys.stderr.write("[soleon-agent-tools] {}\n".format(msg))
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# tools.json → published tool list
# ---------------------------------------------------------------------------

def load_tools(path: str) -> List[Dict[str, Any]]:
    """Accept the raw `list_agent_tools` envelope (`{state, result: {tools}}`),
    a `{tools: [...]}` object, or a bare list."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        if isinstance(data.get("result"), dict) and isinstance(data["result"].get("tools"), list):
            data = data["result"]["tools"]
        elif isinstance(data.get("tools"), list):
            data = data["tools"]
    if not isinstance(data, list):
        raise ValueError("{} does not hold a tool list".format(path))
    return [t for t in data if isinstance(t, dict) and t.get("name")]


def _identity_defaults(schema: Any) -> Dict[str, str]:
    """`{property: default}` for every property whose default is an email —
    i.e. every parameter that names the connection's own account."""
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return {}
    return {
        key: spec["default"] for key, spec in props.items()
        if isinstance(spec, dict) and _is_email(spec.get("default"))
    }


def _annotate_identity_defaults(schema: Dict[str, Any]) -> None:
    """Say, at the point the model reads the parameter, that the default is
    the connected account. In place, on the already-copied schema."""
    props = schema.get("properties")
    if not isinstance(props, dict):
        return
    for key in _identity_defaults(schema):
        spec = props[key]
        existing = str(spec.get("description") or "").rstrip()
        spec["description"] = (existing + " " if existing else "") + IDENTITY_DEFAULT_NOTE


def identity_mismatch_hint(schema: Any, args: Dict[str, Any]) -> Optional[str]:
    """The call supplied an address for a parameter that already defaults to
    the connected account, and it is a DIFFERENT address.

    Returned only alongside a failure, never to block a call: acting on a
    second authorized account is legitimate, so this names the likely cause
    instead of deciding it. Without it the model sees only the upstream's
    "authorize this app" link and reports a healthy connection as broken.
    """
    for key, default in _identity_defaults(schema).items():
        supplied = args.get(key)
        if _is_email(supplied) and supplied.strip().lower() != default.strip().lower():
            return (
                "Note: you passed {}={!r}, but this connection is authorized for {!r} "
                "(the parameter's default). An address the platform holds no credential "
                "for is answered with a fresh authorization link, which is very likely "
                "what happened here. Retry WITHOUT {} so the connected account is used. "
                "Do not ask the person to re-authorize until that retry fails too."
            ).format(key, supplied, default, key)
    return None


def published_tools(tools: List[Dict[str, Any]], include_members: bool = False) -> List[Dict[str, Any]]:
    """Every `kind == external` tool, own name + own schema; approval-gated ones
    gain the optional `approved` boolean and the approval note.

    `include_members`: also publish `kind == pair_member` tools — an
    integration's individual tools behind a subagent-mode wrapper. Only a local
    WORKER's server does (selected by `--only`); the agent itself never sees
    them, exactly as on the platform, where the agent holds the wrapper and only
    its worker holds the integration's tools."""
    out: List[Dict[str, Any]] = []
    for t in tools:
        kind = t.get("kind", "external")
        if kind != "external" and not (include_members and kind == "pair_member"):
            continue
        schema = t.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        schema = json.loads(json.dumps(schema))  # deep copy, JSON-clean
        _annotate_identity_defaults(schema)
        description = str(t.get("description") or "")
        if t.get("approval"):
            props = schema.setdefault("properties", {})
            if not isinstance(props, dict):
                props = {}
                schema["properties"] = props
            props["approved"] = {
                "type": "boolean",
                "description": "Set true ONLY after the person explicitly approved this call.",
            }
            description = (description + " " if description else "") + APPROVAL_NOTE
        out.append({"name": published_name(t), "description": description, "inputSchema": schema})
    return out


def published_name(entry: Dict[str, Any]) -> str:
    """The MCP name a tool is published under. A worker's tool arrives from
    the platform qualified by its wrapper (`mcp_gmail_read::search_emails`) —
    unambiguous across integrations, but `:` is not legal in an MCP tool name —
    so it is published as the name the platform worker's model calls it by."""
    if entry.get("kind") == "pair_member" and entry.get("displayName"):
        return str(entry["displayName"])
    return str(entry["name"])


# ---------------------------------------------------------------------------
# tools/call → call_agent_tool (+ transparent polling)
# ---------------------------------------------------------------------------

def _text_result(text: str, is_error: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


def _render_tool_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


CONVERSATION_IDLE_S = 30 * 60


class ConversationTracker:
    """Which PLATFORM session this local conversation's calls land in.

    The platform starts one session per `conversation` id it is sent, so each
    local conversation is its own Traces entry. This process lives only as
    long as one subagent run (Claude Code starts the inline servers with the
    subagent and stops them with it — a resumed conversation restarts them),
    so the id cannot live in memory alone: it is kept in `<agent dir>/
    .local-conversation.json` with the time of the last call, and REUSED while
    the agent has been idle for less than `idle_s` (30 min — the platform's own
    session-inactivity window). A longer gap mints a new id: a new conversation.
    `None` path = no persistence (tests / ad-hoc runs): one id per process.
    """

    def __init__(self, path: Optional[str], idle_s: float = CONVERSATION_IDLE_S, now=None):
        self.path = path
        self.idle_s = idle_s
        self._now = now or __import__("time").time
        self._id: Optional[str] = None

    def _load(self) -> Optional[Dict[str, Any]]:
        if not self.path or not os.path.isfile(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def current(self) -> str:
        """The id to send with the NEXT call (touching the last-call time)."""
        now = self._now()
        data = self._load() or {}
        cid = data.get("conversation") if isinstance(data.get("conversation"), str) else None
        last = data.get("lastCallAt") if isinstance(data.get("lastCallAt"), (int, float)) else None
        if self._id is None:
            if cid and last is not None and (now - last) < self.idle_s:
                self._id = cid
                _log("continuing conversation {} ({:.0f}s since its last call)".format(cid, now - last))
            else:
                self._id = __import__("uuid").uuid4().hex[:24]
                _log("new conversation {} → a new platform session".format(self._id))
        self._save({"conversation": self._id, "lastCallAt": now})
        return self._id

    def _save(self, data: Dict[str, Any]) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError as exc:
            _log("could not persist the conversation id: {}".format(exc))


#: A hook-written turn file older than this (relative to the server's own
#: start) belongs to an EARLIER subagent run, not to this one.
TURN_FILE_FRESH_S = 60.0
#: Where the turn id is kept, next to tools.json in the pulled agent's dir.
TURN_FILE_NAME = ".local-turn.json"


def mint_turn_id() -> str:
    return __import__("uuid").uuid4().hex[:12]


class TurnTracker:
    """Which platform TURN this subagent run's calls land in.

    One run of the subagent = one prompt = one turn on the platform trace.
    The plugin's SubagentStart hook writes `<agent dir>/.local-turn.json`
    (`{turn, agentId, startedAt, source: "hook"}`) the moment the subagent
    spawns; this server — started for the same run — adopts that id on its
    first call, so every platform tool call carries it and the SubagentStop
    hook can finish the same turn with the prompt and the answer. Without a
    fresh hook file (hooks not installed, or a file left by an earlier run)
    the server mints its own id and writes the file so the stop hook can
    still find it. `None` path = no persistence: one id per process.
    """

    def __init__(self, path: Optional[str], now=None, process_started_at: Optional[float] = None,
                 fixed: Optional[str] = None):
        self.path = path
        self._now = now or __import__("time").time
        self._started = process_started_at if process_started_at is not None else self._now()
        #: A local worker's server is TOLD its turn (`--turn`): it starts
        #: minutes into the agent's run, when the hook file already reads as
        #: stale, and minting its own id would split the message in two.
        self._id: Optional[str] = fixed or None

    def _load(self) -> Optional[Dict[str, Any]]:
        if not self.path or not os.path.isfile(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def current(self) -> str:
        if self._id is not None:
            return self._id
        data = self._load() or {}
        turn = data.get("turn") if isinstance(data.get("turn"), str) else None
        started = data.get("startedAt") if isinstance(data.get("startedAt"), (int, float)) else None
        if turn and started is not None and started >= self._started - TURN_FILE_FRESH_S:
            self._id = turn
            _log("joining turn {} (from the subagent-start hook)".format(turn))
        else:
            self._id = mint_turn_id()
            _log("new turn {} (no fresh hook file — the stop hook will use this one)".format(self._id))
            self._save({"turn": self._id, "agentId": None, "startedAt": self._now(), "source": "server"})
        return self._id

    def _save(self, data: Dict[str, Any]) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError as exc:
            _log("could not persist the turn id: {}".format(exc))


def local_worker_of(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The `localWorker` block of a subagent-mode wrapper, or None.

    None for everything else AND for a wrapper from a platform too old to
    describe its worker — that wrapper keeps running on Soleon as before,
    which is the only honest thing to do with it."""
    if not entry.get("subagentPair"):
        return None
    worker = entry.get("localWorker")
    if not isinstance(worker, dict) or not worker.get("members") or not str(worker.get("prompt") or "").strip():
        return None
    return worker


def claude_binary() -> Optional[str]:
    """The Claude Code binary to run a worker with.

    `CLAUDE_CODE_EXECPATH` first: Claude Code sets it for every process it
    starts (this server included) to the exact binary running the session, so
    the worker runs on the same build and the same login. The VS Code extension
    ships that binary inside the extension and never puts it on PATH, so PATH
    is only the fallback for a terminal install."""
    path = os.environ.get("CLAUDE_CODE_EXECPATH")
    if path and os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return shutil.which("claude")


#: A worker runs until IT finishes — the platform's 240 s cap is exactly the
#: limit this exists to leave behind. The ceiling below only stops a wedged
#: process from holding the agent forever, and it fails loud when it fires.
LOCAL_WORKER_TIMEOUT_S = 1800


class _Finished:
    def __init__(self, stdout: str, stderr: str, returncode: int):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class LocalWorkerRunner:
    """Runs a subagent-mode wrapper's worker LOCALLY: a headless Claude Code
    session whose system prompt is the platform worker's own, whose ONLY tools
    are the integration's individual tools (served by this same script with
    `--only`, so every call still runs on Soleon under the person's
    connection), and whose answer is returned as the wrapper's result.

    Isolation is deliberate and each flag is load-bearing:
      * `--setting-sources ""` — no user/project/local settings, so none of the
        plugin's hooks fire inside the worker (it would otherwise record its
        own "turns" and sync nothing into the draft);
      * `--tools ""` — no built-ins: no Bash, Read, WebSearch;
      * `--strict-mcp-config` + one server — none of the parent's MCP servers;
      * `--permission-mode dontAsk` + `--allowedTools mcp__soleon-agent-tools`
        — its own tools run, anything else is refused without a prompt nobody
        would see;
      * `--no-session-persistence` — no transcript left behind per call;
      * `--turn` on its tools server — its platform calls land in the agent's
        turn, not one the worker would mint for itself.

    The Per Message Token Budget covers the worker too (`soleon_message_budget`):
    with a budget on it runs `--output-format stream-json`, this runner books
    each of its model calls in the message's ledger as it arrives, and
    `--settings` gives it ONLY the budget's PreToolUse hook, which refuses its
    tool calls once the message is spent.
    """

    def __init__(self, *, slug: str, tools_path: str, server_url: str, model: str,
                 app_env: str = "dev", credentials: Optional[str] = None, binary: Optional[str] = None,
                 runner=None, popen=None, timeout_s: float = LOCAL_WORKER_TIMEOUT_S):
        self.slug = slug
        self.tools_path = os.path.abspath(tools_path)
        self.server_url = server_url
        self.model = model
        self.app_env = app_env
        self.credentials = credentials
        self.binary = binary
        self.timeout_s = timeout_s
        self._run = runner or subprocess.run
        self._popen = popen or subprocess.Popen
        self.agent_dir = os.path.dirname(self.tools_path)

    def command(self, entry: Dict[str, Any], task: str, *, approved: bool, turn: Optional[str] = None,
                budget_run: Optional[str] = None) -> List[str]:
        """`budget_run` = this run's id in the message's ledger, when the
        message budget is on."""
        worker = local_worker_of(entry) or {}
        server_args = [os.path.abspath(__file__), "--slug", self.slug, "--tools", self.tools_path,
                       "--server-url", self.server_url, "--app-env", self.app_env,
                       "--only", ",".join(worker["members"])]
        if turn:
            server_args += ["--turn", turn]
        if self.credentials:
            server_args += ["--credentials", self.credentials]
        if approved:
            server_args.append("--pre-approved")
        mcp = {"mcpServers": {"soleon-agent-tools": {
            "type": "stdio", "command": sys.executable, "args": server_args}}}
        cmd = [self.binary or "", "-p", task,
               "--model", self.model,
               "--system-prompt", str(worker["prompt"]),
               "--setting-sources", "",
               "--tools", "",
               "--strict-mcp-config", "--mcp-config", json.dumps(mcp),
               "--allowedTools", "mcp__soleon-agent-tools",
               "--permission-mode", "dontAsk",
               "--no-session-persistence"]
        if budget_run and turn:
            cmd += ["--output-format", "stream-json", "--verbose", "--settings",
                    json.dumps(message_budget.helper_hook_settings(self.agent_dir, turn, budget_run))]
        else:
            cmd += ["--output-format", "json"]
        cap = worker.get("maxIterations")
        if isinstance(cap, int) and cap > 0:
            cmd += ["--max-turns", str(cap)]
        return cmd

    def run(self, entry: Dict[str, Any], task: str, *, approved: bool,
            turn: Optional[str] = None) -> Dict[str, Any]:
        name = entry.get("name")
        if not task.strip():
            return _text_result("{} needs a `prompt`: say what to look up or do.".format(name), True)
        if not self.binary:
            self.binary = claude_binary()
        if not self.binary:
            return _text_result(
                "{} runs its helper locally with Claude Code, and no Claude Code binary was found "
                "(CLAUDE_CODE_EXECPATH is unset and `claude` is not on PATH). Run this agent from "
                "Claude Code, or install the CLI.".format(name), True)
        _log("{} → running its helper locally ({} tool(s))".format(name, len((local_worker_of(entry) or {}).get("members") or [])))
        budget_run = None
        if turn and message_budget.budget_of(Path(self.agent_dir)) > 0:
            budget_run = __import__("uuid").uuid4().hex[:12]
        cmd = self.command(entry, task, approved=approved, turn=turn, budget_run=budget_run)
        if budget_run:
            proc = self._stream(cmd, turn, budget_run)
        else:
            try:
                proc = self._run(cmd, capture_output=True, text=True, timeout=self.timeout_s,
                                 stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                proc = None
        if proc is None:
            return _text_result("{}'s local helper was still running after {} s and was stopped.".format(
                name, int(self.timeout_s)), True)
        return self._render(name, proc)

    def _stream(self, cmd: List[str], turn: str, run_id: str) -> Any:
        """Run a budgeted helper, booking each model call in the message's
        ledger as it streams in. Returns a finished-process-like object whose
        stdout is the final `result` event (what `_render` reads), or None on
        timeout. The finished run's `modelUsage` replaces the running sum — the
        stream's per-call output counts are the first chunk's, not the final."""
        import threading
        ledger = message_budget.Ledger(Path(self.agent_dir))
        calls: Dict[str, int] = {}
        result: Dict[str, Any] = {}
        tail: List[str] = []
        err: List[str] = []
        proc = self._popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           stdin=subprocess.DEVNULL)

        def read_out():
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    tail.append(line)
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    result.update(event)
                    continue
                booked = message_budget.stream_call_tokens(event)
                if booked:
                    calls[booked[0]] = booked[1]
                    ledger.note_helper(turn, run_id, sum(calls.values()))

        def read_err():
            err.append(proc.stderr.read())

        readers = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
        for t in readers:
            t.start()
        try:
            returncode = proc.wait(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            returncode = None
        for t in readers:
            t.join(timeout=5)
        ledger.note_helper(turn, run_id,
                           message_budget.result_tokens(result) if result.get("modelUsage") else sum(calls.values()))
        if returncode is None:
            return None
        stdout = json.dumps(result) if result else "".join(tail)
        return _Finished(stdout=stdout, stderr="".join(err), returncode=returncode)

    @staticmethod
    def _parse(proc: Any) -> Optional[Dict[str, Any]]:
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out.splitlines()[-1]) if out else None
        except (json.JSONDecodeError, IndexError):
            data = None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _render(name: Any, proc: Any) -> Dict[str, Any]:
        out = (proc.stdout or "").strip()
        data = LocalWorkerRunner._parse(proc)
        if not isinstance(data, dict):
            err = (proc.stderr or out or "no output").strip()[-800:]
            return _text_result("{}'s local helper failed (exit {}): {}".format(name, proc.returncode, err), True)
        result = str(data.get("result") or "").strip()
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            why = result or str(data.get("subtype") or "error")
            return _text_result("{}'s local helper stopped without an answer: {}".format(name, why), True)
        return _text_result(result or "(the helper finished with an empty answer)")


class AgentToolsServer:
    def __init__(self, slug: str, tools: List[Dict[str, Any]], client: SoleonMcpClient,
                 app_env: str = "dev", poll_interval_s: float = POLL_INTERVAL_S,
                 conversation: Optional[ConversationTracker] = None,
                 turn: Optional[TurnTracker] = None,
                 local_workers: Optional["LocalWorkerRunner"] = None,
                 pre_approved: bool = False, include_members: bool = False):
        self.slug = slug
        self.app_env = app_env
        self.tools = tools
        self.published = published_tools(tools, include_members=include_members)
        self._gated = {t["name"] for t in tools if t.get("approval")}
        self._by_name = {t["name"]: t for t in tools}
        #: published MCP name → the name the platform calls it by
        self._platform_name = {published_name(t): t["name"] for t in tools}
        self.client = client
        self.poll_interval_s = poll_interval_s
        self.conversation = conversation or ConversationTracker(None)
        self.turn = turn or TurnTracker(None)
        self.local_workers = local_workers
        #: Set only on a LOCAL WORKER's own server, and only when the person
        #: approved the wrapper call that started it: the platform's pair
        #: approval likewise covers the calls its worker makes, and a headless
        #: worker has nobody to ask.
        self.pre_approved = pre_approved

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name not in {t["name"] for t in self.published}:
            return _text_result("Unknown tool {!r} — not one of this agent's external tools.".format(name), True)
        args = dict(arguments or {})
        approved = bool(args.pop("approved", False)) or self.pre_approved
        platform_name = self._platform_name.get(name, name)
        entry = self._by_name.get(platform_name) or {}
        if self.local_workers is not None and local_worker_of(entry) is not None:
            # A subagent-mode wrapper (`mcp_gmail_read`): its worker runs HERE,
            # as a headless Claude Code session over the integration's own
            # tools, instead of as an opaque loop on Soleon.
            if entry.get("approval") and not approved:
                return self.render(name, {"state": "error", "error": "approval_required"})
            return self.local_workers.run(entry, str(args.get("prompt") or ""), approved=approved,
                                          turn=self.turn.current())
        try:
            envelope = self.client.call_tool("call_agent_tool", {
                "slug": self.slug, "app_env": self.app_env,
                "name": platform_name, "args": args, "approved": approved,
                "conversation": self.conversation.current(),
                "turn": self.turn.current(),
            })
            envelope = await_control(
                self.client, self.slug, envelope, app_env=self.app_env,
                interval_s=self.poll_interval_s,
                on_wait=lambda env: _log("{} still running on the platform (call_id {})".format(
                    name, env.get("call_id"))),
            )
        except MissingTokenError as exc:
            return _text_result(str(exc), True)
        except AuthError as exc:
            return _text_result(str(exc), True)
        except ToolError as exc:
            payload = exc.payload or {}
            text = describe_error(payload) if payload.get("error") else str(exc)
            return _text_result("Soleon refused the call — " + text, True)
        except TransportError as exc:
            return _text_result("Could not reach Soleon: {}. Retry once the platform is reachable.".format(exc), True)
        return self._with_identity_hint(name, args, self.render(name, envelope))

    def _schema_for(self, name: str) -> Dict[str, Any]:
        for t in self.published:
            if t["name"] == name:
                return t.get("inputSchema") or {}
        return {}

    def _with_identity_hint(self, name: str, args: Dict[str, Any],
                            out: Dict[str, Any]) -> Dict[str, Any]:
        """A failed call that overrode the connection's own account gets the
        cause named. Only on failure — a call that worked needs no note."""
        if not _looks_failed(out):
            return out
        hint = identity_mismatch_hint(self._schema_for(name), args)
        if hint:
            out["content"].append({"type": "text", "text": hint})
        return out

    def render(self, name: str, envelope: Any) -> Dict[str, Any]:
        if not isinstance(envelope, dict):
            return _text_result(_render_tool_value(envelope))
        if is_platform_error(envelope):
            return _text_result(describe_error(envelope), True)
        state = envelope.get("state")
        if state == "done":
            result = envelope.get("result")
            value = result.get("result") if isinstance(result, dict) and "result" in result else result
            return _text_result(_render_tool_value(value))
        if state == "error":
            err = str(envelope.get("error") or "error")
            if err == "approval_required":
                text = (
                    "approval_required: {!r} needs the person's explicit approval before it runs. "
                    "Ask them plainly what this call will do and wait for a clear yes; then call "
                    "{} again with approved=true. If they decline, do not call it."
                ).format(name, name)
            else:
                text = describe_error(envelope)
            return _text_result(text, True)
        return _text_result(_render_tool_value(envelope))


# ---------------------------------------------------------------------------
# JSON-RPC loop
# ---------------------------------------------------------------------------

def handle_message(server: AgentToolsServer, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        version = params.get("protocolVersion") or PROTOCOL_VERSION
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }}
    if method in ("notifications/initialized", "notifications/cancelled", "notifications/roots/list_changed"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": server.published}}
    if method == "tools/call":
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "arguments must be an object"}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": server.call(params.get("name") or "", arguments)}
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Method not found: {}".format(method)}}


def serve(server: AgentToolsServer, stdin=None, stdout=None) -> None:
    inp = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    for raw in inp:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _log("dropped a non-JSON line")
            continue
        if not isinstance(msg, dict):
            continue
        try:
            resp = handle_message(server, msg)
        except Exception as exc:  # noqa: BLE001 — one bad call must not kill the server
            _log("internal error on {}: {}".format(msg.get("method"), type(exc).__name__))
            resp = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(exc)}}
        if resp is not None:
            out.write((json.dumps(resp) + "\n").encode("utf-8"))
            out.flush()


def _pulled_model(agent_dir: str) -> str:
    """The local model the agent was pulled onto — a worker runs on the same
    one, as the platform's worker runs on the agent's model family."""
    try:
        with open(os.path.join(agent_dir, "pull.json"), "r", encoding="utf-8") as fh:
            model = str((json.load(fh) or {}).get("model") or "")
    except (OSError, ValueError):
        model = ""
    return model or "sonnet"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--slug", required=True)
    ap.add_argument("--tools", required=True, help="tools.json written by /pull-agent")
    ap.add_argument("--server-url", required=True, help="the Soleon MCP server URL")
    ap.add_argument("--credentials", default=None, help="Claude Code credentials file (default ~/.claude/.credentials.json)")
    ap.add_argument("--app-env", default="dev")
    ap.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_S)
    ap.add_argument("--only", default=None,
                    help="comma-separated tool names: publish ONLY these (a configured helper's subset)")
    ap.add_argument("--pre-approved", action="store_true",
                    help="a local WORKER's server whose wrapper call the person approved: run gated calls approved")
    ap.add_argument("--turn", default=None,
                    help="a local WORKER's server: the agent turn its calls belong to")
    ap.add_argument("--model", default=None,
                    help="the local model a subagent-mode wrapper's worker runs on (default: pull.json's)")
    args = ap.parse_args(argv)
    tools = load_tools(args.tools)
    if args.only is not None:
        # A helper subagent's tool subset is applied HERE, in the server its
        # agent file declares inline, because a subagent's `tools:` frontmatter
        # is resolved against the PARENT session's pool before any inline
        # server connects — an `mcp__soleon-agent-tools__<name>` entry there can
        # never match, and Claude Code refuses to spawn the agent ("would be
        # spawned with zero tools"). Verified against Claude Code 2.1.257.
        wanted = [n.strip() for n in args.only.split(",") if n.strip()]
        known = {t["name"] for t in tools}
        unknown = [n for n in wanted if n not in known]
        if unknown:
            _log("--only names tools that are not in {}: {}".format(args.tools, ", ".join(unknown)))
            return 2
        keep = set(wanted)
        tools = [t for t in tools if t["name"] in keep]
    client = SoleonMcpClient(args.server_url, credentials_path=args.credentials)
    # The conversation id lives next to tools.json (the pulled agent's dir),
    # so every restart of this server for the same agent finds it.
    agent_dir = os.path.dirname(os.path.abspath(args.tools))
    tracker = ConversationTracker(os.path.join(agent_dir, ".local-conversation.json"))
    # The turn id lives there too: written by the SubagentStart hook for this
    # run, read by the SubagentStop hook when it records the prompt + answer.
    turn = TurnTracker(None, fixed=args.turn) if args.turn else TurnTracker(os.path.join(agent_dir, TURN_FILE_NAME))
    workers = None
    if args.only is None:
        # Only the AGENT'S server runs wrappers' workers. A worker's own server
        # (`--only`) holds an integration's individual tools and never a
        # wrapper, so a worker can never start another worker.
        model = args.model or _pulled_model(agent_dir)
        workers = LocalWorkerRunner(slug=args.slug, tools_path=args.tools, server_url=args.server_url,
                                    model=model, app_env=args.app_env, credentials=args.credentials)
    server = AgentToolsServer(args.slug, tools, client, app_env=args.app_env,
                              poll_interval_s=args.poll_interval, conversation=tracker, turn=turn,
                              local_workers=workers, pre_approved=args.pre_approved,
                              include_members=args.only is not None)
    _log("serving {} external tool(s) for {} via {}".format(len(server.published), args.slug, client.server_url))
    serve(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
