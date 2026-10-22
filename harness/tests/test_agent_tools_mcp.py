"""bin/soleon_agent_tools_mcp.py + bin/soleon_mcp_client.py against a FAKE Soleon
MCP on localhost (http.server in a thread).

Covers: tools/list publishes only external tools under their own names/schemas
(+ the approved flag on gated ones); a done envelope returns the tool's own
result; pending → get_agent_tool_result polling until done (no timeout);
approval_required → isError worded to ask the person; 401 → one refresh_token
grant → credentials file rewritten (mode 0600) → retry; missing token → the
reconnect message; 429 backoff."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from _local_emulation_fixtures import BIN, SLUG, TOOLS, tools_envelope

sys.path.insert(0, str(BIN))
import soleon_agent_tools_mcp as shim  # noqa: E402
import soleon_mcp_client as client_mod  # noqa: E402

GOOD = "good-token"
STALE = "stale-token"
REFRESH = "refresh-me"


class FakeSoleon(BaseHTTPRequestHandler):
    """Stateless MCP: POST /mcp (tools/call) + OAuth AS metadata + token endpoint."""

    state = {"calls": [], "pending_polls": 2, "rate_limit_left": 0, "tokens": {GOOD}}

    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/.well-known/oauth-authorization-server":
            base = "http://{}:{}".format(*self.server.server_address)
            return self._json(200, {"issuer": base, "token_endpoint": base + "/token"})
        self._json(404, {"error": "nope"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path == "/token":
            form = dict(p.split("=", 1) for p in raw.decode().split("&"))
            self.state["calls"].append(("token", form))
            if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == REFRESH and form.get("client_id") == "cli-1":
                self.state["tokens"].add("refreshed-token")
                return self._json(200, {"access_token": "refreshed-token", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "refresh-2"})
            return self._json(400, {"error": "invalid_grant"})
        if self.path != "/mcp":
            return self._json(404, {"error": "nope"})
        auth = self.headers.get("Authorization", "")
        if self.state["rate_limit_left"] > 0:
            self.state["rate_limit_left"] -= 1
            return self._json(429, {"message": "slow down"})
        if auth.replace("Bearer ", "") not in self.state["tokens"]:
            return self._json(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Bearer resource_metadata="x"'})
        assert self.headers.get("MCP-Protocol-Version") == "2025-03-26"
        assert "text/event-stream" in self.headers.get("Accept", "")
        req = json.loads(raw)
        assert req["method"] == "tools/call"
        name = req["params"]["name"]
        args = req["params"]["arguments"]
        self.state["calls"].append((name, args))
        envelope = self.dispatch(name, args)
        as_sse = name == "get_agent_tool_result"
        text = json.dumps(envelope)
        result = {"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": text}]}}
        if as_sse:  # exercise the event-stream parser on the poll path
            body = ("event: message\ndata: " + json.dumps(result) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(200, result)

    def dispatch(self, name, args):
        if name == "call_agent_tool":
            assert args["slug"] == SLUG and args["app_env"] == "dev"
            tool = args["name"]
            if tool == "custom_echo-server_read":
                return {"status": 200, "state": "done", "call_id": "lc_" + "1" * 24, "op": "tool_call",
                        "result": {"tool": tool, "result": {"echo": args["args"]["prompt"]}, "elapsed_ms": 12}}
            if tool == "web_search":
                return {"status": 202, "state": "pending", "call_id": "lc_" + "2" * 24, "op": "tool_call",
                        "next_action": "poll"}
            if tool == "custom_echo-server_write":
                if not args.get("approved"):
                    return {"status": 200, "state": "error", "call_id": "lc_" + "3" * 24, "op": "tool_call",
                            "error": "approval_required", "tool": tool,
                            "detail": "'custom_echo-server_write' requires human approval before it runs.",
                            "next_action": "Ask the person, re-send with approved=true."}
                assert "approved" not in args["args"]
                return {"status": 200, "state": "done", "call_id": "lc_" + "4" * 24, "op": "tool_call",
                        "result": {"tool": tool, "result": "wrote: " + args["args"]["prompt"], "elapsed_ms": 5}}
            return {"status": 200, "state": "error", "error": "unknown_tool", "detail": "no such tool",
                    "next_action": "Call list_agent_tools."}
        if name == "get_agent_tool_result":
            assert args["call_id"] == "lc_" + "2" * 24
            if self.state["pending_polls"] > 0:
                self.state["pending_polls"] -= 1
                return {"status": 202, "state": "pending", "call_id": args["call_id"], "op": "tool_call"}
            return {"status": 200, "state": "done", "call_id": args["call_id"], "op": "tool_call",
                    "result": {"tool": "web_search", "result": [{"title": "Paris"}], "elapsed_ms": 40000}}
        return {"status": 404, "error": "platform_error", "detail": "unknown tool " + name, "next_action": "-"}


@pytest.fixture()
def soleon():
    FakeSoleon.state = {"calls": [], "pending_polls": 2, "rate_limit_left": 0, "tokens": {GOOD}}
    httpd = HTTPServer(("127.0.0.1", 0), FakeSoleon)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    url = "http://127.0.0.1:{}/mcp".format(httpd.server_address[1])
    yield url, FakeSoleon.state
    httpd.shutdown()


def write_credentials(path: Path, url: str, token: str, **extra) -> None:
    entry = {"serverName": "soleon-agent-toolkit", "serverUrl": url, "accessToken": token, **extra}
    data = {"claudeAiOauth": {"accessToken": "unrelated"}, "mcpOAuth": {
        "figma|abc": {"serverName": "figma", "serverUrl": "https://mcp.figma.com/mcp", "accessToken": "fig"},
        "soleon-agent-toolkit|def": entry,
    }}
    path.write_text(json.dumps(data, indent=2))
    os.chmod(str(path), 0o600)


def make_server(url, creds, sleeps=None):
    client = client_mod.SoleonMcpClient(url, credentials_path=str(creds), token_override="",
                                        sleep=(sleeps.append if sleeps is not None else (lambda s: None)))
    return shim.AgentToolsServer(SLUG, TOOLS, client, poll_interval_s=0.01)


def test_tools_list_publishes_external_tools_with_own_schema_and_approval_flag(soleon, tmp_path):
    url, _ = soleon
    creds = tmp_path / "creds.json"
    write_credentials(creds, url, GOOD)
    server = make_server(url, creds)
    resp = shim.handle_message(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert set(tools) == {"web_search", "custom_echo-server_read", "custom_echo-server_write"}
    assert tools["web_search"]["inputSchema"] == TOOLS[2]["inputSchema"]
    assert tools["web_search"]["description"] == "Search the web."
    gated = tools["custom_echo-server_write"]
    assert gated["inputSchema"]["properties"]["approved"]["type"] == "boolean"
    assert "prompt" in gated["inputSchema"]["properties"]
    assert gated["description"].endswith(shim.APPROVAL_NOTE)
    assert "approved" not in tools["custom_echo-server_read"]["inputSchema"]["properties"]


def test_done_envelope_returns_the_tools_own_result(soleon, tmp_path):
    url, state = soleon
    creds = tmp_path / "creds.json"
    write_credentials(creds, url, GOOD)
    server = make_server(url, creds)
    out = server.call("custom_echo-server_read", {"prompt": "hello"})
    assert "isError" not in out
    assert json.loads(out["content"][0]["text"]) == {"echo": "hello"}
    name, args = state["calls"][-1]
    assert name == "call_agent_tool"
    conversation = args.pop("conversation")
    assert re.fullmatch(r"[0-9a-f]{24}", conversation)  # one platform session per local conversation
    turn = args.pop("turn")
    assert re.fullmatch(r"[0-9a-f]{12}", turn)  # one platform turn per subagent run
    assert args == {"slug": SLUG, "app_env": "dev", "name": "custom_echo-server_read",
                    "args": {"prompt": "hello"}, "approved": False}
    # every call of this process carries the SAME ids
    server.call("custom_echo-server_read", {"prompt": "again"})
    assert state["calls"][-1][1]["conversation"] == conversation
    assert state["calls"][-1][1]["turn"] == turn


def test_turn_tracker_adopts_a_fresh_hook_file_and_mints_otherwise(tmp_path):
    """The SubagentStart hook writes the turn file for THIS run; the server
    joins it. A stale file (an earlier run's) or none → the server mints its
    own and leaves it for the stop hook."""
    path = tmp_path / ".local-turn.json"
    clock = {"t": 1_000_000.0}
    now = lambda: clock["t"]  # noqa: E731
    # fresh hook file, written 2s before this server started
    path.write_text(json.dumps({"turn": "hookturn0001", "agentId": "a1", "startedAt": clock["t"] - 2, "source": "hook"}))
    tracker = shim.TurnTracker(str(path), now=now, process_started_at=clock["t"])
    assert tracker.current() == "hookturn0001" and tracker.current() == "hookturn0001"
    assert json.loads(path.read_text())["source"] == "hook"  # untouched
    # stale file from an earlier run → mint, and persist for the stop hook
    path.write_text(json.dumps({"turn": "oldturn00001", "agentId": "a0", "startedAt": clock["t"] - 3600, "source": "hook"}))
    minted = shim.TurnTracker(str(path), now=now, process_started_at=clock["t"]).current()
    assert minted != "oldturn00001" and re.fullmatch(r"[0-9a-f]{12}", minted)
    assert json.loads(path.read_text()) == {"turn": minted, "agentId": None, "startedAt": clock["t"], "source": "server"}
    # no file at all
    path.unlink()
    fresh = shim.TurnTracker(str(path), now=now, process_started_at=clock["t"]).current()
    assert re.fullmatch(r"[0-9a-f]{12}", fresh) and json.loads(path.read_text())["turn"] == fresh
    assert shim.TurnTracker(None).current()  # no persistence: one id per process


def test_conversation_tracker_reuses_the_id_within_the_idle_window_and_mints_after(tmp_path):
    """The tool server restarts with every subagent run, so the id is kept on
    disk and reused while the agent has been idle < 30 min (the platform's own
    session window); a longer gap is a new conversation → a new session."""
    path = tmp_path / ".local-conversation.json"
    clock = {"t": 1_000_000.0}
    first = shim.ConversationTracker(str(path), idle_s=1800, now=lambda: clock["t"])
    a = first.current()
    assert re.fullmatch(r"[0-9a-f]{24}", a)
    assert json.loads(path.read_text()) == {"conversation": a, "lastCallAt": clock["t"]}
    clock["t"] += 600  # 10 min later, a NEW process (resumed subagent)
    second = shim.ConversationTracker(str(path), idle_s=1800, now=lambda: clock["t"])
    assert second.current() == a
    assert json.loads(path.read_text())["lastCallAt"] == clock["t"]  # touched
    clock["t"] += 1799  # still inside the window measured from the LAST call
    assert shim.ConversationTracker(str(path), idle_s=1800, now=lambda: clock["t"]).current() == a
    clock["t"] += 1801  # idle > 30 min → new conversation
    b = shim.ConversationTracker(str(path), idle_s=1800, now=lambda: clock["t"]).current()
    assert b != a and json.loads(path.read_text())["conversation"] == b
    # a process keeps its id for its whole life even as the clock moves on
    third = shim.ConversationTracker(str(path), idle_s=1800, now=lambda: clock["t"])
    c = third.current()
    clock["t"] += 5000
    assert third.current() == c
    # unreadable or missing state → a fresh id, never a crash
    path.write_text("{not json")
    assert re.fullmatch(r"[0-9a-f]{24}", shim.ConversationTracker(str(path), now=lambda: clock["t"]).current())
    assert shim.ConversationTracker(None).current()  # no persistence: one id per process


def test_pending_is_polled_until_done_without_giving_up(soleon, tmp_path):
    url, state = soleon
    creds = tmp_path / "creds.json"
    write_credentials(creds, url, GOOD)
    sleeps = []
    server = make_server(url, creds, sleeps)
    out = server.call("web_search", {"query": "capital of France"})
    assert "isError" not in out
    assert json.loads(out["content"][0]["text"]) == [{"title": "Paris"}]
    names = [c[0] for c in state["calls"]]
    assert names == ["call_agent_tool", "get_agent_tool_result", "get_agent_tool_result", "get_agent_tool_result"]
    assert len(sleeps) == 3 and all(s == 0.01 for s in sleeps)


def test_approval_required_is_an_error_result_that_asks_then_retries_with_approved(soleon, tmp_path):
    url, state = soleon
    creds = tmp_path / "creds.json"
    write_credentials(creds, url, GOOD)
    server = make_server(url, creds)
    refused = server.call("custom_echo-server_write", {"prompt": "send it"})
    assert refused["isError"] is True
    text = refused["content"][0]["text"]
    assert text.startswith("approval_required:") and "approved=true" in text and "Ask them" in text
    ok = server.call("custom_echo-server_write", {"prompt": "send it", "approved": True})
    assert "isError" not in ok and ok["content"][0]["text"] == "wrote: send it"
    _, args = state["calls"][-1]
    assert args["approved"] is True and args["args"] == {"prompt": "send it"}  # approved is lifted out of args


def test_401_triggers_one_refresh_and_a_retry_and_rewrites_the_credentials_file(soleon, tmp_path):
    url, state = soleon
    creds = tmp_path / "creds.json"
    write_credentials(creds, url, STALE, refreshToken=REFRESH, clientId="cli-1", expiresAt=1)
    server = make_server(url, creds)
    out = server.call("custom_echo-server_read", {"prompt": "after refresh"})
    assert "isError" not in out, out
    assert json.loads(out["content"][0]["text"]) == {"echo": "after refresh"}
    token_calls = [c for c in state["calls"] if c[0] == "token"]
    assert len(token_calls) == 1
    assert token_calls[0][1]["grant_type"] == "refresh_token"
    saved = json.loads(creds.read_text())
    entry = saved["mcpOAuth"]["soleon-agent-toolkit|def"]
    assert entry["accessToken"] == "refreshed-token" and entry["refreshToken"] == "refresh-2"
    assert entry["serverUrl"] == url and entry["clientId"] == "cli-1"
    assert saved["mcpOAuth"]["figma|abc"]["accessToken"] == "fig"  # untouched
    assert saved["claudeAiOauth"] == {"accessToken": "unrelated"}
    assert stat.S_IMODE(os.stat(str(creds)).st_mode) == 0o600


def test_401_without_refresh_material_surfaces_the_reconnect_instruction(soleon, tmp_path):
    url, state = soleon
    creds = tmp_path / "creds.json"
    write_credentials(creds, url, "bogus")
    server = make_server(url, creds)
    out = server.call("custom_echo-server_read", {"prompt": "x"})
    assert out["isError"] is True
    text = out["content"][0]["text"]
    assert "401" in text and "/mcp" in text and "soleon server" in text
    assert "bogus" not in text  # never echo a token
    assert not [c for c in state["calls"] if c[0] == "token"]


def test_missing_token_tells_the_user_to_connect_the_server(soleon, tmp_path):
    url, state = soleon
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"mcpOAuth": {"other|x": {"serverUrl": "https://elsewhere/mcp", "accessToken": "t"}}}))
    server = make_server(url, creds)
    out = server.call("web_search", {"query": "x"})
    assert out["isError"] is True
    assert out["content"][0]["text"] == client_mod.RECONNECT_INSTRUCTION
    assert state["calls"] == []  # no network call without a token
    # and a credentials file that does not exist at all
    server2 = make_server(url, tmp_path / "absent.json")
    assert server2.call("web_search", {"query": "x"})["isError"] is True


def test_token_env_override_wins(soleon, tmp_path, monkeypatch):
    url, _ = soleon
    monkeypatch.setenv(client_mod.TOKEN_ENV_VAR, GOOD)
    client = client_mod.SoleonMcpClient(url, credentials_path=str(tmp_path / "absent.json"))
    env = client.call_tool("call_agent_tool", {"slug": SLUG, "app_env": "dev", "name": "custom_echo-server_read",
                                               "args": {"prompt": "env"}, "approved": False})
    assert env["state"] == "done"


def test_429_is_retried_with_backoff(soleon, tmp_path):
    url, state = soleon
    state["rate_limit_left"] = 2
    creds = tmp_path / "creds.json"
    write_credentials(creds, url, GOOD)
    sleeps = []
    server = make_server(url, creds, sleeps)
    out = server.call("custom_echo-server_read", {"prompt": "retry"})
    assert "isError" not in out
    assert sleeps[:2] == [0.5, 1.0]


def test_same_host_fallback_picks_the_entry(soleon, tmp_path):
    url, _ = soleon
    creds = tmp_path / "creds.json"
    write_credentials(creds, url.replace("/mcp", "/other"), GOOD)
    server = make_server(url, creds)
    assert "isError" not in server.call("custom_echo-server_read", {"prompt": "host"})


def test_stdio_end_to_end(soleon, tmp_path):
    """The real process: tools/list + one tools/call over stdin/stdout."""
    url, _ = soleon
    creds = tmp_path / "creds.json"
    write_credentials(creds, url, GOOD)
    tools_json = tmp_path / "tools.json"
    tools_json.write_text(json.dumps(tools_envelope()))
    proc = subprocess.Popen(
        [sys.executable, str(BIN / "soleon_agent_tools_mcp.py"), "--slug", SLUG, "--tools", str(tools_json),
         "--server-url", url, "--credentials", str(creds), "--poll-interval", "0.01"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"},
    )
    try:
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "q"}}},
        ]
        out, err = proc.communicate(input="".join(json.dumps(m) + "\n" for m in msgs).encode(), timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    lines = [json.loads(l) for l in out.decode().splitlines() if l.strip()]
    assert [l["id"] for l in lines] == [1, 2, 3], err.decode()
    assert lines[0]["result"]["serverInfo"]["name"] == "soleon-agent-tools"
    assert {t["name"] for t in lines[1]["result"]["tools"]} == {"web_search", "custom_echo-server_read", "custom_echo-server_write"}
    assert json.loads(lines[2]["result"]["content"][0]["text"]) == [{"title": "Paris"}]
    assert GOOD not in err.decode()


# ---------------------------------------------------------------------------
# subagent-mode wrappers run their worker LOCALLY (not as an opaque loop on Soleon)
# ---------------------------------------------------------------------------

WRAPPER = {
    "name": "mcp_gmail_read", "kind": "external", "subagentPair": True, "approval": False,
    "description": "Read Gmail.", "serverId": "gmail",
    "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
    # the platform's shape (containers/shared/local_control.pair_worker_listing):
    # members qualified by their wrapper, published by their display name
    "localWorker": {"members": ["mcp_gmail_read::search_emails", "mcp_gmail_read::read_email"],
                    "prompt": "You read Gmail.", "maxIterations": 12, "role": "read"},
}
WRITE_WRAPPER = dict(WRAPPER, name="mcp_gmail_write", approval=True,
                     localWorker={"members": ["mcp_gmail_write::send_email"], "prompt": "You send Gmail.",
                                  "maxIterations": 8, "role": "write"})
MEMBERS = [
    {"name": "mcp_gmail_read::search_emails", "displayName": "search_emails", "kind": "pair_member",
     "pair": "mcp_gmail_read", "approval": False,
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "mcp_gmail_read::read_email", "displayName": "read_email", "kind": "pair_member",
     "pair": "mcp_gmail_read", "approval": False,
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
    {"name": "mcp_gmail_write::send_email", "displayName": "send_email", "kind": "pair_member",
     "pair": "mcp_gmail_write", "approval": True,
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}}},
]


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _runner(tmp_path, answer=None, calls=None, **kw):
    calls = calls if calls is not None else []
    # These tests are about the worker's isolation and approvals; the message
    # budget's streaming run has its own tests (test_message_budget.py).
    (tmp_path / "config.json").write_text(json.dumps({"loop": {"tokenBudgetEnabled": False}}))

    def fake_run(cmd, **opts):
        calls.append(cmd)
        return answer if answer is not None else _Proc(json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "3 unread from investors"}))
    return shim.LocalWorkerRunner(slug=SLUG, tools_path=str(tmp_path / "tools.json"),
                                  server_url="https://mcp-dev.oppizi.com/mcp", model="sonnet",
                                  binary="/opt/claude", runner=fake_run, **kw), calls


def _server(tmp_path, runner, tools=None):
    return shim.AgentToolsServer(SLUG, tools or [WRAPPER, WRITE_WRAPPER] + MEMBERS, client=None,
                                 local_workers=runner)


def test_the_agent_never_sees_an_integrations_individual_tools():
    """On the platform the AGENT holds the wrapper and only its worker holds
    Gmail's tools. The local agent's surface must be the same."""
    names = [t["name"] for t in shim.published_tools([WRAPPER, WRITE_WRAPPER] + MEMBERS)]
    assert names == ["mcp_gmail_read", "mcp_gmail_write"]


def test_a_workers_own_server_publishes_the_integration_tools():
    names = [t["name"] for t in shim.published_tools(MEMBERS, include_members=True)]
    assert names == ["search_emails", "read_email", "send_email"]


def test_a_wrapper_call_runs_its_worker_locally_and_returns_its_answer(tmp_path):
    runner, calls = _runner(tmp_path)
    out = _server(tmp_path, runner).call("mcp_gmail_read", {"prompt": "what came in today?"})
    assert not out.get("isError")
    assert out["content"][0]["text"] == "3 unread from investors"
    assert len(calls) == 1


def test_the_worker_is_isolated_and_holds_only_the_integrations_tools(tmp_path):
    """Every flag is load-bearing: no settings (so no plugin hooks record fake
    turns), no built-ins, none of the parent's MCP servers, nothing prompted."""
    runner, _ = _runner(tmp_path)
    cmd = runner.command(WRAPPER, "what came in today?", approved=False)
    assert cmd[0] == "/opt/claude" and cmd[1:3] == ["-p", "what came in today?"]
    flags = dict(zip(cmd, cmd[1:]))
    assert flags["--system-prompt"] == "You read Gmail."       # the platform worker's own prompt
    assert flags["--model"] == "sonnet"
    assert flags["--setting-sources"] == "" and flags["--tools"] == ""
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert flags["--permission-mode"] == "dontAsk"
    assert flags["--allowedTools"] == "mcp__soleon-agent-tools"
    assert flags["--max-turns"] == "12"                          # the platform's iteration cap
    server = json.loads(flags["--mcp-config"])["mcpServers"]["soleon-agent-tools"]
    args = server["args"]
    assert args[args.index("--only") + 1] == "mcp_gmail_read::search_emails,mcp_gmail_read::read_email"
    assert "--pre-approved" not in args


def test_a_worker_can_never_start_another_worker(tmp_path):
    """Its server is `--only` the integration's tools — never a wrapper — so the
    CLI builds it without a runner."""
    runner, _ = _runner(tmp_path)
    cmd = runner.command(WRAPPER, "x", approved=False)
    args = json.loads(dict(zip(cmd, cmd[1:]))["--mcp-config"])["mcpServers"]["soleon-agent-tools"]["args"]
    only = args[args.index("--only") + 1].split(",")
    assert "mcp_gmail_read" not in only and "mcp_gmail_write" not in only


def test_an_unapproved_write_wrapper_asks_first_and_starts_nothing(tmp_path):
    runner, calls = _runner(tmp_path)
    out = _server(tmp_path, runner).call("mcp_gmail_write", {"prompt": "reply to Sam"})
    assert out["isError"] and "approval_required" in out["content"][0]["text"]
    assert calls == []


def test_an_approved_write_wrapper_carries_the_approval_into_its_worker(tmp_path):
    """The platform's pair approval covers the calls its worker makes; a
    headless worker has nobody to ask, so the approval travels with it."""
    runner, calls = _runner(tmp_path)
    _server(tmp_path, runner).call("mcp_gmail_write", {"prompt": "reply to Sam", "approved": True})
    args = json.loads(dict(zip(calls[0], calls[0][1:]))["--mcp-config"])["mcpServers"]["soleon-agent-tools"]["args"]
    assert "--pre-approved" in args


class _CapturingClient:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _sleep(_seconds):  # await_control's poll wait; a done envelope never waits
        raise AssertionError("a terminal envelope must not be polled")

    def call_tool(self, name, args):
        self.calls.append((name, args))
        return {"state": "done", "result": {"result": "sent"}}


@pytest.mark.parametrize("pre_approved", [True, False])
def test_a_pre_approved_worker_server_sends_its_gated_calls_approved(pre_approved):
    client = _CapturingClient()
    server = shim.AgentToolsServer(SLUG, MEMBERS, client=client, pre_approved=pre_approved,
                                   include_members=True)
    server.call("send_email", {"to": "sam@example.com"})
    name, args = client.calls[0]
    # published as `send_email`, sent to the platform under its qualified name
    assert name == "call_agent_tool" and args["name"] == "mcp_gmail_write::send_email"
    assert args["approved"] is pre_approved


def test_a_wrapper_without_a_worker_description_still_runs_on_soleon():
    """An older platform sends no `localWorker`; that wrapper keeps its old
    behaviour rather than being run half-described."""
    bare = {k: v for k, v in WRAPPER.items() if k != "localWorker"}
    assert shim.local_worker_of(bare) is None
    assert shim.local_worker_of(WRAPPER)["members"] == ["mcp_gmail_read::search_emails",
                                                          "mcp_gmail_read::read_email"]


def test_no_claude_binary_is_a_loud_actionable_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_EXECPATH", raising=False)
    monkeypatch.setattr(shim.shutil, "which", lambda _name: None)
    runner, calls = _runner(tmp_path)
    runner.binary = None
    out = runner.run(WRAPPER, "x", approved=False)
    assert out["isError"] and "no Claude Code binary was found" in out["content"][0]["text"]
    assert calls == []


def test_the_binary_is_the_one_running_this_session(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", str(exe))
    assert shim.claude_binary() == str(exe)


def test_a_worker_that_stops_without_an_answer_says_so(tmp_path):
    runner, _ = _runner(tmp_path, answer=_Proc(json.dumps(
        {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})))
    out = runner.run(WRAPPER, "x", approved=False)
    assert out["isError"] and "error_max_turns" in out["content"][0]["text"]


def test_a_worker_that_crashes_reports_its_stderr(tmp_path):
    runner, _ = _runner(tmp_path, answer=_Proc("", "Error: not logged in", 1))
    out = runner.run(WRAPPER, "x", approved=False)
    assert out["isError"] and "not logged in" in out["content"][0]["text"] and "exit 1" in out["content"][0]["text"]


def test_a_wrapper_call_needs_a_prompt(tmp_path):
    runner, calls = _runner(tmp_path)
    out = runner.run(WRAPPER, "  ", approved=False)
    assert out["isError"] and calls == []


def test_a_workers_tool_is_published_by_the_name_its_model_calls_it():
    """`:` is not legal in an MCP tool name; the platform qualifies a worker's
    tool by its wrapper so two integrations sharing `search` stay distinct."""
    assert shim.published_name(MEMBERS[0]) == "search_emails"
    assert shim.published_name(WRAPPER) == "mcp_gmail_read"


# ---------------------------------------------------------------------------
# Configured subagents and workflows are TOOLS locally, as on the platform
# ---------------------------------------------------------------------------

DELEGATES = {
    "subagent_research": {"kind": "subagent", "name": "Researcher", "offered": True,
                          "description": "Researcher. Hand off when: Find sources.",
                          "system": "Search the web.", "model": "sonnet", "external": ["web_search", "web_fetch"]},
    "subagent_writer": {"kind": "subagent", "name": "Writer", "offered": False,
                        "description": "Writer. Hand off when: Write the brief.",
                        "system": "Write a brief.", "model": "opus", "external": []},
    "workflow_brief": {"kind": "workflow", "name": "Brief", "offered": True,
                       "description": "Brief (manager-led; members: Researcher, Writer). Runs when: a brief.",
                       "system": "# Workflow `workflow_brief`", "model": "sonnet",
                       "members": ["subagent_research", "subagent_writer"]},
}


def _delegate_runner(tmp_path, calls, **kw):
    (tmp_path / "config.json").write_text(json.dumps({"loop": {"tokenBudgetEnabled": False}}))
    (tmp_path / "delegates.json").write_text(json.dumps({"schemaVersion": 1, "delegates": DELEGATES}))

    def fake_run(cmd, **opts):
        calls.append(cmd)
        return _Proc(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                 "result": "brief with 3 checked sources"}))
    return shim.LocalDelegateRunner(delegates=shim.load_delegates(str(tmp_path)), slug=SLUG,
                                    tools_path=str(tmp_path / "tools.json"),
                                    server_url="https://mcp-dev.oppizi.com/mcp", model="sonnet",
                                    binary="/opt/claude", runner=fake_run, **kw)


