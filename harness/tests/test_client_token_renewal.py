"""A long-running client renews its Soleon token after EVERY expiry
(2026-11-18: the knowledge-base loop died after two hours on "cognito:
Signature has expired" — the client renewed once per process, and a tool that
re-verifies the bearer itself reports an expired token as a tool error, not as
HTTP 401)."""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

from _local_emulation_fixtures import BIN

sys.path.insert(0, str(BIN))
import soleon_mcp_client as client_mod  # noqa: E402

URL = "https://mcp-dev.example.com/mcp"
TOKEN_ENDPOINT = "https://mcp-dev.example.com/token"


class _Resp(io.BytesIO):
    def __init__(self, body: dict):
        super().__init__(json.dumps(body).encode())
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSoleon:
    """Issues tokens t1, t2, …; accepts only the tokens in `valid`. A token
    that is not valid is answered the way an admin-forward tool answers it: a
    tool error carrying libs/authz's expiry text, over HTTP 200."""

    def __init__(self, valid=("t0",), *, refresh_ok=True):
        self.valid = set(valid)
        self.issued = 0
        self.refresh_ok = refresh_ok
        self.refreshes = 0
        self.sent = []

    def expire_all(self):
        self.valid.clear()

    def __call__(self, req, timeout=None):
        url = req.full_url
        if url.endswith("/.well-known/oauth-authorization-server"):
            return _Resp({"token_endpoint": TOKEN_ENDPOINT})
        if url == TOKEN_ENDPOINT:
            form = dict(urllib.parse.parse_qsl(req.data.decode()))
            assert form["grant_type"] == "refresh_token" and form["client_id"] == "cli-1"
            self.refreshes += 1
            if not self.refresh_ok:
                raise urllib.error.URLError("refused")
            self.issued += 1
            token = "t{}".format(self.issued)
            self.valid.add(token)
            return _Resp({"access_token": token, "expires_in": 3600})
        token = req.headers.get("Authorization", "").split(" ", 1)[-1]
        self.sent.append(token)
        rpc_id = json.loads(req.data)["id"]
        if token not in self.valid:
            return _Resp({"jsonrpc": "2.0", "id": rpc_id, "result": {
                "isError": True, "content": [{"type": "text", "text": "cognito: Signature has expired"}]}})
        return _Resp({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "content": [{"type": "text", "text": json.dumps({"status": 200, "body": {"ok": token}})}]}})


def _creds(tmp_path: Path, token="t0", expires_at=None) -> Path:
    path = tmp_path / ".credentials.json"
    entry = {"serverUrl": URL, "accessToken": token, "refreshToken": "r", "clientId": "cli-1"}
    if expires_at is not None:
        entry["expiresAt"] = expires_at
    path.write_text(json.dumps({"mcpOAuth": {"plugin:soleon:soleon|x": entry}}))
    return path


def _client(tmp_path, fake, **kw):
    return client_mod.SoleonMcpClient(URL, credentials_path=str(_creds(tmp_path, **kw)),
                                      token_override="", opener=fake, sleep=lambda s: None)


def test_an_expired_token_tool_error_renews_and_retries(tmp_path):
    fake = FakeSoleon(valid=())
    client = _client(tmp_path, fake)
    assert client.call_tool("local_kb_ingest", {})["body"] == {"ok": "t1"}
    assert fake.refreshes == 1


def test_a_long_running_client_renews_after_every_expiry(tmp_path):
    fake = FakeSoleon()
    client = _client(tmp_path, fake)
    assert client.call_tool("x", {})["body"] == {"ok": "t0"}
    for expected in ("t1", "t2", "t3"):
        fake.expire_all()
        assert client.call_tool("x", {})["body"] == {"ok": expected}
    assert fake.refreshes == 3


def test_a_token_about_to_expire_is_renewed_before_the_call(tmp_path):
    fake = FakeSoleon()
    client = _client(tmp_path, fake, expires_at=(time.time() + 30) * 1000)
    assert client.call_tool("x", {})["body"] == {"ok": "t1"}
    assert fake.sent == ["t1"], "the expiring token is never sent"


def test_a_failed_refresh_is_not_retried_for_the_same_token(tmp_path):
    fake = FakeSoleon(valid=(), refresh_ok=False)
    client = _client(tmp_path, fake)
    for _ in range(2):
        try:
            client.call_tool("x", {})
        except client_mod.ToolError as exc:
            assert "Signature has expired" in str(exc)
        else:
            raise AssertionError("an unrenewable token must surface the error")
    assert fake.refreshes == 1


def test_a_token_another_process_saved_is_adopted_without_a_refresh(tmp_path):
    fake = FakeSoleon()
    client = _client(tmp_path, fake)
    assert client.call_tool("x", {})["body"] == {"ok": "t0"}
    fake.valid = {"t5"}  # Claude Code renewed and saved t5
    _creds(tmp_path, token="t5", expires_at=(time.time() + 3600) * 1000)
    assert client.call_tool("x", {})["body"] == {"ok": "t5"}
    assert fake.refreshes == 0


def test_an_ordinary_tool_error_is_not_mistaken_for_an_expired_token(tmp_path):
    fake = FakeSoleon()
    client = _client(tmp_path, fake)

    def deny(req, timeout=None):
        rpc_id = json.loads(req.data)["id"]
        return _Resp({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "isError": True, "content": [{"type": "text", "text": "kb not found"}]}})

    client._open = deny
    try:
        client.call_tool("x", {})
    except client_mod.ToolError as exc:
        assert str(exc) == "kb not found"
    else:
        raise AssertionError("the tool error must surface")
    assert fake.refreshes == 0
