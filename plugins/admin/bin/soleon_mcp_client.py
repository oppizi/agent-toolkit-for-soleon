"""Stdlib-only HTTP client for the Soleon MCP server (`tools/call` over JSON-RPC).

Shared by the two local processes that talk to Soleon OUTSIDE the Claude Code
MCP connection: the `soleon-agent-tools` stdio shim (`soleon_agent_tools_mcp.py`)
and the draft save hook (`soleon_draft_sync.py`). Both need the same three
things and must agree on all of them:

1. **The bearer token Claude Code already holds.** Claude Code stores MCP OAuth
   tokens as one JSON document with an `mcpOAuth` map, keyed
   `"<serverName>|<hash>"`; each entry carries `serverUrl`, `accessToken`, and
   may carry `refreshToken`, `expiresAt` (ms), `clientId`, `clientSecret`.
   On Linux the document is `~/.claude/.credentials.json` (mode 0600). On
   macOS it is the login-Keychain generic password with service
   `Claude Code-credentials` and the login user as account, read with
   `/usr/bin/security find-generic-password -w`; the file is the fallback.
   We pick the entry whose `serverUrl` equals the configured Soleon URL
   (fallback: same host); when several match — a sign-in left behind by an
   older plugin name — the one that expires last wins. `SOLEON_MCP_TOKEN`
   overrides both (tests, CI), and an explicit credentials path means that
   file only.
2. **The wire.** The Soleon MCP is a STATELESS streamable-HTTP server: one
   `POST <server-url>` per call, no `initialize`, no session id. The tool's JSON
   envelope rides `result.content[0].text` (a JSON string); `result.isError`
   marks a failed call. Responses may arrive as `application/json` or as a
   single-event `text/event-stream` — both are handled.
3. **Failure handling.** 429 → exponential backoff (6 attempts). 401 → ONE
   refresh through the AS's `token_endpoint` (discovered from
   `<origin>/.well-known/oauth-authorization-server`) when the entry carries a
   `refreshToken` + `clientId`, the new access token written back into the
   credentials file (mode kept at 0600, every other key preserved), then ONE
   retry; anything else surfaces as `AuthError` with the reconnect instruction.
   A token read from the Keychain is refreshed in memory only: the Keychain
   item is Claude Code's, and Soleon's Cognito-backed refresh grant does not
   rotate the refresh token, so Claude Code's copy stays valid untouched.

Tokens are never logged, never printed, never included in exception text.
"""
from __future__ import annotations

import getpass
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

DEFAULT_SERVER_URL = "https://mcp-dev.oppizi.com/mcp"
DEFAULT_CREDENTIALS_PATH = os.path.join("~", ".claude", ".credentials.json")
#: Claude Code's macOS Keychain item (generic password; account = login user).
KEYCHAIN_SERVICE = "Claude Code-credentials"
SECURITY_BIN = "/usr/bin/security"
PROTOCOL_VERSION = "2025-03-26"
TOKEN_ENV_VAR = "SOLEON_MCP_TOKEN"

RECONNECT_INSTRUCTION = (
    "No usable Soleon token. In Claude Code run /mcp, connect (or reconnect) the "
    "soleon server, then retry."
)

_RETRY_429_ATTEMPTS = 6
_TIMEOUT_S = 120


class SoleonClientError(Exception):
    """Base class — every failure this module raises is one of these."""


class MissingTokenError(SoleonClientError):
    """No token in the Keychain, the credentials file or the env for the configured server."""


class AuthError(SoleonClientError):
    """The server rejected the token and it could not be refreshed."""


