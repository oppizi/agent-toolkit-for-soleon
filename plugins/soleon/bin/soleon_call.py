#!/usr/bin/env python3
"""Call one Soleon MCP tool from a terminal, with your own Claude Code sign-in.

    python3 soleon_call.py --list [--server-url URL] [--grep TEXT]
    python3 soleon_call.py <tool> ['{"json": "arguments"}' | @args.json] [--server-url URL]

For testing a server tool without a Claude Code session: a NEW tool (or a
changed description or schema) only reaches a running session after
`/mcp` -> soleon -> Reconnect, because the Soleon server is stateless and
cannot tell a session its tool list changed. This script asks the server
directly, so a tool can be tried the moment it is deployed.

* `--list` prints every tool the server lists for you (one name per line),
  `--grep` narrows it; `--json` prints the full tools/list answer instead.
* `<tool>` runs one `tools/call` and prints the tool's JSON envelope. An
  envelope that is `{state: pending, call_id}` (an agent tool call, a system
  prompt read) is followed to its end when the arguments carry `slug`, the way
  the toolkit's own agent shim waits on it.
* The token is the one Claude Code saved when you signed in
  (`soleon_mcp_client.CredentialStore`): never printed, never logged. A
  missing or rejected sign-in exits 3 with the reconnect instruction.

Exit codes: 0 success, 1 the tool answered an error, 2 bad usage, 3 sign-in,
4 the server could not be reached.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from soleon_mcp_client import (  # noqa: E402
    DEFAULT_SERVER_URL,
    MissingTokenError,
    AuthError,
    SoleonMcpClient,
    ToolError,
    TransportError,
    await_control,
    is_platform_error,
)

#: Overrides the server without a flag (the name the skills use for it).
SERVER_URL_ENV = "SOLEON_MCP_URL"


def parse_arguments(raw: Optional[str]) -> Dict[str, Any]:
    """The tool arguments: inline JSON, `@path` to a JSON file, or none."""
    if raw is None or raw == "":
        return {}
    if raw.startswith("@"):
        with open(raw[1:], "r", encoding="utf-8") as fh:
            raw = fh.read()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be a JSON object, got {}".format(type(value).__name__))
    return value


def list_tools(client: SoleonMcpClient) -> List[Dict[str, Any]]:
    """The server's tools/list answer for this sign-in."""
    token = client._resolve_token()
    rpc = client._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, token)
    result = rpc.get("result") if isinstance(rpc, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise TransportError("tools/list answered no tool list")
    return tools


def call(client: SoleonMcpClient, name: str, args: Dict[str, Any]) -> Any:
    """One tools/call, followed to its end when it answers pending for an agent."""
    envelope = client.call_tool(name, args)
    if isinstance(envelope, dict) and envelope.get("state") == "pending" and args.get("slug"):
        def _waiting(env: Dict[str, Any]) -> None:
            sys.stderr.write("pending: {} (call {})\n".format(env.get("op") or name, env.get("call_id")))
        envelope = await_control(client, str(args["slug"]), envelope,
                                 app_env=str(args.get("app_env") or "dev"), on_wait=_waiting)
    return envelope


def failed(envelope: Any) -> bool:
    if is_platform_error(envelope):
        return True
    if isinstance(envelope, dict):
        if envelope.get("state") == "error":
            return True
        status = envelope.get("status")
        if isinstance(status, int) and status >= 400:
            return True
    return False


def main(argv: Optional[List[str]] = None, client_factory=SoleonMcpClient) -> int:
    ap = argparse.ArgumentParser(description="Call one Soleon MCP tool with your Claude Code sign-in.")
    ap.add_argument("tool", nargs="?", help="the tool to call (omit with --list)")
    ap.add_argument("arguments", nargs="?", help="JSON object, or @file.json")
    ap.add_argument("--list", action="store_true", help="list the tools the server offers you")
    ap.add_argument("--grep", help="with --list: only names containing this text")
    ap.add_argument("--json", action="store_true", help="with --list: print the full tools/list answer")
    ap.add_argument("--server-url", default=os.environ.get(SERVER_URL_ENV) or DEFAULT_SERVER_URL,
                    help="the Soleon MCP server (default: $SOLEON_MCP_URL, else dev)")
    ap.add_argument("--credentials", help="a credentials file other than Claude Code's own")
    opts = ap.parse_args(argv)
    if not opts.list and not opts.tool:
        ap.print_usage(sys.stderr)
        sys.stderr.write("soleon_call: name a tool, or pass --list\n")
        return 2
    try:
        args = {} if opts.list else parse_arguments(opts.arguments)
    except (OSError, ValueError) as exc:
        sys.stderr.write("soleon_call: bad arguments: {}\n".format(exc))
        return 2
    client = client_factory(opts.server_url, credentials_path=opts.credentials)
    try:
        if opts.list:
            tools = list_tools(client)
            if opts.json:
                print(json.dumps(tools, indent=2, ensure_ascii=False))
                return 0
            names = sorted(str(t.get("name")) for t in tools if isinstance(t, dict))
            if opts.grep:
                names = [n for n in names if opts.grep in n]
            print("\n".join(names))
            sys.stderr.write("{} tool(s) from {}\n".format(len(names), client.server_url))
            return 0
        envelope = call(client, opts.tool, args)
    except (MissingTokenError, AuthError) as exc:
        sys.stderr.write("soleon_call: {}\n".format(exc))
        return 3
    except ToolError as exc:
        sys.stderr.write("soleon_call: {} answered an error: {}\n".format(opts.tool, exc))
        return 1
    except TransportError as exc:
        sys.stderr.write("soleon_call: {}\n".format(exc))
        return 4
    print(json.dumps(envelope, indent=2, ensure_ascii=False))
    return 1 if failed(envelope) else 0


if __name__ == "__main__":
    sys.exit(main())
