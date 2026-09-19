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
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
SERVER_INFO = {"name": "soleon-agent-tools", "version": "0.4.3"}
APPROVAL_NOTE = "Requires human approval: ask the person first, then call with approved=true."
POLL_INTERVAL_S = 1.5


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


def published_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every `kind == external` tool, own name + own schema; approval-gated ones
    gain the optional `approved` boolean and the approval note."""
    out: List[Dict[str, Any]] = []
    for t in tools:
        if t.get("kind", "external") != "external":
            continue
        schema = t.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        schema = json.loads(json.dumps(schema))  # deep copy, JSON-clean
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
        out.append({"name": t["name"], "description": description, "inputSchema": schema})
    return out


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


class AgentToolsServer:
    def __init__(self, slug: str, tools: List[Dict[str, Any]], client: SoleonMcpClient,
                 app_env: str = "dev", poll_interval_s: float = POLL_INTERVAL_S):
        self.slug = slug
        self.app_env = app_env
        self.tools = tools
        self.published = published_tools(tools)
        self._gated = {t["name"] for t in tools if t.get("approval")}
        self.client = client
        self.poll_interval_s = poll_interval_s

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name not in {t["name"] for t in self.published}:
            return _text_result("Unknown tool {!r} — not one of this agent's external tools.".format(name), True)
        args = dict(arguments or {})
        approved = bool(args.pop("approved", False))
        try:
            envelope = self.client.call_tool("call_agent_tool", {
                "slug": self.slug, "app_env": self.app_env,
                "name": name, "args": args, "approved": approved,
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
        return self.render(name, envelope)

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
    server = AgentToolsServer(args.slug, tools, client, app_env=args.app_env, poll_interval_s=args.poll_interval)
    _log("serving {} external tool(s) for {} via {}".format(len(server.published), args.slug, client.server_url))
    serve(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
