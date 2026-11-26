#!/usr/bin/env python3
"""Background calls for a pulled agent's local run (AHP-1834).

On Soleon, every helper and workflow call runs in the background: the call
answers at once with an id, the agent keeps working, and the result reaches
it in the step after it finishes. This is the same for a pulled agent: its
tool server (``soleon_agent_tools_mcp.py``) starts each configured subagent,
workflow, ``spawn_worker`` and subagent-mode integration helper here, in a
thread, and answers with the id. A tool server cannot speak to the model by
itself, so a finished result rides on the agent's next tool call; the agent
can also wait for one (``wait_for_background``), stop one
(``stop_background``) or run any other tool the same way
(``run_in_background``).

The agent cannot finish its run while a call is open: the server keeps the
open calls in ``<agent dir>/.background-open.json`` and the SubagentStop hook
(``soleon_turn_hooks.py``) holds the answer back, twice, then asks this
server to cancel them (``.background-cancel``). Nothing outlives the server
either: when it exits, every call still running is cancelled.

Locally, "interrupt" ends a helper like "cancel": a headless Claude Code run
has no way to hand back what it has so far.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

LIMIT = 8
MAX_WAIT_S = 300.0
WAIT_POLL_S = 2.0
OPEN_FILE = ".background-open.json"
HOLDS_FILE = ".background-holds.json"
CANCEL_FILE = ".background-cancel"

WAIT_TOOL = "wait_for_background"
STOP_TOOL = "stop_background"
RUN_TOOL = "run_in_background"
TOOL_NAMES = frozenset({WAIT_TOOL, STOP_TOOL, RUN_TOOL})

_local = threading.local()


def track(proc: Any) -> None:
    """A helper process started inside a background call: stopping the call
    kills it."""
    call = getattr(_local, "call", None)
    if call is not None:
        call.procs.append(proc)
        if call.stopped:
            _kill(proc)


def in_call() -> bool:
    return getattr(_local, "call", None) is not None


def _kill(proc: Any) -> None:
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 — it may have exited already
        pass


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return "{} s".format(seconds)
    m, s = divmod(seconds, 60)
    return "{} min {} s".format(m, s) if s else "{} min".format(m)


class Call:
    def __init__(self, call_id: str, name: str):
        self.id, self.name = call_id, name
        self.started = time.monotonic()
        self.finished: Optional[float] = None
        self.result: Optional[Dict[str, Any]] = None
        self.delivered = False
        self.stopped: Optional[str] = None
        self.procs: List[Any] = []

    @property
    def done(self) -> bool:
        return self.finished is not None

    def seconds(self) -> float:
        return (self.finished or time.monotonic()) - self.started


class BackgroundCalls:
    def __init__(self, agent_dir: Optional[str] = None, limit: int = LIMIT, watch: bool = True):
        self.agent_dir = Path(agent_dir) if agent_dir else None
        self._limit = limit
        self._places = threading.BoundedSemaphore(limit)
        self._cond = threading.Condition()
        self._calls: Dict[str, Call] = {}
        self._seq = 0
        self._closed = False
        self._write_state()
        if watch and self.agent_dir is not None:
            threading.Thread(target=self._watch_cancel, daemon=True).start()

    # ── state the stop hook reads ─────────────────────────────────────────
    def _write_state(self) -> None:
        if self.agent_dir is None:
            return
        open_calls = [{"id": c.id, "name": c.name, "done": c.done, "seconds": int(c.seconds())}
                      for c in self._calls.values() if not c.delivered]
        try:
            path = self.agent_dir / OPEN_FILE
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"pid": os.getpid(), "open": open_calls}), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _progress(self) -> None:
        """The agent got a result or started a call: the hook's count of held answers restarts."""
        if self.agent_dir is not None:
            try:
                (self.agent_dir / HOLDS_FILE).unlink()
            except OSError:
                pass

    def _watch_cancel(self) -> None:
        marker = self.agent_dir / CANCEL_FILE
        while not self._closed:
            if marker.exists():
                try:
                    marker.unlink()
                except OSError:
                    pass
                self.cancel_all()
            time.sleep(1.0)

    # ── starting and finishing ────────────────────────────────────────────
    def start(self, name: str, run: Callable[[], Dict[str, Any]]) -> Call:
        with self._cond:
            self._seq += 1
            call = Call("bg_{}".format(self._seq), name)
            self._calls[call.id] = call

        def body() -> None:
            _local.call = call
            try:
                with self._places:
                    result = run() if not call.stopped else None
            except Exception as exc:  # noqa: BLE001 — becomes the call's result
                result = {"content": [{"type": "text", "text": "{} failed: {}: {}".format(
                    name, type(exc).__name__, exc)}], "isError": True}
            finally:
                _local.call = None
            with self._cond:
                call.result = result
                call.finished = time.monotonic()
                if call.stopped == "cancel":
                    call.delivered = True
                elif call.stopped == "interrupt":
                    call.result = {"content": [{"type": "text", "text": (
                        "[{} was stopped. A local helper cannot hand back what it had so far: call it again "
                        "with a smaller task if you need part of it.]".format(name))}], "isError": True}
                self._write_state()
                self._cond.notify_all()

        threading.Thread(target=body, daemon=True, name="background-" + call.id).start()
        with self._cond:
            self._write_state()
        self._progress()
        return call

    def started_text(self, call: Call) -> str:
        with self._cond:
            running = sum(1 for c in self._calls.values() if not c.done)
        waiting = running > self._limit
        return ("Started in the background as {id}.{wait} Carry on with other work. Its result will arrive "
                "with your next tool call after it finishes. Call {w} only when you have nothing else to do, "
                "or {s} if you no longer need it.").format(
            id=call.id, w=WAIT_TOOL, s=STOP_TOOL,
            wait=" {} calls are already running, so it starts when one of them finishes.".format(self._limit)
            if waiting else "")

    def take_finished(self) -> List[Call]:
        with self._cond:
            out = [c for c in self._calls.values() if c.done and not c.delivered]
            for c in out:
                c.delivered = True
            if out:
                self._write_state()
        if out:
            self._progress()
        return out

    def describe(self, calls: Optional[List[Call]] = None) -> str:
        with self._cond:
            calls = [c for c in self._calls.values() if not c.delivered] if calls is None else calls
            return ", ".join("{} ({}, {})".format(c.id, c.name, "finished" if c.done else
                                                   "running for " + _duration(c.seconds()))
                             for c in calls) or "none"

    @staticmethod
    def delivery(calls: List[Call]) -> List[Dict[str, Any]]:
        """Content parts that carry finished calls' results on a tool result."""
        parts: List[Dict[str, Any]] = []
        for c in calls:
            result = c.result or {"content": [{"type": "text", "text": "(no result)"}]}
            parts.append({"type": "text", "text": "\n\n[Background call {} ({}) finished after {}{}. Its result:]".format(
                c.id, c.name, _duration(c.seconds()), " and failed" if result.get("isError") else "")})
            parts.extend(result.get("content") or [{"type": "text", "text": "(empty)"}])
        return parts

    # ── the agent's tools ─────────────────────────────────────────────────
    def wait(self, ids: Optional[List[str]] = None, timeout_s: Optional[float] = None,
             sleep: Callable[[float], None] = None) -> str:
        with self._cond:
            unknown = [i for i in ids or [] if i not in self._calls]
            if unknown:
                return "Error: no background call {}. Background calls: {}.".format(", ".join(unknown), self.describe())
            watched = [self._calls[i] for i in ids] if ids else None
            if not (watched or any(not c.delivered for c in self._calls.values())):
                return "No background calls are running."
            limit = MAX_WAIT_S if timeout_s is None else max(0.0, min(float(timeout_s), MAX_WAIT_S))
            deadline = time.monotonic() + limit

            def ready() -> bool:
                if watched is not None:
                    return all(c.done for c in watched)
                return any(c.done and not c.delivered for c in self._calls.values())

            while not ready():
                left = deadline - time.monotonic()
                if left <= 0:
                    running = [c for c in self._calls.values() if not c.done]
                    return "Still running after {}: {}. Carry on, wait again, or call {}.".format(
                        _duration(limit), self.describe(running), STOP_TOOL)
                self._cond.wait(timeout=min(WAIT_POLL_S, left))
        return "Finished. The results follow."

    def stop(self, call_id: str, mode: str) -> str:
        with self._cond:
            call = self._calls.get(call_id)
            if call is None:
                return "Error: no background call {}. Background calls: {}.".format(call_id, self.describe())
            if mode not in ("cancel", "interrupt"):
                return 'Error: mode must be "cancel" or "interrupt".'
            if call.done:
                if mode == "cancel" and not call.delivered:
                    call.delivered = True
                    self._write_state()
                    return "{} had already finished; its result is dropped.".format(call_id)
                return "{} had already finished.".format(call_id)
            call.stopped = mode
            if mode == "cancel":
                call.delivered = True
            procs = list(call.procs)
            self._write_state()
        for proc in procs:
            _kill(proc)
        if mode == "cancel":
            return "{} ({}) is cancelled. Nothing comes back from it.".format(call_id, call.name)
        with self._cond:
            self._cond.wait_for(lambda: call.done, timeout=10)
        return "{} ({}) is stopped; what it says follows.".format(call_id, call.name)

    def cancel_all(self) -> None:
        with self._cond:
            calls = [c for c in self._calls.values() if not c.done]
            for c in calls:
                c.stopped = "cancel"
            for c in self._calls.values():
                c.delivered = True
            procs = [p for c in calls for p in c.procs]
            self._write_state()
        for proc in procs:
            _kill(proc)

    def close(self) -> None:
        self.cancel_all()
        self._closed = True
        if self.agent_dir is not None:
            for name in (OPEN_FILE, HOLDS_FILE):
                try:
                    (self.agent_dir / name).unlink()
                except OSError:
                    pass


