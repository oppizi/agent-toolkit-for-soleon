#!/usr/bin/env python3
"""`soleon-workspace` — a stdio MCP server serving the platform's filesystem
tools against a LOCAL copy of a Soleon agent workspace (spec D9).

    python3 soleon_workspace_mcp.py <workspace-root> [--readable <dir>]...

The four tools mirror the maverick container's filesystem tools byte-for-byte
in name, description and input schema (`containers/maverick-agent/maverick/
agent/tools/filesystem.py`: `read_file`, `write_file`, `edit_file`,
`list_dir`), so the pulled system prompt — which teaches those names — keeps
working unchanged. Behaviour follows the platform too: numbered lines with
offset/limit pagination, whitespace-tolerant `edit_file` with a best-match
diff on miss, noise-directory filtering in `list_dir`.

Path policy (the platform's `_resolve_path` + `extra_allowed_dirs`): every
path resolves under the workspace root — relative paths against the root,
absolute paths as given — and is refused (as an `isError` result, never an
exception) when it escapes. `--readable <dir>` adds READ-ONLY roots, which is
how the pulled `skills/` tree stays readable while "only writing there is
refused", exactly as on the platform. The container's own workspace paths
(`/mnt/workspace`, `/app/workspace`) are aliased onto the root so a memory
file that mentions them still resolves.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout. Logs go to stderr
only — stdout is the wire.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "soleon-workspace", "version": "0.4.15"}

#: Container paths the platform prompt and memory files mention; they alias the root.
CONTAINER_WORKSPACE_ALIASES = ("/mnt/workspace", "/app/workspace")

#: Per-call page size. The platform tool serves up to 128k chars per read, but
#: Claude Code refuses any MCP result over MAX_MCP_OUTPUT_TOKENS (default
#: 25,000 tokens): it spills the result to a file and hands the agent an
#: "exceeds maximum allowed tokens" error, which the Traces UI then shows as a
#: failed step. Dense HTML tokenises at ~1.5 chars/token, so a 128k page can
#: never reach the model here. 30k chars stays under the cap at that density,
#: and the "(Showing lines a-b of N. Use offset=… to continue.)" tail — the
#: platform's own paging mechanic — carries the rest, so a whole-file read of
#: a big file pages instead of failing.
_READ_MAX_CHARS = 30_000
_READ_DEFAULT_LIMIT = 2000
_LIST_DEFAULT_MAX = 200
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".coverage", "htmlcov", "._extracted",
}

# --- tool surface: names, descriptions and schemas copied from the platform ---

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file. Returns numbered lines. "
            "Use offset and limit to paginate through large files. "
            "Binary documents (PDF, Word, PowerPoint, Excel) ARE readable: "
            "their text is extracted automatically. PDF/Word/PowerPoint also "
            "yield embedded images, referenced by [image N: …] markers whose "
            "paths you can read directly; Excel is text/values only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to read"},
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed, default 1)",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (default 2000)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file at the given path. Creates parent directories if needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to write to"},
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Edit a file by replacing old_text with new_text. "
            "Supports minor whitespace/line-ending differences. "
            "Set replace_all=true to replace every occurrence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to edit"},
                "old_text": {"type": "string", "description": "The text to find and replace"},
                "new_text": {"type": "string", "description": "The text to replace with"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false)",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "list_dir",
        "description": (
            "List the contents of a directory. "
            "Set recursive=true to explore nested structure. "
            "Common noise directories (.git, node_modules, __pycache__, etc.) are auto-ignored."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The directory path to list"},
                "recursive": {
                    "type": "boolean",
                    "description": "Recursively list all files (default false)",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum entries to return (default 200)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        },
    },
]


def _log(msg: str) -> None:
    sys.stderr.write("[soleon-workspace] {}\n".format(msg))
    sys.stderr.flush()


# --- path policy -------------------------------------------------------------

class Workspace:
    def __init__(self, root: str, readable: Optional[List[str]] = None):
        self.root = Path(root).expanduser().resolve()
        self.readable = [Path(p).expanduser().resolve() for p in (readable or [])]

    @staticmethod
    def _is_under(path: Path, directory: Path) -> bool:
        try:
            path.relative_to(directory)
            return True
        except ValueError:
            return False

    def resolve(self, raw: str, *, write: bool) -> Path:
        """Resolve `raw` under the root (or a readable dir for reads); raise
        PermissionError when it escapes."""
        text = str(raw)
        for alias in CONTAINER_WORKSPACE_ALIASES:
            if text == alias or text.startswith(alias + "/"):
                text = str(self.root) + text[len(alias):]
                break
        p = Path(text).expanduser()
        if not p.is_absolute():
            p = self.root / p
        resolved = p.resolve()
        if self._is_under(resolved, self.root):
            return resolved
        if not write and any(self._is_under(resolved, d) for d in self.readable):
            return resolved
        if write and any(self._is_under(resolved, d) for d in self.readable):
            raise PermissionError(
                "Path {} is read-only (skills and agent config are readable, never writable)".format(raw)
            )
        raise PermissionError("Path {} is outside allowed directory {}".format(raw, self.root))


# --- tool implementations (platform behaviour) ---------------------------------

def _read_file(ws: Workspace, path: Optional[str] = None, offset: int = 1,
               limit: Optional[int] = None, **_: Any) -> str:
    try:
        if not path:
            return "Error reading file: Unknown path"
        fp = ws.resolve(path, write=False)
        if not fp.exists():
            return "Error: File not found: {}".format(path)
        if not fp.is_file():
            return "Error: Not a file: {}".format(path)
        raw = fp.read_bytes()
        if not raw:
            return "(Empty file: {})".format(path)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return (
                "Error: Cannot read binary file {} locally. The platform extracts PDF/Office "
                "text in the container; this local snapshot serves UTF-8 text only.".format(path)
            )
        all_lines = text.splitlines()
        total = len(all_lines)
        try:
            offset = int(offset or 1)
        except (TypeError, ValueError):
            offset = 1
        if offset < 1:
            offset = 1
        if offset > total:
            return "Error: offset {} is beyond end of file ({} lines)".format(offset, total)
        start = offset - 1
        end = min(start + int(limit or _READ_DEFAULT_LIMIT), total)
        numbered = ["{}| {}".format(start + i + 1, line) for i, line in enumerate(all_lines[start:end])]
        result = "\n".join(numbered)
        if len(result) > _READ_MAX_CHARS:
            trimmed, chars = [], 0
            for line in numbered:
                chars += len(line) + 1
                if chars > _READ_MAX_CHARS:
                    break
                trimmed.append(line)
            end = start + len(trimmed)
            result = "\n".join(trimmed)
        if end < total:
            result += "\n\n(Showing lines {}-{} of {}. Use offset={} to continue.)".format(offset, end, total, end + 1)
        else:
            result += "\n\n(End of file — {} lines total)".format(total)
        return result
    except PermissionError as e:
        return "Error: {}".format(e)
    except Exception as e:  # noqa: BLE001 — surfaced to the model as text, like the platform
        return "Error reading file: {}".format(e)


def _write_file(ws: Workspace, path: Optional[str] = None, content: Optional[str] = None, **_: Any) -> str:
    try:
        if not path:
            raise ValueError("Unknown path")
        if content is None:
            raise ValueError("Unknown content")
        fp = ws.resolve(path, write=True)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(str(content), encoding="utf-8")
        return "Successfully wrote {} bytes to {}".format(len(str(content)), fp)
    except PermissionError as e:
        return "Error: {}".format(e)
    except Exception as e:  # noqa: BLE001
        return "Error writing file: {}".format(e)


def _find_match(content: str, old_text: str) -> Tuple[Optional[str], int]:
    if old_text in content:
        return old_text, content.count(old_text)
    old_lines = old_text.splitlines()
    if not old_lines:
        return None, 0
    stripped_old = [l.strip() for l in old_lines]
    content_lines = content.splitlines()
    candidates = []
    for i in range(len(content_lines) - len(stripped_old) + 1):
        window = content_lines[i:i + len(stripped_old)]
        if [l.strip() for l in window] == stripped_old:
            candidates.append("\n".join(window))
    if candidates:
        return candidates[0], len(candidates)
    return None, 0


def _not_found_msg(old_text: str, content: str, path: str) -> str:
    lines = content.splitlines(keepends=True)
    old_lines = old_text.splitlines(keepends=True)
    window = len(old_lines)
    best_ratio, best_start = 0.0, 0
    for i in range(max(1, len(lines) - window + 1)):
        ratio = difflib.SequenceMatcher(None, old_lines, lines[i:i + window]).ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, i
    if best_ratio > 0.5:
        diff = "\n".join(difflib.unified_diff(
            old_lines, lines[best_start:best_start + window],
            fromfile="old_text (provided)",
            tofile="{} (actual, line {})".format(path, best_start + 1),
            lineterm="",
        ))
        return "Error: old_text not found in {}.\nBest match ({:.0%} similar) at line {}:\n{}".format(
            path, best_ratio, best_start + 1, diff)
    return "Error: old_text not found in {}. No similar text found. Verify the file content.".format(path)


def _edit_file(ws: Workspace, path: Optional[str] = None, old_text: Optional[str] = None,
               new_text: Optional[str] = None, replace_all: bool = False, **_: Any) -> str:
    try:
        if not path:
            raise ValueError("Unknown path")
        if old_text is None:
            raise ValueError("Unknown old_text")
        if new_text is None:
            raise ValueError("Unknown new_text")
        fp = ws.resolve(path, write=True)
        if not fp.exists():
            return "Error: File not found: {}".format(path)
        raw = fp.read_bytes()
        uses_crlf = b"\r\n" in raw
        content = raw.decode("utf-8").replace("\r\n", "\n")
        match, count = _find_match(content, str(old_text).replace("\r\n", "\n"))
        if match is None:
            return _not_found_msg(str(old_text), content, path)
        if count > 1 and not replace_all:
            return (
                "Warning: old_text appears {} times. "
                "Provide more context to make it unique, or set replace_all=true.".format(count)
            )
        norm_new = str(new_text).replace("\r\n", "\n")
        new_content = content.replace(match, norm_new) if replace_all else content.replace(match, norm_new, 1)
        if uses_crlf:
            new_content = new_content.replace("\n", "\r\n")
        fp.write_bytes(new_content.encode("utf-8"))
        return "Successfully edited {}".format(fp)
    except PermissionError as e:
        return "Error: {}".format(e)
    except Exception as e:  # noqa: BLE001
        return "Error editing file: {}".format(e)


def _list_dir(ws: Workspace, path: Optional[str] = None, recursive: bool = False,
              max_entries: Optional[int] = None, **_: Any) -> str:
    try:
        if path is None:
            raise ValueError("Unknown path")
        dp = ws.resolve(path, write=False)
        if not dp.exists():
            return "Error: Directory not found: {}".format(path)
        if not dp.is_dir():
            return "Error: Not a directory: {}".format(path)
        cap = int(max_entries or _LIST_DEFAULT_MAX)
        items: List[str] = []
        total = 0
        if recursive:
            for item in sorted(dp.rglob("*")):
                if any(p in _IGNORE_DIRS for p in item.parts):
                    continue
                total += 1
                if len(items) < cap:
                    rel = item.relative_to(dp)
                    items.append("{}/".format(rel) if item.is_dir() else str(rel))
        else:
            for item in sorted(dp.iterdir()):
                if item.name in _IGNORE_DIRS:
                    continue
                total += 1
                if len(items) < cap:
                    pfx = "📁 " if item.is_dir() else "📄 "
                    items.append(pfx + item.name)
        if not items and total == 0:
            return "Directory {} is empty".format(path)
        result = "\n".join(items)
        if total > cap:
            result += "\n\n(truncated, showing first {} of {} entries)".format(cap, total)
        return result
    except PermissionError as e:
        return "Error: {}".format(e)
    except Exception as e:  # noqa: BLE001
        return "Error listing directory: {}".format(e)


_IMPL = {
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_dir": _list_dir,
}

#: The tools a READ-ONLY server (a `spawn_worker` worker's) publishes.
READ_ONLY_TOOLS = ("read_file", "list_dir")


def _local_result(ws: Workspace, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    text = _IMPL[name](ws, **(arguments or {}))
    out: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if text.startswith("Error"):
        out["isError"] = True
    return out


def _result_text(out: Dict[str, Any]) -> str:
    return " ".join(str(p.get("text") or "") for p in out.get("content") or [] if p.get("type") == "text")


class PlatformWorkspace:
    """The agent's workspace ON SOLEON — the one its code execution reads and
    writes. On the platform the file tools and `run_code_file` /
    `write_and_run_code` share one workspace; locally the file tools used to
    serve only the pulled snapshot, so the agent could not open a preview the
    sandbox had just rendered, and the sandbox never saw a file the agent wrote.

    With a platform attached, Soleon's copy is the truth: reads and listings go
    there first and fall back to the snapshot (memory, files from the person's
    own conversations) only when Soleon has no such file; writes and edits land
    on Soleon first, then are mirrored into the snapshot. Paths under a
    read-only root (the pulled skills) never leave this machine."""

    def __init__(self, ws: Workspace, server):
        self.ws = ws
        self.server = server  # an AgentToolsServer publishing the four platform file tools

    def _platform_path(self, raw: Any) -> Optional[str]:
        """The workspace-relative path Soleon resolves, or None when `raw` is
        not in the workspace (a skills path, or outside every root)."""
        try:
            resolved = self.ws.resolve(str(raw or ""), write=False)
        except PermissionError:
            return None
        if not Workspace._is_under(resolved, self.ws.root):
            return None
        rel = resolved.relative_to(self.ws.root).as_posix()
        return rel or "."

    def _platform(self, name: str, arguments: Dict[str, Any], rel: str) -> Dict[str, Any]:
        return self.server.call(name, dict(arguments, path=rel))

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        rel = self._platform_path(arguments.get("path"))
        if rel is None:
            return _local_result(self.ws, name, arguments)
        out = self._platform(name, arguments, rel)
        failed = bool(out.get("isError")) or _result_text(out).startswith("Error")
        if name in ("read_file", "list_dir"):
            if not failed:
                return out
            local = _local_result(self.ws, name, arguments)
            return out if local.get("isError") else local
        if failed:
            return out
        # The write landed on Soleon: mirror it into the snapshot (best effort —
        # Soleon's copy is the one that counts, and an edit whose text is only
        # there must not fail here).
        mirrored = _local_result(self.ws, name, arguments)
        if mirrored.get("isError"):
            _log("{} {}: saved on Soleon, not mirrored locally ({})".format(name, rel, _result_text(mirrored)[:200]))
        return out


def call_tool(ws: Workspace, name: str, arguments: Dict[str, Any],
              platform: Optional[PlatformWorkspace] = None,
              tools: Optional[List[str]] = None) -> Dict[str, Any]:
    """MCP `tools/call` result. Platform tools report failures as text that
    starts with `Error:`/`Warning:`; we mirror that AND flag `isError` so an
    MCP client can tell."""
    if name not in _IMPL or (tools is not None and name not in tools):
        return {"content": [{"type": "text", "text": "Unknown tool: {}".format(name)}], "isError": True}
    if platform is not None:
        return platform.call(name, arguments or {})
    return _local_result(ws, name, arguments)


# --- JSON-RPC loop ---------------------------------------------------------------

def handle_message(ws: Workspace, msg: Dict[str, Any], platform: Optional[PlatformWorkspace] = None,
                   tools: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """One request → one response dict (None for notifications)."""
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
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "tools": [t for t in TOOLS if tools is None or t["name"] in tools]}}
    if method == "tools/call":
        name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "arguments must be an object"}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": call_tool(ws, name, arguments, platform, tools)}
    if msg_id is None:
        return None  # unknown notification — ignore
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Method not found: {}".format(method)}}


def serve(ws: Workspace, stdin=None, stdout=None, platform: Optional[PlatformWorkspace] = None,
          tools: Optional[List[str]] = None) -> None:
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
            resp = handle_message(ws, msg, platform, tools)
        except Exception as exc:  # noqa: BLE001 — the loop must survive any single call
            _log("internal error on {}: {}".format(msg.get("method"), exc))
            resp = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(exc)}}
        if resp is not None:
            out.write((json.dumps(resp) + "\n").encode("utf-8"))
            out.flush()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", help="the local workspace copy (.soleon/agents/<slug>/workspace)")
    ap.add_argument("--readable", action="append", default=[],
                    help="extra READ-ONLY root (repeatable) — e.g. the pulled skills/ dir")
    ap.add_argument("--slug", default=None,
                    help="the pulled agent: with --server-url, file tools work on its workspace ON SOLEON "
                         "(the one code execution uses), falling back to this local snapshot")
    ap.add_argument("--server-url", default=None, help="the Soleon MCP URL the agent was pulled from")
    ap.add_argument("--app-env", default="dev")
    ap.add_argument("--credentials", default=None)
    ap.add_argument("--turn", default=None, help="a local worker's server: the agent turn its calls belong to")
    ap.add_argument("--read-only", action="store_true",
                    help="publish read_file and list_dir only (a spawn_worker worker's server)")
    args = ap.parse_args(argv)
    ws = Workspace(args.root, args.readable)
    ws.root.mkdir(parents=True, exist_ok=True)
    tools = list(READ_ONLY_TOOLS) if args.read_only else None
    platform = None
    if args.slug and args.server_url:
        platform = PlatformWorkspace(ws, _platform_file_tools(args))
    _log("serving {} (readable: {}; {})".format(
        ws.root, ", ".join(str(d) for d in ws.readable) or "-",
        "Soleon's workspace first via " + args.server_url if platform else "local snapshot only"))
    serve(ws, platform=platform, tools=tools)
    return 0


def _platform_file_tools(args: Any):
    """An `AgentToolsServer` publishing exactly the platform's four file tools,
    in the same platform session (conversation + turn) as the agent's other
    tools, so a file the sandbox writes is the file `read_file` opens."""
    import soleon_agent_tools_mcp as tools_mcp
    from soleon_mcp_client import SoleonMcpClient
    agent_dir = os.path.dirname(os.path.abspath(args.root))
    entries = [{"name": n, "kind": "external", "description": "", "inputSchema": t["inputSchema"]}
               for t in TOOLS for n in [t["name"]]]
    turn = (tools_mcp.TurnTracker(None, fixed=args.turn) if args.turn
            else tools_mcp.TurnTracker(os.path.join(agent_dir, tools_mcp.TURN_FILE_NAME)))
    return tools_mcp.AgentToolsServer(
        args.slug, entries, SoleonMcpClient(args.server_url, credentials_path=args.credentials),
        app_env=args.app_env,
        conversation=tools_mcp.ConversationTracker(os.path.join(agent_dir, ".local-conversation.json")),
        turn=turn)


if __name__ == "__main__":
    sys.exit(main())
