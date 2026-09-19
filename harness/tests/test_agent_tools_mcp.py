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
    assert args == {"slug": SLUG, "app_env": "dev", "name": "custom_echo-server_read",
                    "args": {"prompt": "hello"}, "approved": False}
    # every call of this process carries the SAME id
    server.call("custom_echo-server_read", {"prompt": "again"})
    assert state["calls"][-1][1]["conversation"] == conversation


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
    assert "401" in text and "/mcp" in text and "soleon-agent-toolkit" in text
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
