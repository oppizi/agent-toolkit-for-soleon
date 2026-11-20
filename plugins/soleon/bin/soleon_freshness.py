#!/usr/bin/env python3
"""Tell the person when their session runs something older than what exists.

    soleon_freshness.py session-start --data DIR --loaded PLUGIN_ROOT   (SessionStart hook; JSON on stdin)
    soleon_freshness.py check SESSION --data DIR                        (the prompt gate, every 10 min)
    soleon_freshness.py work SESSION --data DIR                         (detached worker)

Three things went stale without a word on 2026-11-18/19, and each cost a day:

1. **The plugin.** A GitHub marketplace is never refreshed by itself (auto-update
   is off for every marketplace Anthropic does not publish): a session ran 1.0.22
   while 1.0.28 was out. A live checkout (the repo's dev_live.py) falls behind
   origin/main the same way.
2. **The tool list.** The Soleon server is stateless and cannot tell a session
   its tools changed: `search_knowledge_base` was served for a day before a
   session saw it.
3. **The Claude Code build.** The VS Code extension updates itself, but an open
   conversation keeps the build it started with; a two-day-old conversation
   ignored a reconnect until the window was reloaded.

Each has a one-command fix the person already has. This hook says which, once
per change. It never runs the fix (a reload or reconnect changes the person's
session), never blocks, and the prompt never waits for the network: the prompt
gate is shell that reads a file, and starts Python at most every 10 minutes
only to launch the detached worker that does the reads. A message the worker
finds is shown on the next prompt.

The running build comes from CLAUDE_CODE_EXECPATH, the binary Claude Code passes
its child processes (not a documented variable: when it is absent or does not
name a VS Code extension folder, that check is skipped, never guessed).
Tokens: the tool list is read with the person's own sign-in through
soleon_mcp_client (never printed); the version read from GitHub is anonymous.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#: The published plugin manifest (the repo is public: no token is sent).
LATEST_URL = ("https://raw.githubusercontent.com/oppizi/agent-toolkit-for-soleon/"
              "main/plugins/soleon/.claude-plugin/plugin.json")
MARKETPLACE = "agent-toolkit-for-soleon"
LATEST_CACHE_S = 3600.0
LOCK_STALE_S = 120.0
NET_TIMEOUT_S = 20.0
NAMES_SHOWN = 6
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_BUILD_RE = re.compile(r"anthropic\.claude-code-(\d+(?:\.\d+)+)-([A-Za-z0-9_-]+)$")
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")


def _log(msg: str) -> None:
    sys.stderr.write("soleon freshness: " + msg.rstrip("\n") + "\n")


def vkey(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version or "0"))


# ---------------------------------------------------------------------------
# What the session runs
# ---------------------------------------------------------------------------

def plugin_info(root: Path) -> Dict[str, Any]:
    """Name, version, server and install kind of the plugin the session loaded."""
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    server = ""
    try:
        mcp = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
        entry = next(iter((mcp.get("mcpServers") or {}).values()), {})
        server = str(entry.get("url") or "")
    except (OSError, ValueError, StopIteration):
        pass
    if "${user_config.server_url}" in server or not server:
        server = str(((manifest.get("userConfig") or {}).get("server_url") or {}).get("default") or "")
    return {"name": str(manifest.get("name") or ""), "version": str(manifest.get("version") or ""),
            "server": server, "root": str(root), "kind": install_kind(root, str(manifest.get("name") or ""))}


def _git(root: Path, *args: str, timeout: float = NET_TIMEOUT_S) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                             timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def install_kind(root: Path, name: str) -> str:
    """`live` (a checkout of the repo, loaded in place), `copy` (installed from
    GitHub into the plugin cache), or `generated` (another plugin built from these
    files, e.g. soleon-prod: dev_live.py rebuilds it with the live checkout)."""
    if _VERSION_RE.match(root.name) is None and _git(root, "rev-parse", "--is-inside-work-tree", timeout=5) == "true":
        return "live"
    return "copy" if name == "soleon" else "generated"


def running_build(execpath: str) -> Optional[Dict[str, str]]:
    """The VS Code extension build this session runs, from its binary path."""
    if not execpath:
        return None
    for part in reversed(Path(execpath).parents):
        m = _BUILD_RE.match(part.name)
        if m:
            return {"version": m.group(1), "platform": m.group(2), "extensions": str(part.parent)}
    return None


def newest_build(extensions: str, platform: str) -> Optional[str]:
    best = None
    try:
        names = os.listdir(extensions)
    except OSError:
        return None
    for name in names:
        m = _BUILD_RE.match(name)
        if m and m.group(2) == platform and (Path(extensions) / name / "resources").is_dir():
            if best is None or vkey(m.group(1)) > vkey(best):
                best = m.group(1)
    return best


def fingerprint(tools: List[Dict[str, Any]]) -> Dict[str, str]:
    """name -> short hash of what the model sees (description + input schema)."""
    out = {}
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        seen = json.dumps([tool.get("description") or "", tool.get("inputSchema") or {}], sort_keys=True)
        out[str(tool["name"])] = hashlib.sha256(seen.encode("utf-8")).hexdigest()[:16]
    return out


def tool_diff(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, List[str]]:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(n for n in set(before) & set(after) if before[n] != after[n]),
    }


# ---------------------------------------------------------------------------
# What exists
# ---------------------------------------------------------------------------

def latest_published(data: Path, now: float, opener: Callable[..., Any] = urllib.request.urlopen) -> Optional[str]:
    """The version on GitHub main, cached an hour (anonymous read)."""
    cache = data / "latest.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if now - float(cached["at"]) < LATEST_CACHE_S:
            return str(cached["version"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        with opener(urllib.request.Request(LATEST_URL, headers={"Accept": "application/json"}),
                    timeout=NET_TIMEOUT_S) as resp:
            version = str(json.loads(resp.read().decode("utf-8"))["version"])
    except Exception as exc:  # noqa: BLE001 — advisory: a failed read only skips this check
        _log("could not read the published version: {}".format(exc))
        return None
    _write_json(cache, {"at": now, "version": version})
    return version


def installed_copies(root: Path) -> List[str]:
    """Versions installed beside a cache copy (<cache>/<marketplace>/<plugin>/<version>)."""
    try:
        return [p.name for p in root.parent.iterdir()
                if _VERSION_RE.match(p.name) and (p / ".claude-plugin" / "plugin.json").is_file()]
    except OSError:
        return []


def live_state(root: Path) -> Optional[Dict[str, Any]]:
    top = _git(root, "rev-parse", "--show-toplevel", timeout=5)
    if not top:
        return None
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=5)
    if _git(root, "fetch", "--quiet", "origin", "main") is None:
        _log("could not fetch origin/main for {}".format(top))
        return None
    behind = _git(root, "rev-list", "--count", "HEAD..origin/main", timeout=5)
    tip = _git(root, "rev-parse", "--short", "origin/main", timeout=5)
    return {"top": top, "branch": branch, "behind": int(behind or 0), "tip": tip}


def server_tools(server: str, client_factory: Optional[Callable[..., Any]] = None) -> Optional[Dict[str, str]]:
    """The fingerprint of what the server lists for this person, or None when it
    cannot be read (not signed in, server down): the check is skipped, never faked."""
    try:
        from soleon_mcp_client import SoleonMcpClient, SoleonClientError
        client = (client_factory or SoleonMcpClient)(server)
        token = client._resolve_token()
        rpc = client._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, token)
    except Exception as exc:  # noqa: BLE001 — advisory: an unreadable list only skips this check
        _log("could not read the tool list from {}: {}".format(server, type(exc).__name__))
        return None
    tools = (rpc.get("result") or {}).get("tools") if isinstance(rpc, dict) else None
    return fingerprint(tools) if isinstance(tools, list) else None


# ---------------------------------------------------------------------------
# State files (one per session, all under the plugin's data folder)
# ---------------------------------------------------------------------------

def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp{}".format(os.getpid()))
    tmp.write_text(json.dumps(value, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(path))


def state_path(data: Path, session: str) -> Path:
    return data / "fresh-{}.json".format(session)


def ready_path(data: Path, session: str) -> Path:
    """What the prompt gate prints (and deletes): one hook-output JSON object."""
    return data / "fresh-{}.ready".format(session)


def load_state(data: Path, session: str) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(state_path(data, session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def leave_message(data: Path, session: str, lines: List[str]) -> None:
    """Add lines to what the next prompt shows; the person and the model both see them."""
    path = ready_path(data, session)
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        prior = [l for l in str(old.get("systemMessage") or "").split("\n") if l]
    except (OSError, ValueError, AttributeError):
        prior = []
    text = "\n".join(prior + [l for l in lines if l not in prior])
    _write_json(path, {"systemMessage": text, "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "The Soleon toolkit's freshness check told the person:\n" + text}})


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def _names(names: List[str]) -> str:
    shown = ", ".join("`{}`".format(n) for n in names[:NAMES_SHOWN])
    return shown + (" and {} more".format(len(names) - NAMES_SHOWN) if len(names) > NAMES_SHOWN else "")


def plugin_advice(state: Dict[str, Any], data: Path, now: float,
                  opener: Callable[..., Any] = urllib.request.urlopen,
                  live: Callable[[Path], Optional[Dict[str, Any]]] = live_state) -> Tuple[Optional[str], Optional[str]]:
    """(advice, the key it is given once for) — or (None, None)."""
    plugin = state["plugin"]
    root = Path(plugin["root"])
    if plugin["kind"] == "live":
        info = live(root)
        if not info or info["branch"] != "live" or info["behind"] <= 0:
            return None, None  # a development branch is the developer's business
        script = "/".join((info["top"], "harness", "dev_live.py"))  # the checkout's own script
        return ("Soleon toolkit: your live checkout is {} commit(s) behind origin/main. Run "
                "`python3 {} sync`, then /reload-plugins.".format(info["behind"], script),
                "live:" + str(info["tip"]))
    if plugin["kind"] != "copy":
        return None, None
    latest = latest_published(data, now, opener)
    if not latest:
        return None, None
    have = max([plugin["version"]] + installed_copies(root), key=vkey)
    if vkey(latest) > vkey(have):
        return ("Soleon toolkit {} is published; this session runs {}. Run "
                "`/plugin marketplace update {}`, then /reload-plugins.".format(latest, plugin["version"], MARKETPLACE),
                "copy:" + latest)
    if vkey(have) > vkey(plugin["version"]):
        return ("Soleon toolkit {} is installed; this session still runs {}. Run /reload-plugins.".format(
            have, plugin["version"]), "reload:" + have)
    return None, None


def tools_advice(state: Dict[str, Any], current: Dict[str, str]) -> Optional[str]:
    diff = tool_diff(state.get("tools") or {}, current)
    if not any(diff.values()):
        return None
    parts = []
    for word in ("added", "changed", "removed"):
        if diff[word]:
            parts.append("{} {}".format(word, _names(diff[word])))
    return ("Soleon tools changed since this session connected ({}). Run /mcp, pick "
            "plugin:{}:soleon, choose Reconnect.".format("; ".join(parts), state["plugin"]["name"]))


def build_advice(state: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    build = state.get("build")
    if not build or state["plugin"]["kind"] == "generated":
        return None, None  # a generated copy (soleon-prod) runs these hooks too: one plugin says it
    newest = newest_build(build["extensions"], build["platform"])
    if not newest or vkey(newest) <= vkey(build["version"]):
        return None, None
    return ("This conversation runs Claude Code {}; {} is installed. Run Developer: Reload Window in "
            "VS Code, then reopen the conversation.".format(build["version"], newest), "build:" + newest)


def work(data: Path, session: str, now: Optional[float] = None,
         tools_reader: Callable[[str], Optional[Dict[str, str]]] = server_tools,
         opener: Callable[..., Any] = urllib.request.urlopen,
         live: Callable[[Path], Optional[Dict[str, Any]]] = live_state) -> List[str]:
    """One round of checks; returns (and leaves for the next prompt) any advice."""
    now = time.time() if now is None else now
    lock = data / "fresh-{}.lock".format(session)
    if not _take_lock(lock, now):
        return []
    try:
        state = load_state(data, session)
        if state is None:
            return []
        advised = state.setdefault("advised", {})
        lines: List[str] = []
        current = tools_reader(state["plugin"]["server"]) if state["plugin"]["server"] else None
        if current is not None:
            if state.get("tools") is None:
                state["tools"] = current  # the first read after the session connected
            else:
                said = tools_advice(state, current)
                if said:
                    lines.append(said)
                    state["tools"] = current  # once per change: the next diff starts here
        for advice, key, slot in (plugin_advice(state, data, now, opener, live) + ("plugin",),
                                  build_advice(state) + ("build",)):
            if advice and advised.get(slot) != key:
                lines.append(advice)
                advised[slot] = key
        state["checkedAt"] = now
        _write_json(state_path(data, session), state)
        if lines:
            leave_message(data, session, lines)
        return lines
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _take_lock(lock: Path, now: float) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in (0, 1):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(now).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if now - float(lock.read_text(encoding="ascii") or 0) < LOCK_STALE_S:
                    return False
                lock.unlink()
            except (OSError, ValueError):
                return False
    return False


# ---------------------------------------------------------------------------
# Hook entry points
# ---------------------------------------------------------------------------

def session_start(hook: Dict[str, Any], data: Path, loaded: Path, env: Dict[str, str],
                  now: Optional[float] = None) -> Optional[str]:
    """Record what this session runs. A fresh start or a resume reconnects the
    Soleon server, so its tool list is re-read; a /clear or /compact keeps it."""
    session = str(hook.get("session_id") or "")
    if not _SESSION_RE.match(session):
        return None
    now = time.time() if now is None else now
    prior = load_state(data, session)
    fresh = hook.get("source") in ("startup", "resume", "fork") or prior is None
    state = prior if (prior and not fresh) else {}
    state.update({
        "startedAt": state.get("startedAt") or now,
        "plugin": plugin_info(loaded),
        "build": running_build(env.get("CLAUDE_CODE_EXECPATH", "")),
        "tools": None if fresh else state.get("tools"),
        "advised": state.get("advised") or {},
    })
    _write_json(state_path(data, session), state)
    return session


def spawn_worker(data: Path, session: str) -> None:
    """Start `work` detached: the hook returns at once."""
    cmd = [sys.executable, os.path.abspath(__file__), "work", session, "--data", str(data)]
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # detached, own group, no window
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)  # noqa: S603 — our own script, our own interpreter


def main(argv: Optional[List[str]] = None, stdin=None,
         spawn: Callable[[Path, str], None] = spawn_worker) -> int:
    ap = argparse.ArgumentParser(description="Soleon toolkit freshness check")
    ap.add_argument("command", choices=("session-start", "check", "work"))
    ap.add_argument("session", nargs="?")
    ap.add_argument("--data", required=True, type=Path, help="the plugin's data folder")
    ap.add_argument("--loaded", type=Path, help="session-start: the plugin root the session loaded")
    opts = ap.parse_args(argv)
    # Advisory from end to end: whatever goes wrong, the hook exits 0 and the
    # session carries on (a failure is logged to stderr, which Claude Code keeps).
    try:
        if opts.command == "session-start":
            raw = (stdin or sys.stdin).read()
            hook = json.loads(raw) if raw.strip() else {}
            loaded = opts.loaded or Path(os.environ.get("CLAUDE_PLUGIN_ROOT", ""))
            session = session_start(hook, opts.data, loaded, dict(os.environ))
            if session:
                spawn(opts.data, session)
        elif opts.session and _SESSION_RE.match(opts.session):
            if opts.command == "check":
                spawn(opts.data, opts.session)
            else:
                work(opts.data, opts.session)
    except Exception as exc:  # noqa: BLE001 — see above
        _log("{}: {}".format(type(exc).__name__, exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
