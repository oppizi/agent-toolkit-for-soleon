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
import time
from typing import Any, Callable, Dict, List, Optional

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


#: How long the first platform call of a step waits for the step's other calls
#: before it goes: Claude Code sends a step's calls within a few milliseconds.
BATCH_WINDOW_S = 0.05
#: The platform's cap on one batch (agent-infra local_control TOOL_CALLS_MAX).
BATCH_MAX = 16

_BATCH_DEFAULT_NEXT = ("Read error/detail; fix the call (or ask the person, for "
                       "approval_required) and retry.")


def split_batch(envelope: Any, count: int) -> List[Any]:
    """The answer of each call of a finished `call_agent_tools`, shaped exactly
    as `call_agent_tool` answers that call alone. A batch that failed as a whole
    is every call's answer."""
    if not isinstance(envelope, dict) or envelope.get("state") != "done":
        return [envelope] * count
    result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
    results = result.get("results")
    if not isinstance(results, list) or len(results) != count:
        bad = {"status": 200, "state": "error", "call_id": envelope.get("call_id"), "op": "tool_call",
               "error": "batch_result_malformed",
               "detail": "Soleon answered the step's calls without one answer per call.",
               "next_action": "Retry the call."}
        return [bad] * count
    common = {"call_id": envelope.get("call_id"), "op": "tool_call", "session_id": envelope.get("session_id")}
    if envelope.get("turn_id"):
        common["turn_id"] = envelope["turn_id"]
    out = []
    for r in results:
        r = r if isinstance(r, dict) else {"error": "batch_result_malformed", "detail": str(r)[:200]}
        if r.get("error"):
            out.append({"status": 200, "state": "error", **common, "error": r["error"],
                        "detail": r.get("detail") or "", "next_action": r.get("next_action") or _BATCH_DEFAULT_NEXT,
                        **{k: v for k, v in r.items() if k in ("tool", "missing", "preview", "elapsed_ms")}})
        else:
            out.append({"status": 200, "state": "done", **common, "result": r})
    return out


class PlatformBatcher:
    """A step's platform calls go to Soleon as ONE `call_agent_tools` request,
    which runs them together in one turn, as the agent's own loop runs a step's
    calls on the platform. A call alone goes as `call_agent_tool`, as before.

    `run(call)` blocks until that call's answer is in. `call` is the
    `call_agent_tool` arguments of one call (slug, app_env, name, args,
    approved, conversation, turn, ...). `send_one(call)` and
    `send_batch(calls)` make the request and follow it to its end (polling
    included); `send_batch` raises `BatchUnsupported` when the platform has no
    such op, and the calls then go one by one from then on."""

    def __init__(self, send_one: Callable[[Dict[str, Any]], Any],
                 send_batch: Callable[[List[Dict[str, Any]]], Any],
                 window_s: float = BATCH_WINDOW_S, log: Callable[[str], None] = lambda _m: None):
        self._send_one = send_one
        self._send_batch = send_batch
        self._window_s = window_s
        self._log = log
        self._lock = threading.Lock()
        self._pending: List[Dict[str, Any]] = []
        self._supported = True

    @staticmethod
    def _key(call: Dict[str, Any]) -> tuple:
        return tuple(call.get(k) for k in ("slug", "app_env", "conversation", "turn"))

    def run(self, call: Dict[str, Any]) -> Any:
        # An approved call, or one made under an extraction job, is the
        # person's yes or a job's own op: it goes alone, as before.
        if not self._supported or call.get("approved") or any(k.startswith("extraction") for k in call):
            return self._send_one(call)
        slot = {"call": call, "done": threading.Event(), "answer": None, "error": None}
        with self._lock:
            self._pending.append(slot)
            leader = len(self._pending) == 1
        if leader:
            time.sleep(self._window_s)
            with self._lock:
                group, self._pending = self._pending, []
            self._flush(group)
        slot["done"].wait()
        if slot["error"] is not None:
            raise slot["error"]
        return slot["answer"]

    def _flush(self, group: List[Dict[str, Any]]) -> None:
        by_key: Dict[tuple, List[Dict[str, Any]]] = {}
        for s in group:
            by_key.setdefault(self._key(s["call"]), []).append(s)
        threads = []
        for slots in by_key.values():
            for i in range(0, len(slots), BATCH_MAX):
                chunk = slots[i:i + BATCH_MAX]
                t = threading.Thread(target=self._send, args=(chunk,), daemon=True, name="batch")
                t.start()
                threads.append(t)
        for t in threads:
            t.join()

    def _send(self, slots: List[Dict[str, Any]]) -> None:
        try:
            if len(slots) == 1 or not self._supported:
                for s in slots:
                    self._deliver(s, lambda c=s["call"]: self._send_one(c))
                return
            try:
                envelope = self._send_batch([s["call"] for s in slots])
            except BatchUnsupported as exc:
                self._log("the platform runs no batches ({}); calls go one by one".format(exc))
                self._supported = False
                threads = [threading.Thread(target=self._deliver, args=(s, lambda c=s["call"]: self._send_one(c)),
                                            daemon=True) for s in slots]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                return
            except Exception as exc:  # noqa: BLE001 — every call of the batch gets the batch's failure
                for s in slots:
                    s["error"] = exc
                    s["done"].set()
                return
            self._log("{} calls ran together ({})".format(
                len(slots), ", ".join(str(s["call"].get("name")) for s in slots)))
            for s, answer in zip(slots, split_batch(envelope, len(slots))):
                s["answer"] = answer
                s["done"].set()
        finally:
            for s in slots:
                s["done"].set()

    @staticmethod
    def _deliver(slot: Dict[str, Any], send: Callable[[], Any]) -> None:
        try:
            slot["answer"] = send()
        except Exception as exc:  # noqa: BLE001 — raised in the caller's thread
            slot["error"] = exc
        finally:
            slot["done"].set()


