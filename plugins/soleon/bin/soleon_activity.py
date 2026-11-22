"""Live activity for a local run — the feed an app gets from Soleon's API.

An app calling an agent through Soleon's API can ask for the agent's live
activity (`soleon_activity` on POST /v1/responses): every step the agent
finishes, with the status line's safe label and the workspace files the step
saved, of the media types the app asked for. A pulled agent run locally gives
an app the SAME feed, so an app that shows it works the same on both.

How an app asks: it starts the local run (`claude -p ...`) with the
environment variable SOLEON_ACTIVITY holding the request's own spec plus the
file the feed is written to:

    SOLEON_ACTIVITY='{"feed": "/path/run-1.jsonl", "files": ["image/*", "application/json"],
                      "max_file_bytes": 10000000}'

Each platform tool call the agent makes then appends ONE line to `feed`, in
the shape of an API `soleon_activity` entry, with each file saved next to the
feed instead of linked:

    {"id": "act_...", "created_at": 1795000000.0, "label": "Working…", "tool": "write_file",
     "files": [{"filename": "001.json", "path": "studio/t1/001.json", "mime_type": "application/json",
                "size": 120, "local_path": "/path/run-1-files/0001/001.json"}]}

The files are the ones the platform says the call saved (`savedFiles` on its
reply, each with a short download link). Nothing runs on the agent's path:
the reply goes back to the agent at once and a background thread writes the
feed. Without SOLEON_ACTIVITY nothing is written.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ENV = "SOLEON_ACTIVITY"
DEFAULT_MAX_FILE_BYTES = 10_000_000
MAX_FILE_BYTES = 50_000_000
MAX_FILES_PER_ENTRY = 20
_NAME_RE = re.compile(r"[\x00-\x1f/\\:*?\"<>|]")


def _stderr(msg: str) -> None:
    sys.stderr.write("[soleon-activity] {}\n".format(msg))
    sys.stderr.flush()


def spec(environ: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """The run's activity request, or None (not asked, or unusable — said once)."""
    raw = (environ if environ is not None else os.environ).get(ENV, "")
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        _stderr("{} is not JSON; no activity is written".format(ENV))
        return None
    if not isinstance(value, dict) or not isinstance(value.get("feed"), str) or not value["feed"].strip():
        _stderr("{} needs a \"feed\" file; no activity is written".format(ENV))
        return None
    files = value.get("files") or []
    limit = value.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES)
    if not isinstance(files, list) or isinstance(limit, bool) or not isinstance(limit, int):
        _stderr("{}: files must be a list and max_file_bytes a number; no activity is written".format(ENV))
        return None
    return {"feed": value["feed"], "files": [str(t).strip().lower() for t in files],
            "max_file_bytes": max(1, min(int(limit), MAX_FILE_BYTES))}


#: Types matched only when named exactly, as on Soleon: a wildcard never
#: matches content that can run scripts when rendered (HTML, script, and
#: every XML type: SVG, XSLT, RSS...).
ACTIVE_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/javascript", "application/javascript",
                          "application/ecmascript", "text/ecmascript", "text/xsl"})


def is_active_type(ct: str) -> bool:
    return ct in ACTIVE_TYPES or ct.endswith("+xml") or ct.endswith("/xml")