def _mcp_config(cmd):
    return json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]


def test_the_agent_is_offered_its_subagents_and_workflows_as_task_tools(tmp_path):
    """research-desk-test, 2026-09-25: with no `workflow_brief` tool the local
    agent could only SAY it would run the workflow, and then answered with its
    own web_search. The tool must exist, with the platform's `task` schema."""
    runner = _delegate_runner(tmp_path, [])
    server = shim.AgentToolsServer(SLUG, [], client=None, delegate_runner=runner,
                                   delegate_ids=["subagent_research", "workflow_brief"])
    by_name = {t["name"]: t for t in server.published}
    assert set(by_name) == {"subagent_research", "workflow_brief"}
    assert by_name["workflow_brief"]["inputSchema"]["required"] == ["task"]
    assert by_name["workflow_brief"]["description"].startswith("Brief (manager-led")


def test_a_subagent_runs_headless_with_only_its_own_tools_and_returns_its_answer(tmp_path):
    calls = []
    runner = _delegate_runner(tmp_path, calls)
    server = shim.AgentToolsServer(SLUG, [], client=None, delegate_runner=runner,
                                   delegate_ids=["subagent_research"])
    out = server.call("subagent_research", {"task": "find sources on espresso machines"})
    assert out["content"][0]["text"] == "brief with 3 checked sources" and not out.get("isError")
    cmd = calls[0]
    assert cmd[:3] == ["/opt/claude", "-p", "find sources on espresso machines"]
    assert cmd[cmd.index("--system-prompt") + 1] == "Search the web."
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    servers = _mcp_config(cmd)
    assert set(servers) == {"soleon-workspace", "soleon-agent-tools"}
    targs = servers["soleon-agent-tools"]["args"]
    assert targs[targs.index("--only") + 1] == "web_search,web_fetch"
    assert "--delegates" not in targs  # a member cannot delegate further (maxDepth 1)
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__soleon-workspace,mcp__soleon-agent-tools"