class BatchUnsupported(Exception):
    """The platform has no `call_agent_tools` / `tool_calls` op (an older Soleon)."""


# --- One batch for the agent's two servers -----------------------------------------------------------------
# The file tools (soleon-workspace) and the other tools (soleon-agent-tools) are two processes, so a step that
# writes a note and fetches a page made two requests, and Soleon's queue ran them one after the other. The
# agent-tools server hosts a broker on 127.0.0.1; the workspace server hands its platform calls to it, so the
# step's calls from both servers join one batch. Without a broker (an older server, a worker) each server
# batches its own calls, as before.

def broker_file(agent_dir: str, parent: Optional[int] = None) -> str:
    """Where the broker of THIS Claude Code session (the servers' common parent) is announced."""
    return os.path.join(agent_dir, ".batch-broker-{}.json".format(os.getppid() if parent is None else parent))


#: The exception classes a broker call may raise, by name, re-raised as the same class on the other side.
_ERRORS: Dict[str, Any] = {}


def register_errors(*classes: Any) -> None:
    for c in classes:
        _ERRORS[c.__name__] = c


class BatchBroker:
    """Serves `PlatformBatcher.run` to the session's other server: one JSON line in ({token, call}), one out
    ({answer} or {error: {type, message, payload}}). Only callers holding the token in the announcement file
    (readable by this user only) are served."""

    def __init__(self, agent_dir: str, batcher: "PlatformBatcher", log: Callable[[str], None] = lambda _m: None):
        import socket
        self._batcher = batcher
        self._log = log
        self._token = os.urandom(16).hex()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(32)
        self.port = self._sock.getsockname()[1]
        self.path = broker_file(agent_dir)
        tmp = "{}.{}.tmp".format(self.path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"port": self.port, "token": self._token, "pid": os.getpid()}, fh)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def start(self) -> "BatchBroker":
        threading.Thread(target=self._accept, daemon=True, name="batch-broker").start()
        return self

    def close(self) -> None:
        try:
            self._sock.close()
        finally:
            try:
                os.remove(self.path)
            except OSError:
                pass

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True, name="batch-broker-call").start()

    def _serve(self, conn) -> None:
        with conn:
            try:
                req = json.loads(_read_line(conn))
                if req.get("token") != self._token:
                    raise PermissionError("wrong broker token")
                out = {"answer": self._batcher.run(req["call"])}
            except Exception as exc:  # noqa: BLE001 — the caller re-raises it as the same class
                out = {"error": {"type": type(exc).__name__, "message": str(exc),
                                 "payload": getattr(exc, "payload", None)}}
            try:
                conn.sendall((json.dumps(out, default=str) + "\n").encode("utf-8"))
            except OSError as exc:
                self._log("batch broker: could not answer a call ({})".format(exc))


def _read_line(conn) -> str:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8")


class RemoteBatcher:
    """`run(call)` through the session's broker, so this server's calls join the other server's batch; the
    local batcher when there is no broker (or it cannot be reached)."""

    def __init__(self, agent_dir: str, local: "PlatformBatcher", log: Callable[[str], None] = lambda _m: None):
        self._agent_dir = agent_dir
        self._local = local
        self._log = log

    def _broker(self) -> Optional[Dict[str, Any]]:
        try:
            with open(broker_file(self._agent_dir), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) and data.get("port") and data.get("token") else None
        except (OSError, ValueError):
            return None

    def run(self, call: Dict[str, Any]) -> Any:
        import socket
        b = self._broker()
        if b is None:
            return self._local.run(call)
        try:
            conn = socket.create_connection(("127.0.0.1", int(b["port"])), timeout=2)
        except OSError as exc:
            self._log("batch broker unreachable ({}); this server batches alone".format(exc))
            return self._local.run(call)
        with conn:
            conn.settimeout(None)  # a call runs as long as the platform takes
            conn.sendall((json.dumps({"token": b["token"], "call": call}) + "\n").encode("utf-8"))
            out = json.loads(_read_line(conn) or "{}")
        if "answer" in out:
            return out["answer"]
        err = out.get("error") or {"type": "RuntimeError", "message": "the batch broker answered nothing"}
        cls = _ERRORS.get(err.get("type"))
        if cls is None:
            raise RuntimeError("{}: {}".format(err.get("type"), err.get("message")))
        try:
            exc = cls(err.get("message"), err.get("payload")) if err.get("payload") is not None else cls(err.get("message"))
        except TypeError:
            exc = cls(err.get("message"))
        raise exc