# ── the stop hook's side (soleon_turn_hooks.py) ──────────────────────────

#: Answers held back before the hook cancels the calls instead.
MAX_HOLDS = 2


def hold_answer(agent_dir: Path) -> Optional[str]:
    """The reason to hold the agent's answer back, or None to let it finish.

    Called by the SubagentStop hook. Calls are open → hold, at most
    ``MAX_HOLDS`` times in a row; after that the calls are cancelled (the
    server watches ``CANCEL_FILE``) and the answer goes."""
    try:
        state = json.loads((agent_dir / OPEN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    open_calls = state.get("open") or []
    if not open_calls:
        return None
    try:
        holds = int(json.loads((agent_dir / HOLDS_FILE).read_text(encoding="utf-8")).get("holds") or 0)
    except (OSError, ValueError):
        holds = 0
    if holds >= MAX_HOLDS:
        try:
            (agent_dir / CANCEL_FILE).write_text("cancel", encoding="utf-8")
            (agent_dir / HOLDS_FILE).unlink()
        except OSError:
            pass
        return None
    try:
        (agent_dir / HOLDS_FILE).write_text(json.dumps({"holds": holds + 1}), encoding="utf-8")
    except OSError:
        pass
    listed = ", ".join("{} ({}, {})".format(c.get("id"), c.get("name"), "finished" if c.get("done") else
                                             "running for " + _duration(c.get("seconds") or 0))
                       for c in open_calls)
    return ("Your answer was NOT sent: these background calls have not given you their results yet: {}. "
            "Call {} to get them, or {} for any you no longer need, then write your answer again.").format(
        listed, WAIT_TOOL, STOP_TOOL)


# ── the three tools the model sees ───────────────────────────────────────

TOOLS = [
    {"name": WAIT_TOOL, "description": (
        "Wait for background calls to finish. Use it only when you have no other work to do. With no ids, it "
        "returns as soon as any background call finishes; with ids, when all of them have finished. It waits "
        "at most 5 minutes. The finished calls' results come with its answer."),
     "inputSchema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string"},
                 "description": "The background ids to wait for, e.g. [\"bg_1\"]. Leave out to wait for the next one."},
         "timeout_s": {"type": "number", "description": "At most this many seconds (up to 300)."}}}},
    {"name": STOP_TOOL, "description": (
        "Stop a background call you no longer need. mode \"cancel\" ends it and nothing comes back. In a local "
        "run \"interrupt\" also ends it: a local helper cannot hand back what it had so far."),
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "The background id, e.g. \"bg_2\"."},
         "mode": {"type": "string", "enum": ["cancel", "interrupt"]}}, "required": ["id", "mode"]}},
    {"name": RUN_TOOL, "description": (
        "Run one of your other tools in the background, so you can keep working while it runs. Use it for a call "
        "you expect to be slow whose result you do not need for your very next step. It answers at once with a "
        "background id; the result comes with a later tool call. Helpers and workflows always run in the "
        "background already, so do not use this for them."),
     "inputSchema": {"type": "object", "properties": {
         "tool": {"type": "string", "description": (
             "The tool's name, exactly as you would call it (with or without its mcp__soleon-agent-tools__ "
             "prefix).")},
         "arguments": {"type": "object", "description": "The tool's arguments."}},
         "required": ["tool", "arguments"]}},
]