def test_a_subagent_without_external_tools_gets_the_workspace_only(tmp_path):
    calls = []
    runner = _delegate_runner(tmp_path, calls)
    runner.run_delegate("subagent_writer", "write it")
    assert set(_mcp_config(calls[0])) == {"soleon-workspace"}
    assert calls[0][calls[0].index("--model") + 1] == "opus"


def test_a_workflow_runs_its_manager_over_its_members_as_tools(tmp_path):
    calls = []
    runner = _delegate_runner(tmp_path, calls)
    runner.run_delegate("workflow_brief", "sourced brief on budget espresso machines", turn="t_1")
    cmd = calls[0]
    assert cmd[cmd.index("--system-prompt") + 1] == "# Workflow `workflow_brief`"
    servers = _mcp_config(cmd)
    assert set(servers) == {"soleon-agent-tools"}  # members only: the manager does no work itself
    targs = servers["soleon-agent-tools"]["args"]
    assert targs[targs.index("--only") + 1] == ""
    assert targs[targs.index("--delegates") + 1] == "subagent_research,subagent_writer"
    assert targs[targs.index("--turn") + 1] == "t_1"


def test_a_managers_server_publishes_exactly_its_members(tmp_path):
    """`--only "" --delegates a,b`: no external tools, the members as tools —
    including a member the agent itself is not offered."""
    (tmp_path / "tools.json").write_text(json.dumps(tools_envelope()))
    (tmp_path / "delegates.json").write_text(json.dumps({"schemaVersion": 1, "delegates": DELEGATES}))
    import io
    stdin = io.BytesIO((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n").encode())
    stdout = io.BytesIO()
    orig = shim.serve
    shim.serve = lambda server: orig(server, stdin=stdin, stdout=stdout)
    try:
        rc = shim.main(["--slug", SLUG, "--tools", str(tmp_path / "tools.json"), "--server-url",
                        "https://mcp-dev.oppizi.com/mcp", "--only", "", "--delegates",
                        "subagent_research,subagent_writer"])
    finally:
        shim.serve = orig
    assert rc == 0
    names = [t["name"] for t in json.loads(stdout.getvalue().decode().splitlines()[0])["result"]["tools"]]
    assert names == ["subagent_research", "subagent_writer"]


def test_the_agents_server_offers_only_the_offered_delegates(tmp_path):
    (tmp_path / "tools.json").write_text(json.dumps(tools_envelope()))
    (tmp_path / "delegates.json").write_text(json.dumps({"schemaVersion": 1, "delegates": DELEGATES}))
    import io
    stdin = io.BytesIO((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n").encode())
    stdout = io.BytesIO()
    orig = shim.serve
    shim.serve = lambda server: orig(server, stdin=stdin, stdout=stdout)
    try:
        assert shim.main(["--slug", SLUG, "--tools", str(tmp_path / "tools.json"),
                          "--server-url", "https://mcp-dev.oppizi.com/mcp"]) == 0
    finally:
        shim.serve = orig
    names = {t["name"] for t in json.loads(stdout.getvalue().decode().splitlines()[0])["result"]["tools"]}
    assert {"subagent_research", "workflow_brief"} <= names and "subagent_writer" not in names


def test_an_unknown_delegate_id_fails_loud(tmp_path):
    (tmp_path / "tools.json").write_text(json.dumps(tools_envelope()))
    (tmp_path / "delegates.json").write_text(json.dumps({"schemaVersion": 1, "delegates": DELEGATES}))
    assert shim.main(["--slug", SLUG, "--tools", str(tmp_path / "tools.json"), "--server-url",
                      "https://mcp-dev.oppizi.com/mcp", "--only", "", "--delegates", "subagent_nope"]) == 2


def test_a_delegate_call_needs_a_task(tmp_path):
    calls = []
    out = _delegate_runner(tmp_path, calls).run_delegate("subagent_research", "  ")
    assert out.get("isError") and "needs a `task`" in out["content"][0]["text"] and calls == []


def test_a_workflow_members_join_the_managers_message_budget(tmp_path):
    """A member's run books into the message the manager was started for —
    the manager's own calls do not touch the ledger, so without the explicit
    message a member starting 30 s later would get a fresh budget."""
    runner = _delegate_runner(tmp_path, [], budget_message="prompt:abc")
    assert runner._budget_message("t_1", "r1") == "prompt:abc"
    (tmp_path / "config.json").write_text(json.dumps({"loop": {"tokenBudget": 50000}}))
    cmd = runner.delegate_command("workflow_brief", "go", turn="t_1", budget_run=("prompt:abc", "r1"))
    targs = _mcp_config(cmd)["soleon-agent-tools"]["args"]
    assert targs[targs.index("--budget-message") + 1] == "prompt:abc"
    assert "--settings" in cmd and cmd[cmd.index("--output-format") + 1] == "stream-json"


# ---------------------------------------------------------------------------
# macOS: Claude Code keeps the credentials document in the login Keychain
# (generic password, service "Claude Code-credentials", account = login user).
# The Keychain read is stubbed — the real one is never touched by the suite.
# ---------------------------------------------------------------------------

def _doc(url, token, **extra):
    return {"mcpOAuth": {"soleon-agent-toolkit|k": {"serverName": "plugin:admin:soleon", "serverUrl": url,
                                                    "accessToken": token, **extra}}}


class FakeSecurity:
    """Stands in for `subprocess.run` of /usr/bin/security."""

    def __init__(self, outputs):
        self.outputs = list(outputs)  # (returncode, stdout) per call, in order
        self.argv = []

    def __call__(self, argv, **kwargs):
        self.argv.append(argv)
        code, out = self.outputs.pop(0)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")


def test_read_keychain_asks_security_for_claude_codes_item():
    doc = {"mcpOAuth": {}}
    run = FakeSecurity([(0, json.dumps(doc) + "\n")])
    assert client_mod.read_keychain(account="scott", run=run) == doc
    assert run.argv == [["/usr/bin/security", "find-generic-password",
                         "-s", "Claude Code-credentials", "-a", "scott", "-w"]]


def test_read_keychain_accepts_a_hex_secret_and_falls_back_to_the_service_alone():
    doc = {"mcpOAuth": {"x": {"accessToken": "t"}}}
    run = FakeSecurity([(44, ""), (0, json.dumps(doc).encode().hex() + "\n")])
    assert client_mod.read_keychain(account="nobody", run=run) == doc
    assert run.argv[1] == ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]


def test_read_keychain_is_empty_when_security_is_missing_or_finds_nothing():
    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])
    assert client_mod.read_keychain(account="scott", run=missing) == {}
    assert client_mod.read_keychain(account="scott", run=FakeSecurity([(44, ""), (44, "")])) == {}
    assert client_mod.read_keychain(account="scott", run=FakeSecurity([(0, "not json")])) == {}


def test_keychain_token_is_used_on_macos(soleon, tmp_path, monkeypatch):
    """The receipt (2026-10-15): on a Mac the token is only in the Keychain, the
    file does not exist, and every helper failed with 'No usable Soleon token'."""
    url, state = soleon
    monkeypatch.delenv(client_mod.TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr(client_mod.sys, "platform", "darwin")
    monkeypatch.setattr(client_mod, "read_keychain", lambda: _doc(url, GOOD))
    monkeypatch.setattr(client_mod, "DEFAULT_CREDENTIALS_PATH", str(tmp_path / "absent.json"))
    client = client_mod.SoleonMcpClient(url)
    env = client.call_tool("call_agent_tool", {"slug": SLUG, "app_env": "dev", "name": "custom_echo-server_read",
                                               "args": {"prompt": "kc"}, "approved": False})
    assert env["state"] == "done"
    assert client._entry["_source"] == "keychain"


def test_keychain_is_not_read_off_macos_or_with_an_explicit_file(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("the Keychain must not be read here")
    monkeypatch.setattr(client_mod, "read_keychain", boom)
    monkeypatch.setattr(client_mod.sys, "platform", "linux")
    assert client_mod.CredentialStore(str(tmp_path / "c.json")).find_entry("https://x/mcp") is None
    monkeypatch.setattr(client_mod.sys, "platform", "darwin")
    assert client_mod.CredentialStore(str(tmp_path / "c.json")).find_entry("https://x/mcp") is None


def test_the_file_is_the_fallback_when_the_keychain_has_no_entry(tmp_path):
    url = "https://mcp.example.com/mcp"
    creds = tmp_path / "creds.json"
    write_credentials(creds, url, "from-file")
    store = client_mod.CredentialStore(str(creds), keychain=lambda: {"mcpOAuth": {}})
    entry = store.find_entry(url)
    assert entry["accessToken"] == "from-file" and entry["_source"] == "file"
    store = client_mod.CredentialStore(str(creds), keychain=lambda: _doc(url, "from-keychain"))
    entry = store.find_entry(url)
    assert entry["accessToken"] == "from-keychain" and entry["_source"] == "keychain"


def test_env_override_beats_the_keychain(soleon, monkeypatch):
    url, _ = soleon
    monkeypatch.setenv(client_mod.TOKEN_ENV_VAR, GOOD)
    monkeypatch.setattr(client_mod.sys, "platform", "darwin")
    monkeypatch.setattr(client_mod, "read_keychain", lambda: _doc(url, STALE))
    client = client_mod.SoleonMcpClient(url)
    env = client.call_tool("call_agent_tool", {"slug": SLUG, "app_env": "dev", "name": "custom_echo-server_read",
                                               "args": {"prompt": "env"}, "approved": False})
    assert env["state"] == "done"


def test_a_leftover_sign_in_for_the_same_server_loses_to_the_live_one():
    """Receipt (2026-10-15, Scott's Keychain): the plugin rename left an expired
    `plugin:soleon-admin:…` entry for the same URL ahead of the live
    `plugin:admin:soleon` one in the map; first-match picked the dead one."""
    import time as _time
    url = "https://mcp.example.com/mcp"
    now = _time.time() * 1000
    doc = {"mcpOAuth": {
        "plugin:soleon-admin:soleon-agent-toolkit|old": {"serverUrl": url, "accessToken": "dead",
                                                         "expiresAt": now - 9 * 86400000},
        "plugin:admin:soleon|new": {"serverUrl": url, "accessToken": "live", "expiresAt": now + 3600000},
        "no-expiry|x": {"serverUrl": url, "accessToken": "unknown"},
    }}
    store = client_mod.CredentialStore(keychain=lambda: doc)
    assert store.find_entry(url)["accessToken"] == "live"
    del doc["mcpOAuth"]["plugin:admin:soleon|new"]
    # an entry with no recorded expiry beats one known to be expired
    assert store.find_entry(url)["accessToken"] == "unknown"


def test_a_keychain_token_is_refreshed_in_memory_and_nothing_is_written(soleon, tmp_path, monkeypatch):
    url, state = soleon
    monkeypatch.delenv(client_mod.TOKEN_ENV_VAR, raising=False)
    creds = tmp_path / "creds.json"
    write_credentials(creds, url, "file-token")
    before = creds.read_bytes()
    client = client_mod.SoleonMcpClient(url, credentials_path=str(creds))
    client.store = client_mod.CredentialStore(str(creds), keychain=lambda: _doc(
        url, STALE, refreshToken=REFRESH, clientId="cli-1"))
    env = client.call_tool("call_agent_tool", {"slug": SLUG, "app_env": "dev", "name": "custom_echo-server_read",
                                               "args": {"prompt": "r"}, "approved": False})
    assert env["state"] == "done"
    assert [c for c in state["calls"] if c[0] == "token"], "the refresh grant never ran"
    assert creds.read_bytes() == before  # the file was not the source, so it is not rewritten


# ---------------------------------------------------------------------------
# A result the platform answered only as a link (AHP-1191)
# ---------------------------------------------------------------------------

class _StoredResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _LinkClient:
    """The platform's answer for a result over its 800,000-character reply
    limit: `result: {}` plus `result_url` (agent-infra
    `capabilities._local_control_respond`)."""

    def __init__(self, stored=None, fail=None, inline=None, state="done"):
        self.opened = []
        self._stored, self._fail, self._inline, self._state = stored, fail, inline, state

    @staticmethod
    def _sleep(_seconds):
        raise AssertionError("a terminal envelope must not be polled")

    def _open(self, req, timeout=None):
        self.opened.append(req.full_url)
        if self._fail:
            raise self._fail
        return _StoredResp(json.dumps(self._stored).encode("utf-8"))

    def call_tool(self, name, args):
        env = {"state": self._state, "call_id": "lc_0123456789abcdef01234567", "op": "tool_call",
               "result_url": "https://bucket.s3.amazonaws.com/local-call-results/lc_x.json?X-Amz-Signature=s",
               "result_url_expires_in_seconds": 900, "result_chars": 900_123,
               "result": self._inline or {},
               "next_action": "The result is 900123 characters, too large to return here. Download it."}
        if self._state == "error":
            env.update(error="op_failed", detail="")
        return env


def test_a_result_answered_as_a_link_reaches_the_local_agent_whole():
    """On Soleon the agent gets a 900,000-character tool result whole, so the
    local agent must too — not the empty `result` the reply carried."""
    big = "row," * 225_000
    client = _LinkClient(stored={"op": "tool_call", "callId": "lc_0123456789abcdef01234567",
                                 "turnId": "t1", "sessionId": "ses_x", "tool": "send_email",
                                 "result": big, "elapsed_ms": 12})
    out = shim.AgentToolsServer(SLUG, MEMBERS, client=client, include_members=True).call(
        "send_email", {"to": "sam@example.com"})
    assert not out.get("isError"), out
    assert client.opened == ["https://bucket.s3.amazonaws.com/local-call-results/lc_x.json?X-Amz-Signature=s"]
    assert out["content"][0]["text"] == big


def test_a_failed_download_is_an_error_never_an_empty_result():
    import urllib.error
    client = _LinkClient(fail=urllib.error.HTTPError("u", 403, "Forbidden", {}, None))
    out = shim.AgentToolsServer(SLUG, MEMBERS, client=client, include_members=True).call(
        "send_email", {"to": "sam@example.com"})
    assert out["isError"]
    text = out["content"][0]["text"]
    assert "900123-character result" in text and "get_agent_tool_result" in text


def test_a_result_that_arrived_inline_is_not_downloaded_again():
    client = _LinkClient(inline={"tool": "send_email", "result": "sent"})
    out = shim.AgentToolsServer(SLUG, MEMBERS, client=client, include_members=True).call(
        "send_email", {"to": "sam@example.com"})
    assert client.opened == [] and out["content"][0]["text"] == "sent"


def test_a_stored_error_answers_its_own_error_and_detail():
    from soleon_mcp_client import resolve_stored_result
    client = _LinkClient(state="error", stored={"error": "tool_error", "detail": "RuntimeError: boom"})
    out = resolve_stored_result(client, client.call_tool("call_agent_tool", {}))
    assert out["error"] == "tool_error" and out["detail"] == "RuntimeError: boom"
    assert "next_action" not in out  # "download it" no longer applies