def type_matches(content_type: str, patterns: List[str]) -> bool:
    """Same rule as the platform's (activity_feed.type_matches)."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        return False
    for p in patterns or []:
        if p == ct:
            return True
        if not is_active_type(ct) and (p == "*/*" or (p.endswith("/*") and ct.startswith(p[:-1]))):
            return True
    return False


CODE_RUN_TOOLS = ("run_code_file", "write_and_run_code")
_MCP_RE = re.compile(r"^(?:mcp|custom)_(?P<server>[a-z]{1,6}_[0-9a-f]{6,32}(?=_|\Z)|[^_:.\s]+)", re.I)


def label_for(platform_name: str, entry: Optional[Dict[str, Any]] = None) -> str:
    """The status line's safe label for a finished tool step, by the same
    rules as the platform's (trace_stream_router/emit_status.safe_label for
    a `tool:` step, then its server-name fallback). Never the arguments."""
    name = str(platform_name or "")
    low = name.lower()
    entry = entry or {}
    if low.startswith("kb_") or re.search(r"kb_[a-z0-9_]*read", low):
        return "Searching the knowledge base…"
    if low == "read_file":
        return "Reading files…"
    if low.startswith("web_prompt"):
        return "Searching the web…"
    if low.startswith("filesystem_prompt"):
        return "Working with files…"
    if "browser" in low:
        return "Browsing…"
    if low in CODE_RUN_TOOLS:
        # On Soleon a code run's own steps read "Running Python…"; a local
        # feed has only the call, so it carries that label.
        return "Running Python…"
    server_name = str(entry.get("serverName") or "").strip()
    m = _MCP_RE.match(name)
    if m:
        return "Using {}…".format(server_name or m.group("server"))
    if server_name:
        return "Using {}…".format(server_name)
    return "Working…"


def _safe(name: str) -> str:
    return (_NAME_RE.sub("_", str(name or "")).strip(". ") or "file")[:200]


def _download(url: str, target: Path, limit: int, opener: Optional[Callable[[str], Any]] = None) -> bool:
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        resp = (opener or (lambda u: urllib.request.urlopen(u, timeout=60)))(url)  # noqa: S310 — https only
        size = 0
        with open(str(target) + ".part", "wb") as fh:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise OSError("the file is over max_file_bytes")
                fh.write(chunk)
        os.replace(str(target) + ".part", str(target))
        return True
    except Exception as exc:  # noqa: BLE001 — a missed file is reported, never raised into the tool server
        _stderr("could not fetch {}: {}".format(target.name, exc))
        return False


def build_entry(s: Dict[str, Any], *, seq: int, platform_name: str, entry: Optional[Dict[str, Any]],
                saved: List[Dict[str, Any]], turn: str, at: float,
                opener: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """One feed line: the call's label and the files it saved that the run
    asked for, each saved next to the feed (`local_path`), or named without
    one when over the limit or not fetchable."""
    feed = Path(s["feed"])
    folder = feed.parent / "{}-files".format(feed.stem) / "{:04d}".format(seq)
    files = []
    for f in saved or []:
        if not isinstance(f, dict) or not f.get("path"):
            continue
        ctype = str(f.get("contentType") or "application/octet-stream")
        if not type_matches(ctype, s["files"]):
            continue
        path = str(f["path"])
        size = int(f.get("size") or 0)
        item = {"filename": path.rsplit("/", 1)[-1], "path": path, "mime_type": ctype, "size": size}
        target = folder / _safe(item["filename"])
        if size <= s["max_file_bytes"] and _download(f.get("url") or "", target, s["max_file_bytes"], opener):
            item["local_path"] = str(target)
        files.append(item)
        if len(files) >= MAX_FILES_PER_ENTRY:
            break
    key = "{}#{}#{}".format(turn, seq, platform_name)
    return {"id": "act_" + hashlib.sha256(key.encode()).hexdigest()[:24], "created_at": float(at),
            "label": label_for(platform_name, entry), "tool": platform_name, "files": files}


class Feed:
    """Writes the feed on one background thread, in call order."""

    def __init__(self, s: Dict[str, Any], opener: Optional[Callable[[str], Any]] = None):
        self.spec = s
        self.opener = opener
        self._q: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._seq = 0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def add(self, platform_name: str, entry: Optional[Dict[str, Any]], saved: List[Dict[str, Any]],
            turn: str) -> None:
        with self._lock:
            self._seq += 1
            self._q.put((self._seq, platform_name, entry, list(saved or []), turn, time.time()))
            if self._thread is None or not self._thread.is_alive():
                # Not a daemon: the last entries are written before the process ends.
                self._thread = threading.Thread(target=self._run, name="soleon-activity", daemon=False)
                self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=5.0)
            except queue.Empty:
                return
            if item is None:
                return
            seq, name, entry, saved, turn, at = item
            try:
                line = build_entry(self.spec, seq=seq, platform_name=name, entry=entry, saved=saved,
                                   turn=turn, at=at, opener=self.opener)
                feed = Path(self.spec["feed"])
                feed.parent.mkdir(parents=True, exist_ok=True)
                with open(str(feed), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(line) + "\n")
            except Exception as exc:  # noqa: BLE001 — a missed entry, never a failed tool call
                _stderr("could not write an activity entry for {}: {}".format(name, exc))

    def flush(self, timeout: float = 30.0) -> None:
        """Wait for the entries added so far (tests; the end of a run)."""
        deadline = time.time() + timeout
        while not self._q.empty() and time.time() < deadline:
            time.sleep(0.02)
        if self._thread is not None:
            self._thread.join(max(0.0, deadline - time.time()))


_feed: Optional[Feed] = None
_feed_lock = threading.Lock()


def current() -> Optional[Feed]:
    """This process's feed, when the run asked for one."""
    global _feed
    with _feed_lock:
        if _feed is None:
            s = spec()
            if s is None:
                return None
            _feed = Feed(s)
        return _feed


def saved_files_of(envelope: Any) -> List[Dict[str, Any]]:
    """`savedFiles` from a `call_agent_tool` reply (done: `result.savedFiles`)."""
    if not isinstance(envelope, dict):
        return []
    result = envelope.get("result")
    files = result.get("savedFiles") if isinstance(result, dict) else None
    if files is None:
        files = envelope.get("savedFiles")
    return [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []
