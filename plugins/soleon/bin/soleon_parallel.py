"""A step's tool calls run together, as on Soleon.

The platform runs every call of one model step at the same time (agent-infra
`containers/maverick-agent/maverick/agent/loop.py`: `bounded_gather` over
`_in_file_order`), at most the agent's fan-out at once
(`containers/shared/effort.py` `_TOOL_FANOUT_LIMIT`: 2/4/6/8 for
swift/balanced/thorough/exhaustive), and a file call on a path that an EARLIER
call of the same step writes waits for that write to finish.

Locally two things stood in the way, and both are needed:

- Claude Code sends a step's MCP calls together only for tools marked
  `readOnlyHint` (measured 2026-11-23: two 4 s calls took 8 s unmarked, 4 s
  marked). `mark_concurrent` adds the mark. It carries no permission meaning
  here: the pull pre-approves both of the agent's servers by name, and
  approval-gated tools stay unmarked, so they still run one at a time.
- The stdio servers answered one request at a time. `ParallelCalls` runs each
  `tools/call` on its own thread, in the order received, holding back a call
  on a path an earlier call of the batch writes, and writes each answer whole.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict, Optional

#: The platform's fan-out per effort stop (effort.py STOPS / _TOOL_FANOUT_LIMIT).
FANOUT = {"swift": 2, "balanced": 4, "thorough": 6, "exhaustive": 8}
DEFAULT_FANOUT = FANOUT["balanced"]  # effort.py: default and ceiling "balanced"

#: loop.py _FILE_WRITE_TOOLS / _FILE_PATH_TOOLS.
FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
FILE_PATH_TOOLS = FILE_WRITE_TOOLS | {"read_file"}


def fanout_of(agent_dir: Optional[str]) -> int:
    """The agent's fan-out: its config's `loop.effort` default stop (capped by
    its ceiling, as the platform resolves it), else balanced."""
    stops = list(FANOUT)
    try:
        with open(os.path.join(agent_dir or "", "config.json"), "r", encoding="utf-8") as fh:
            effort = ((json.load(fh) or {}).get("loop") or {}).get("effort") or {}
    except (OSError, ValueError, AttributeError):
        return DEFAULT_FANOUT
    if not isinstance(effort, dict):
        return DEFAULT_FANOUT
    level = effort.get("default") if effort.get("default") in FANOUT else "balanced"
    ceiling = effort.get("ceiling") if effort.get("ceiling") in FANOUT else None
    if ceiling and stops.index(level) > stops.index(ceiling):
        level = ceiling
    return FANOUT[level]


def file_path_of(name: str, arguments: Any) -> Optional[str]:
    """loop.py _file_path_of: the normalised path of a file call, else None."""
    if name not in FILE_PATH_TOOLS or not isinstance(arguments, dict):
        return None
    path = arguments.get("path")
    return path.strip().lstrip("./").rstrip("/") if isinstance(path, str) and path.strip() else None


def mark_concurrent(tool: Dict[str, Any]) -> Dict[str, Any]:
    """A published tool Claude Code may run alongside the step's other calls."""
    return dict(tool, annotations=dict(tool.get("annotations") or {}, readOnlyHint=True))


class ParallelCalls:
    """`submit(msg, handle)` runs `handle(msg)` on its own thread and writes
    the answer with `write(resp)`. At most `limit` run at once; a call waits
    (without holding a place) for an earlier call that writes its path."""

    def __init__(self, write: Callable[[Dict[str, Any]], None], limit: int = DEFAULT_FANOUT,
                 log: Callable[[str], None] = lambda _m: None):
        self._write = write
        self._places = threading.BoundedSemaphore(max(1, int(limit)))
        self._lock = threading.Lock()
        self._last_write: Dict[str, threading.Event] = {}
        self._log = log
        self._threads: list = []

    def submit(self, msg: Dict[str, Any], handle: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]) -> None:
        params = msg.get("params") or {}
        name = str(params.get("name") or "")
        path = file_path_of(name, params.get("arguments"))
        with self._lock:
            before = self._last_write.get(path) if path else None
            done = threading.Event() if path and name in FILE_WRITE_TOOLS else None
            if done is not None:
                self._last_write[path] = done
            self._threads = [t for t in self._threads if t.is_alive()]
            t = threading.Thread(target=self._run, args=(msg, handle, before, done, path), daemon=True,
                                 name="call-{}".format(msg.get("id")))
            self._threads.append(t)
        t.start()

    def _run(self, msg, handle, before, done, path) -> None:
        resp: Optional[Dict[str, Any]]
        try:
            if before is not None:
                before.wait()
            with self._places:
                resp = handle(msg)
        except Exception as exc:  # noqa: BLE001 — one bad call must not kill the server
            self._log("internal error on {}: {}".format(msg.get("method"), type(exc).__name__))
            resp = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(exc)}}
        finally:
            if done is not None:
                done.set()
                with self._lock:
                    if self._last_write.get(path) is done:
                        del self._last_write[path]
        if resp is not None:
            self._write(resp)

    def join(self, timeout_s: Optional[float] = None) -> None:
        """Waits for the calls in flight (tests, and a clean end of input)."""
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(timeout_s)


def line_writer(out) -> Callable[[Dict[str, Any]], None]:
    """One JSON-RPC answer per line, never two interleaved."""
    lock = threading.Lock()

    def write(resp: Dict[str, Any]) -> None:
        data = (json.dumps(resp) + "\n").encode("utf-8")
        with lock:
            out.write(data)
            out.flush()
    return write