class ToolError(SoleonClientError):
    """`tools/call` answered `isError: true` (or a JSON-RPC error). `.payload`
    carries the parsed envelope when the text was JSON, else `{"text": ...}`."""

    def __init__(self, message: str, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.payload = payload or {}


class TransportError(SoleonClientError):
    """HTTP-level failure other than 401/429 (5xx, connection refused, ...)."""


# ---------------------------------------------------------------------------
# Credentials (macOS Keychain, else the credentials file)
# ---------------------------------------------------------------------------

def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return "{}://{}".format(parts.scheme, parts.netloc)


def _parse_secret(text: str) -> Dict[str, Any]:
    """The Keychain secret as a dict. `security -w` prints the password as
    text, or as hex when it holds bytes it cannot print — accept both."""
    text = (text or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = json.loads(bytes.fromhex(text).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
    return data if isinstance(data, dict) else {}


def read_keychain(service: str = KEYCHAIN_SERVICE, account: Optional[str] = None,
                  run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Claude Code's credentials document from the macOS login Keychain, or {}.

    Tries the login user's account first, then the service alone. The secret
    travels only through the child's stdout pipe — never argv, logs or
    exception text.
    """
    if account is None:
        try:
            account = getpass.getuser()
        except Exception:  # noqa: BLE001 — no user name is just "try the service alone"
            account = ""
    attempts = [["-s", service, "-a", account]] if account else []
    attempts.append(["-s", service])
    for args in attempts:
        try:
            proc = run([SECURITY_BIN, "find-generic-password", *args, "-w"],
                       capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            return {}
        if proc.returncode == 0:
            return _parse_secret(proc.stdout)
    return {}


def _expiry_rank(entry: Dict[str, Any], now_ms: float) -> tuple:
    """Sort key among same-server entries: unexpired (latest first), then no
    expiry recorded, then expired (latest first)."""
    expires = entry.get("expiresAt")
    if isinstance(expires, (int, float)) and expires:
        return (2, expires) if expires > now_ms else (0, expires)
    return (1, 0)


class CredentialStore:
    """Read access to Claude Code's credentials — the macOS Keychain item, else
    `~/.claude/.credentials.json` — and write-back to the FILE only.

    Only the `mcpOAuth` entry for ONE server is ever touched, and the whole
    file is rewritten byte-for-byte otherwise (same keys, same order). The
    Keychain item belongs to Claude Code and is never written.

    `keychain`: a zero-argument callable returning the Keychain document; the
    default reads the real Keychain on macOS when no explicit `path` was given
    (an explicit path means that file only — what tests and `--credentials`
    ask for). `use_keychain=False` turns the Keychain off.
    """

    def __init__(self, path: Optional[str] = None,
                 keychain: Optional[Callable[[], Dict[str, Any]]] = None,
                 use_keychain: Optional[bool] = None):
        self.path = os.path.expanduser(path or DEFAULT_CREDENTIALS_PATH)
        if use_keychain is None:
            use_keychain = keychain is not None or (path is None and sys.platform == "darwin")
        self._keychain = (keychain or read_keychain) if use_keychain else None

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def find_entry(self, server_url: str) -> Optional[Dict[str, Any]]:
        """The `mcpOAuth` entry for `server_url`, from the Keychain first (macOS)
        and then the file. Returns a dict with the entry's key under `_key` and
        where it came from under `_source` ("keychain" | "file")."""
        sources = []
        if self._keychain is not None:
            sources.append(("keychain", self._keychain))
        sources.append(("file", self._load))
        for source, load in sources:
            hit = self._pick(load().get("mcpOAuth"), server_url)
            if hit is not None:
                hit["_source"] = source
                return hit
        return None

    @staticmethod
    def _pick(oauth: Any, server_url: str) -> Optional[Dict[str, Any]]:
        """Exact `serverUrl` match first, then same host. Several sign-ins for
        one server (a new plugin name next to an old one's leftover) resolve to
        the one that expires last — never just the first in the map."""
        if not isinstance(oauth, dict):
            return None
        want = server_url.rstrip("/")
        now_ms = time.time() * 1000.0
        exact = []
        same_host = []
        for key, entry in oauth.items():
            if not isinstance(entry, dict) or not entry.get("accessToken"):
                continue
            url = str(entry.get("serverUrl") or "").rstrip("/")
            if url == want:
                exact.append((key, entry))
            elif _host(url) and _host(url) == _host(want):
                same_host.append((key, entry))
        pool = exact or same_host
        if not pool:
            return None
        key, entry = max(pool, key=lambda kv: _expiry_rank(kv[1], now_ms))
        out = dict(entry)
        out["_key"] = key
        return out

    def update_access_token(self, key: str, access_token: str,
                            refresh_token: Optional[str] = None,
                            expires_at_ms: Optional[int] = None) -> None:
        """Write the refreshed token(s) back, preserving everything else and
        the file's 0600 mode."""
        data = self._load()
        oauth = data.get("mcpOAuth")
        if not isinstance(oauth, dict) or key not in oauth:
            return
        entry = dict(oauth[key])
        entry["accessToken"] = access_token
        if refresh_token:
            entry["refreshToken"] = refresh_token
        if expires_at_ms is not None:
            entry["expiresAt"] = expires_at_ms
        oauth[key] = entry
        data["mcpOAuth"] = oauth
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

def _parse_mcp_body(raw: bytes, content_type: str) -> Dict[str, Any]:
    """The JSON-RPC response object from a JSON or single-event SSE body."""
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in (content_type or "") or text.lstrip().startswith(("event:", "data:", ":")):
        payloads: List[str] = []
        for line in text.splitlines():
            if line.startswith("data:"):
                payloads.append(line[5:].strip())
        for chunk in reversed(payloads):
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                return obj
        raise TransportError("Soleon MCP answered an event stream with no JSON-RPC result")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        raise TransportError("Soleon MCP answered a non-JSON body ({} bytes)".format(len(raw)))
    if not isinstance(obj, dict):
        raise TransportError("Soleon MCP answered a JSON-RPC body that is not an object")
    return obj


def unwrap_tool_result(rpc: Dict[str, Any]) -> Dict[str, Any]:
    """`result.content[].text` → the tool's parsed envelope. Raises ToolError on
    `isError` or a JSON-RPC `error`."""
    if "error" in rpc and rpc["error"] is not None:
        err = rpc["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise ToolError("Soleon MCP error: {}".format(msg), {"error": "jsonrpc_error", "detail": msg})
    result = rpc.get("result")
    if not isinstance(result, dict):
        raise TransportError("Soleon MCP answered without a result object")
    structured = result.get("structuredContent")
    texts: List[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    joined = "\n".join(texts)
    payload: Dict[str, Any]
    if isinstance(structured, dict):
        payload = structured
    else:
        try:
            parsed = json.loads(joined) if joined.strip() else {}
        except json.JSONDecodeError:
            parsed = {"text": joined}
        payload = parsed if isinstance(parsed, dict) else {"value": parsed}
    if result.get("isError"):
        raise ToolError(joined or "tool call failed", payload)
    return payload


class SoleonMcpClient:
    """`call_tool(name, arguments)` against the stateless Soleon MCP."""

    def __init__(
        self,
        server_url: Optional[str] = None,
        credentials_path: Optional[str] = None,
        token_override: Optional[str] = None,
        sleep: Callable[[float], None] = time.sleep,
        opener: Optional[Callable[..., Any]] = None,
    ):
        self.server_url = (server_url or DEFAULT_SERVER_URL).rstrip("/")
        self.store = CredentialStore(credentials_path)
        self._token_override = token_override if token_override is not None else os.environ.get(TOKEN_ENV_VAR)
        self._sleep = sleep
        self._open = opener or urllib.request.urlopen
        self._next_id = 1
        self._entry: Optional[Dict[str, Any]] = None
        self._token: Optional[str] = None
        #: ONE refresh per process, whether triggered by a past `expiresAt` or a 401.
        self._refresh_attempted = False

    # -- token -------------------------------------------------------------

    def _resolve_token(self) -> str:
        if self._token:
            return self._token
        if self._token_override:
            self._token = self._token_override
            return self._token
        entry = self.store.find_entry(self.server_url)
        if entry is None:
            raise MissingTokenError(RECONNECT_INSTRUCTION)
        self._entry = entry
        expires_at = entry.get("expiresAt")
        if isinstance(expires_at, (int, float)) and expires_at and expires_at / 1000.0 <= time.time():
            if self._try_refresh():
                return self._token or ""
        self._token = str(entry["accessToken"])
        return self._token

    def _try_refresh(self) -> bool:
        """ONE refresh_token grant. True when a new access token is in place."""
        entry = self._entry
        if self._refresh_attempted or not entry or not entry.get("refreshToken") or not entry.get("clientId"):
            return False
        self._refresh_attempted = True
        try:
            meta_url = _origin(self.server_url) + "/.well-known/oauth-authorization-server"
            req = urllib.request.Request(meta_url, headers={"Accept": "application/json"})
            with self._open(req, timeout=30) as resp:
                meta = json.loads(resp.read().decode("utf-8", "replace"))
            token_endpoint = meta.get("token_endpoint") if isinstance(meta, dict) else None
            if not token_endpoint:
                return False
            form = {
                "grant_type": "refresh_token",
                "refresh_token": entry["refreshToken"],
                "client_id": entry["clientId"],
            }
            if entry.get("clientSecret"):
                form["client_secret"] = entry["clientSecret"]
            body = urllib.parse.urlencode(form).encode("ascii")
            req = urllib.request.Request(
                token_endpoint, data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
            with self._open(req, timeout=30) as resp:
                tok = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            return False
        access = tok.get("access_token") if isinstance(tok, dict) else None
        if not access:
            return False
        expires_in = tok.get("expires_in")
        expires_at_ms = None
        if isinstance(expires_in, (int, float)):
            expires_at_ms = int((time.time() + float(expires_in)) * 1000)
        self._token = str(access)
        if entry.get("_source") != "file":
            return True  # Keychain: Claude Code's item, refreshed in memory only
        try:
            self.store.update_access_token(
                entry["_key"], self._token, tok.get("refresh_token"), expires_at_ms,
            )
        except OSError:
            pass  # the token still works for this process; persisting it is best-effort
        return True

    # -- wire --------------------------------------------------------------

    def _post(self, payload: Dict[str, Any], token: str) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.server_url, data=body, method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
        )
        with self._open(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "") if hasattr(resp, "headers") else ""
        return _parse_mcp_body(raw, ctype)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """One `tools/call`; returns the tool's parsed JSON envelope."""
        token = self._resolve_token()
        attempt = 0
        while True:
            rpc_id = self._next_id
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            try:
                rpc = self._post(payload, token)
            except urllib.error.HTTPError as exc:
                code = getattr(exc, "code", 0)
                try:
                    exc.read()
                except Exception:
                    pass
                if code == 401:
                    if self._try_refresh():
                        token = self._token or token
                        continue
                    raise AuthError("Soleon rejected the token (401). " + RECONNECT_INSTRUCTION)
                if code == 429 and attempt < _RETRY_429_ATTEMPTS - 1:
                    self._sleep(min(0.5 * (2 ** attempt), 8.0))
                    attempt += 1
                    continue
                raise TransportError("Soleon MCP HTTP {} on tools/call {}".format(code, name))
            except urllib.error.URLError as exc:
                raise TransportError("Soleon MCP unreachable: {}".format(getattr(exc, "reason", exc)))
            return unwrap_tool_result(rpc)


# ---------------------------------------------------------------------------
# Local-control await helper (spec D14 — no client-side timeout)
# ---------------------------------------------------------------------------

def await_control(
    client: SoleonMcpClient,
    slug: str,
    envelope: Dict[str, Any],
    *,
    app_env: str = "dev",
    interval_s: float = 1.5,
    sleep: Optional[Callable[[float], None]] = None,
    on_wait: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Follow a `{state: pending, call_id}` envelope until it is `done` or
    `error`, polling `get_agent_tool_result`. Never gives up — a cold container
    can take minutes and the platform behaviour is to wait for the tool."""
    do_sleep = sleep or client._sleep
    while isinstance(envelope, dict) and envelope.get("state") == "pending":
        if on_wait:
            on_wait(envelope)
        do_sleep(interval_s)
        envelope = client.call_tool("get_agent_tool_result", {
            "slug": slug, "app_env": app_env, "call_id": envelope["call_id"],
        })
    return envelope


def is_platform_error(envelope: Any) -> bool:
    """A non-2xx platform envelope (`{error, detail, next_action, status}`) —
    as opposed to a control envelope whose `state` says done/error/pending."""
    return (
        isinstance(envelope, dict)
        and "state" not in envelope
        and "body" not in envelope
        and bool(envelope.get("error"))
    )


def describe_error(envelope: Dict[str, Any]) -> str:
    err = str(envelope.get("error") or "error")
    detail = str(envelope.get("detail") or "")
    nxt = str(envelope.get("next_action") or "")
    out = err
    if detail:
        out += ": " + detail
    if nxt:
        out += " — " + nxt
    return out
