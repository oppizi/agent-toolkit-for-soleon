#!/usr/bin/env python3
"""Progress messages for a pulled Soleon agent that runs locally.

On Soleon, an agent whose Progress messages setting is on sends the person
short, kept messages while it is still working: after each finished step the
platform asks the agent's decision model whether to say something now, and
on a yes the agent's own model writes the message; a file the agent handed
over goes with it when the decision picks that file. A local run has no
conversation to post into, so this module does the same thing for it:

* `post-tool` (PostToolUse hook, every tool call inside a pulled agent's
  run) does no network work, so the agent is never held up: it SHOWS the
  messages that are ready (the message in the terminal, its files already
  saved under `<agent dir>/delivered/`), tells the agent what was sent so its
  answer does not repeat it, and starts a detached worker for the rest.
* `work` (that worker, one at a time per agent): sends the next check with
  the steps finished since the last one, waits for its answer, saves the
  message's files and leaves the message for the next `post-tool` to show.
  A message therefore appears one step after the step it reports.
* `finish()` (called by the SubagentStop hook before the turn is recorded):
  shows a check still in flight, then delivers every file the agent handed
  over this turn, as the final answer does on Soleon.
* `stage_local_file()` (called by the agent's tool server): `attach_file` on
  a file in the LOCAL workspace. The platform's `attach_file` reads the
  platform copy of the workspace, which does not have a file the local agent
  just wrote, so the handover is done here: the file is kept as it is at this
  moment and listed as handed over.

The decision, the writer and the setting are the platform's
(`check_agent_progress`); nothing here decides anything. Nothing runs when
the pulled config has Progress messages off. Stdlib only, Python 3.9.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STATE_FILE = ".local-progress.json"        # the worker's own state (and `finish`'s)
HANDED_FILE = ".local-handed.jsonl"        # files the agent handed over (the tool server appends)
READY_FILE = ".local-progress-ready.jsonl"  # messages ready to show (the worker appends, the hook takes)
LOCK_FILE = ".local-progress.lock"         # one worker at a time
JOB_PREFIX = ".local-progress-job-"
OUTBOX_DIR = ".outbox"
DELIVERED_DIR = "delivered"
ON = ("on_demand", "key_milestones", "verbose")

#: What one check may carry (the platform's own limits are the same or higher).
STEPS_MAX = 60
STEP_TEXT_MAX = 4_000
PROMPT_MAX = 32_000
IDENTITY_MAX = 16_000
SKILLS_MAX = 8
SKILL_TEXT_MAX = 12_000
SENT_MAX = 12
FILES_MAX = 20
#: How long a worker waits for its check's answer (a decision plus, on a yes,
#: the agent's model writing the message) before leaving it for the next one.
ANSWER_WAIT_S = 45.0
#: How long the end of the turn waits for a worker or a check still in flight.
FINISH_WAIT_S = 45.0
#: Files of one message downloaded at once.
DOWNLOAD_PARALLEL = 4
#: A lock older than this was left by a worker that died.
LOCK_STALE_S = 180.0
DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024

PLATFORM_TOOL_PREFIX = "mcp__soleon-agent-tools__"
LOCAL_TOOL_PREFIX = "mcp__soleon-workspace__"
_NAME_RE = re.compile(r"[\x00-\x1f/\\]")


def _stderr(msg: str) -> None:
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# The setting, the state, the files handed over
# ---------------------------------------------------------------------------

def setting(agent_dir: Path) -> Dict[str, str]:
    """The pulled config's `loop.progressMessages`, or `{}` when off."""
    try:
        config = json.loads((agent_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    block = (config.get("loop") or {}).get("progressMessages") if isinstance(config, dict) else None
    if not isinstance(block, dict) or block.get("frequency") not in ON:
        return {}
    return {"frequency": block["frequency"], "guidance": str(block.get("guidance") or "")}


def load_state(agent_dir: Path, turn: str) -> Dict[str, Any]:
    """This turn's state; a file left by an earlier turn starts over."""
    try:
        data = json.loads((agent_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or data.get("turn") != turn:
        data = {"turn": turn}
    data.setdefault("covered", [])        # transcript ids already reported
    data.setdefault("stepsSoFar", 0)
    data.setdefault("sent", [])           # messages shown this turn
    data.setdefault("sentFiles", [])      # what already went with a message
    data.setdefault("lastAt", None)
    data.setdefault("pending", None)
    return data


def save_state(agent_dir: Path, state: Dict[str, Any]) -> None:
    path = agent_dir / STATE_FILE
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError as exc:
        _stderr("soleon: could not save the progress state: {}".format(exc))


def safe_name(name: str) -> str:
    cleaned = _NAME_RE.sub("_", str(name or "")).strip(". ") or "file"
    return cleaned[:200]


def stage_local_file(agent_dir: Path, turn: str, path: str, filename: Optional[str] = None) -> Tuple[bool, str]:
    """`attach_file` for a file in the LOCAL workspace. Keeps the file as it
    is now under `.outbox/` and lists it as handed over this turn. Returns
    `(handled, text)`: `handled` False means the file is not in the local
    workspace (the platform's own `attach_file` decides); True with the
    platform's confirmation or error text otherwise."""
    workspace = (agent_dir / "workspace").resolve()
    raw = Path(str(path or ""))
    candidate = (raw if raw.is_absolute() else workspace / raw)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(workspace)
    except (OSError, ValueError):
        if raw.is_absolute():
            return False, ""
        return True, ("Error: {} is outside the workspace — you can only attach files you created in "
                      "your workspace.".format(path))
    if not resolved.is_file():
        return False, ""
    display = safe_name(filename or resolved.name)
    file_id = "local_{}".format(len(handed_files(agent_dir, turn)) + 1)
    staged = agent_dir / OUTBOX_DIR / turn / "{}-{}".format(file_id, display)
    try:
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(resolved), str(staged))
        # Its own append-only file: the tool server writes here while a worker
        # may be writing the state.
        with open(str(agent_dir / HANDED_FILE), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"turn": turn, "id": file_id, "name": display, "path": str(staged)}) + "\n")
    except OSError as exc:
        return True, "Error attaching file: {}".format(exc)
    return True, ("Delivered {} to the user as a downloadable attachment. It appears in the chat "
                  "automatically — do not paste the file contents.".format(display))


def _read_lines(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with open(str(path), "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    out.append(item)
    except OSError:
        pass
    return out


def handed_files(agent_dir: Path, turn: str) -> List[Dict[str, Any]]:
    """The local files the agent handed over this turn, oldest first."""
    return [h for h in _read_lines(agent_dir / HANDED_FILE) if h.get("turn") == turn]


def _deliver_local(agent_dir: Path, entry: Dict[str, Any]) -> Optional[str]:
    target = agent_dir / DELIVERED_DIR / safe_name(entry.get("name") or "file")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(entry["path"]), str(target))
    except (OSError, KeyError) as exc:
        _stderr("soleon: could not deliver {}: {}".format(entry.get("name"), exc))
        return None
    return str(target)


def _download(url: str, target: Path, opener: Optional[Callable[[str], Any]] = None) -> bool:
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
                if size > DOWNLOAD_MAX_BYTES:
                    raise OSError("the file is over the download limit")
                fh.write(chunk)
        os.replace(str(target) + ".part", str(target))
        return True
    except Exception as exc:  # noqa: BLE001 — a failed download is reported, never raised into the hook
        _stderr("soleon: could not download {}: {}".format(target.name, exc))
        return False


def deliver(agent_dir: Path, state: Dict[str, Any], files: List[Dict[str, Any]],
            opener: Optional[Callable[[str], Any]] = None) -> List[str]:
    """Save the files of one message (or of the final answer) under
    `delivered/` and return their paths. A newer version of a name replaces
    the older file, as the newest upload is the one a person opens."""
    handed = {h["id"]: h for h in handed_files(agent_dir, str(state.get("turn") or ""))}

    def one(item: Dict[str, Any]) -> Optional[str]:
        if item.get("source") == "local":
            entry = handed.get(item.get("id"))
            return _deliver_local(agent_dir, entry) if entry else None
        target = agent_dir / DELIVERED_DIR / safe_name(item.get("name") or "file")
        return str(target) if _download(item.get("url") or "", target, opener) else None

    items = [i for i in files or [] if isinstance(i, dict)]
    # The downloads run together (a turn's four SVGs took 5 s one after another); the
    # same name twice keeps the later file, as before, so those run in order.
    names = [safe_name(i.get("name") or "file") for i in items]
    if len(items) > 1 and len(set(names)) == len(names):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(DOWNLOAD_PARALLEL, len(items))) as pool:
            saved = list(pool.map(one, items))
    else:
        saved = [one(i) for i in items]
    return [p for p in saved if p]


# ---------------------------------------------------------------------------
# What happened since the last check (the agent's own transcript)
# ---------------------------------------------------------------------------

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def _short_name(full: str) -> str:
    for prefix in (PLATFORM_TOOL_PREFIX, LOCAL_TOOL_PREFIX):
        if full.startswith(prefix):
            return full[len(prefix):]
    return full


def load_records(path: Optional[str]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not path or not os.path.isfile(path):
        return records
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("message"), dict):
                    records.append(rec)
    except OSError:
        pass
    return records


def read_turn(records: List[Dict[str, Any]], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The run so far: the prompt, when it began, the finished steps in order
    (each `{id, kind, name, args, result, status}`; kind `llm` is something
    the agent said) and the skill documents it read. `current` is the tool
    call the hook fired for, which the transcript may not hold yet."""
    prompt, started_at = "", None
    steps: List[Dict[str, Any]] = []
    open_calls: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        msg = rec["message"]
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        if started_at is None and rec.get("timestamp"):
            started_at = rec["timestamp"]
        if msg.get("role") == "user":
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            if not prompt and not results:
                prompt = _text_of(content)
            for block in results:
                call = open_calls.pop(str(block.get("tool_use_id")), None)
                if call is None:
                    continue
                call["result"] = _text_of(block.get("content"))
                call["status"] = "error" if block.get("is_error") else "success"
                steps.append(call)
        elif msg.get("role") == "assistant":
            text = _text_of(content).strip()
            if text:
                steps.append({"id": "say:" + str(rec.get("uuid") or len(steps)), "kind": "llm", "name": "",
                              "args": {}, "result": text, "status": "success"})
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    open_calls[str(block.get("id"))] = {
                        "id": str(block.get("id")), "kind": "tool", "name": _short_name(str(block.get("name") or "")),
                        "args": block.get("input") if isinstance(block.get("input"), dict) else {}}
    if current and current.get("id") and current["id"] not in {s["id"] for s in steps}:
        steps.append(current)
    return {"prompt": prompt, "startedAt": started_at, "steps": steps}


def current_call(hook: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The tool call a PostToolUse payload reports, as a finished step."""
    use_id, name = hook.get("tool_use_id"), hook.get("tool_name")
    if not isinstance(use_id, str) or not isinstance(name, str) or not name:
        return None
    response = hook.get("tool_response")
    if isinstance(response, (dict, list)):
        text = _text_of(response.get("content") if isinstance(response, dict) else response) or json.dumps(
            response, default=str)
    else:
        text = "" if response is None else str(response)
    failed = isinstance(response, dict) and bool(response.get("is_error") or response.get("isError"))
    return {"id": use_id, "kind": "tool", "name": _short_name(name),
            "args": hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {},
            "result": text, "status": "error" if failed else "success"}


def skills_read(agent_dir: Path, steps: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """The skill documents the agent read this turn (a `read_file` under
    `skills/`), as the platform passes the skills in use to the decision."""
    out: List[Dict[str, str]] = []
    seen = set()
    for step in steps:
        path = str((step.get("args") or {}).get("path") or (step.get("args") or {}).get("file_path") or "")
        norm = path.replace("\\", "/")
        if step.get("kind") != "tool" or "skills/" not in norm or not norm.lower().endswith(".md"):
            continue
        name = norm.split("skills/", 1)[1].split("/", 1)[0]
        if name in seen or not step.get("result"):
            continue
        seen.add(name)
        out.append({"name": name[:128], "text": str(step["result"])[:SKILL_TEXT_MAX]})
    return out[:SKILLS_MAX]


def _epoch(stamp: Any) -> Optional[float]:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def build_check(agent_dir: Path, state: Dict[str, Any], turn: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
    """The next check's arguments, or None when nothing new has finished."""
    covered = set(state["covered"])
    fresh = [s for s in turn["steps"] if s["id"] not in covered]
    if not any(s["kind"] == "tool" for s in fresh):
        return None                      # the agent's words alone never trigger a check
    fresh = fresh[-STEPS_MAX:]
    try:
        identity = (agent_dir / "SOUL.md").read_text(encoding="utf-8")
    except OSError:
        identity = ""
    started = _epoch(turn.get("startedAt"))
    steps = []
    for step in fresh:
        args_json = json.dumps(step.get("args") or {}, default=str)
        steps.append({"kind": step["kind"], "name": step["name"],
                      "args": step.get("args") if len(args_json) <= STEP_TEXT_MAX else {"_truncated": args_json[:STEP_TEXT_MAX]},
                      "result": str(step.get("result") or "")[:STEP_TEXT_MAX], "status": step.get("status") or "success"})
    return {
        "prompt": str(turn.get("prompt") or "")[:PROMPT_MAX],
        "identity": identity[:IDENTITY_MAX],
        "elapsed_s": round(max(0.0, now - started), 1) if started else 0.0,
        "since_last_s": round(max(0.0, now - state["lastAt"]), 1) if state.get("lastAt") else -1,
        "steps_so_far": state["stepsSoFar"] + len(fresh),
        "steps": steps,
        "sent": list(state["sent"])[-SENT_MAX:],
        "skills": skills_read(agent_dir, turn["steps"]),
        "files": [{"id": h["id"], "name": h["name"]} for h in handed_files(agent_dir, state["turn"])][-FILES_MAX:],
        "sent_files": list(state["sentFiles"]),
        "_covers": [s["id"] for s in fresh],
    }


# ---------------------------------------------------------------------------
# Showing a message
# ---------------------------------------------------------------------------

def agent_name(agent_dir: Path) -> str:
    try:
        pull = json.loads((agent_dir / "pull.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pull = {}
    return str(pull.get("displayName") or pull.get("slug") or agent_dir.name)


def shown(name: str, text: str, paths: List[str]) -> str:
    lines = ["Progress message from {}: {}".format(name, text)]
    lines += ["  File: {}".format(p) for p in paths]
    return "\n".join(lines)


def note(text: str, paths: List[str]) -> str:
    """What the agent is told, so its answer does not repeat the message."""
    extra = " with {}".format(", ".join(os.path.basename(p) for p in paths)) if paths else ""
    return ("Soleon already sent the person this progress message{} while you were working. Do not "
            "repeat it in your answer: {}".format(extra, text))


def apply_result(agent_dir: Path, state: Dict[str, Any], result: Dict[str, Any], now: float,
                 opener: Optional[Callable[[str], Any]] = None) -> Optional[Tuple[str, str]]:
    """Act on one check's answer. `(what the person sees, what the agent is
    told)` when a message was sent, else None."""
    if not isinstance(result, dict) or not result.get("send") or not str(result.get("text") or "").strip():
        return None
    text = str(result["text"]).strip()
    files = [f for f in result.get("files") or [] if isinstance(f, dict)]
    paths = deliver(agent_dir, state, files, opener)
    state["sent"].append(text)
    state["lastAt"] = now
    for item in files:
        state["sentFiles"].append(item.get("name") if item.get("source") == "local" else item.get("marker"))
    state["sentFiles"] = [f for f in state["sentFiles"] if isinstance(f, str)]
    return shown(agent_name(agent_dir), text, paths), note(text, paths)


def _result_of(envelope: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
    """`(state, result)` of a control envelope: `pending`, `done` or `error`."""
    if not isinstance(envelope, dict):
        return "error", None
    state = envelope.get("state")
    if state == "pending":
        return "pending", None
    if state == "done" and isinstance(envelope.get("result"), dict):
        return "done", envelope["result"]
    return "error", None


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------

def _context(hook: Dict[str, Any]):
    """`(agent dir, turn id, conversation id, slug, server url)` for a tool
    call inside a pulled agent's own run with Progress messages on, else None."""
    import soleon_message_budget as budget
    from soleon_agent_tools_mcp import EXTRACTION_FILE, TURN_FILE_NAME, ConversationTracker
    from soleon_mcp_client import DEFAULT_SERVER_URL

    agent_id = hook.get("agent_id")
    found = budget._pulled_agent_of(hook) if agent_id else None
    if found is None or found[1]:
        return None                       # not a pulled agent, or one of its helpers
    agent_dir = found[0]
    if (agent_dir / EXTRACTION_FILE).exists() or not setting(agent_dir):
        return None
    try:
        turn_file = json.loads((agent_dir / TURN_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    turn = turn_file.get("turn") if isinstance(turn_file, dict) else None
    if not isinstance(turn, str) or not turn or turn_file.get("agentId") not in (None, agent_id):
        return None
    try:
        pull = json.loads((agent_dir / "pull.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pull = {}
    conversation = ConversationTracker(str(agent_dir / ".local-conversation.json")).current()
    return (agent_dir, turn, conversation, str(pull.get("slug") or agent_dir.name),
            str(pull.get("serverUrl") or DEFAULT_SERVER_URL))


def take_ready(agent_dir: Path) -> List[Dict[str, str]]:
    """The messages a worker left to be shown, taken exactly once."""
    path = agent_dir / READY_FILE
    taken = path.with_name(path.name + ".taken")
    try:
        os.replace(str(path), str(taken))
    except OSError:
        return []
    items = _read_lines(taken)
    try:
        taken.unlink()
    except OSError:
        pass
    return [i for i in items if isinstance(i.get("shown"), str)]


def _leave_ready(agent_dir: Path, said: Tuple[str, str]) -> None:
    with open(str(agent_dir / READY_FILE), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"shown": said[0], "note": said[1]}) + "\n")


#: Windows flags for the worker: its own process group and a console with no window
#: (CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW). Never DETACHED_PROCESS: Windows ignores
#: CREATE_NO_WINDOW beside it, so anything the worker starts would open a visible window.
WORKER_CREATIONFLAGS = 0x00000200 | 0x08000000


def _spawn_worker(job_path: Path) -> None:
    """Start `work` in the background: the hook returns at once and the
    agent's next step never waits for a network call."""
    import subprocess
    cmd = [sys.executable, os.path.abspath(__file__), "work", str(job_path)]
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = WORKER_CREATIONFLAGS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)  # noqa: S603 — our own script, our own interpreter


def post_tool(hook: Dict[str, Any], spawn: Optional[Callable[[Path], None]] = None,
              now: Optional[Callable[[], float]] = None) -> Optional[Dict[str, Any]]:
    """One PostToolUse call: local work only. Returns the hook's JSON output
    (the messages ready to show), or None."""
    ctx = _context(hook)
    if ctx is None:
        return None
    agent_dir, turn_id, conversation, slug, server_url = ctx
    output: Optional[Dict[str, Any]] = None
    ready = take_ready(agent_dir)
    if ready:
        output = {"systemMessage": "\n".join(r["shown"] for r in ready),
                  "hookSpecificOutput": {"hookEventName": "PostToolUse",
                                         "additionalContext": "\n".join(str(r.get("note") or "") for r in ready)}}
    job = {"turn": turn_id, "conversation": conversation, "slug": slug, "serverUrl": server_url,
           "agentDir": str(agent_dir), "agentId": hook.get("agent_id"),
           "transcript": hook.get("transcript_path"), "current": current_call(hook)}
    job_path = agent_dir / "{}{}.json".format(JOB_PREFIX, int(((now or time.time)()) * 1000))
    try:
        job_path.write_text(json.dumps(job, default=str), encoding="utf-8")
        (spawn or _spawn_worker)(job_path)
    except OSError as exc:
        _stderr("soleon: could not start the progress check: {}".format(exc))
    return output


def _take_lock(agent_dir: Path, now: float) -> bool:
    path = agent_dir / LOCK_FILE
    for _attempt in (0, 1):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(now).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if now - float(path.read_text(encoding="ascii") or 0) < LOCK_STALE_S:
                    return False
                path.unlink()             # left by a worker that died
            except (OSError, ValueError):
                return False
        except OSError:
            return False
    return False


def _lock_is_live(agent_dir: Path, now: float) -> bool:
    try:
        return now - float((agent_dir / LOCK_FILE).read_text(encoding="ascii") or 0) < LOCK_STALE_S
    except (OSError, ValueError):
        return False


def _release_lock(agent_dir: Path) -> None:
    try:
        (agent_dir / LOCK_FILE).unlink()
    except OSError:
        pass


def _await_answer(client, slug: str, call_id: str, clock, nap, wait_s: float) -> Tuple[str, Optional[Dict[str, Any]]]:
    deadline = clock() + wait_s
    while True:
        status, result = _result_of(client.call_tool(
            "get_agent_tool_result", {"slug": slug, "app_env": "dev", "call_id": call_id}))
        if status != "pending" or clock() >= deadline:
            return status, result
        nap(1.0)


def work(job: Dict[str, Any], client_factory=None, now: Optional[Callable[[], float]] = None,
         sleep: Optional[Callable[[float], None]] = None,
         opener: Optional[Callable[[str], Any]] = None) -> str:
    """The detached worker: one check at a time per agent. Picks up a check an
    earlier worker left unanswered, then sends the next one with everything
    finished since and waits for its answer. A message that is sent is saved
    (its files delivered) and left for the next `post-tool` to show. Returns
    what happened, for logs and tests."""
    import soleon_message_budget as budget
    from soleon_mcp_client import SoleonClientError, SoleonMcpClient

    agent_dir = Path(job["agentDir"])
    clock, nap = now or time.time, sleep or time.sleep
    if not _take_lock(agent_dir, clock()):
        return "busy"                     # the worker already running reports these steps with its next check
    state = load_state(agent_dir, job["turn"])
    try:
        client = (client_factory or SoleonMcpClient)(job["serverUrl"])
        pending = state.get("pending")
        if pending:
            status, result = _await_answer(client, job["slug"], pending["callId"], clock, nap, ANSWER_WAIT_S)
            if status == "pending":
                return "still_pending"
            state["pending"] = None
            said = apply_result(agent_dir, state, result, clock(), opener) if status == "done" else None
            if said:
                _leave_ready(agent_dir, said)
        turn = read_turn(load_records(budget.subagent_transcript(job.get("transcript"), str(job.get("agentId")))),
                         job.get("current"))
        check = build_check(agent_dir, state, turn, clock())
        if check is None:
            return "nothing_new"
        covers = check.pop("_covers")
        envelope = client.call_tool("check_agent_progress", dict(
            check, slug=job["slug"], app_env="dev", conversation=job["conversation"], turn=job["turn"], wait=False))
        status, result = _result_of(envelope)
        state["covered"] = (state["covered"] + covers)[-2000:]
        state["stepsSoFar"] = check["steps_so_far"]
        if status == "pending" and envelope.get("call_id"):
            state["pending"] = {"callId": envelope["call_id"], "at": clock()}
            save_state(agent_dir, state)
            status, result = _await_answer(client, job["slug"], envelope["call_id"], clock, nap, ANSWER_WAIT_S)
            if status == "pending":
                return "still_pending"
            state["pending"] = None
        if status == "done":
            said = apply_result(agent_dir, state, result, clock(), opener)
            if said:
                _leave_ready(agent_dir, said)
                return "sent"
        return "no"
    except SoleonClientError as exc:
        _stderr("soleon: progress check failed — {}".format(exc))
        return "failed"
    finally:
        save_state(agent_dir, state)
        _release_lock(agent_dir)


def finish(agent_dir: Path, turn_id: str, conversation: str, slug: str, server_url: str, *,
           platform_tools_used: bool, client_factory=None, now: Optional[Callable[[], float]] = None,
           sleep: Optional[Callable[[float], None]] = None,
           opener: Optional[Callable[[str], Any]] = None,
           on_checked: Optional[Callable[[], None]] = None) -> List[str]:
    """The end of a turn: show a check still in flight, then deliver every
    file the agent handed over (the final answer carries them all). Returns
    the lines to show the person. Never raises.

    `on_checked` is called once, as soon as the platform's final check has
    answered (or was not needed): the stop hook starts the turn's record then,
    so it runs while the files download instead of after them."""
    from soleon_mcp_client import SoleonClientError, SoleonMcpClient

    told = [False]

    def checked() -> None:
        if on_checked is not None and not told[0]:
            told[0] = True
            on_checked()

    clock, nap = now or time.time, sleep or time.sleep
    # A worker may still be waiting for its check's answer: let it finish.
    deadline = clock() + FINISH_WAIT_S
    while _lock_is_live(agent_dir, clock()) and clock() < deadline:
        nap(0.5)
    state = load_state(agent_dir, turn_id)
    lines: List[str] = [r["shown"] for r in take_ready(agent_dir)]
    client = None
    try:
        pending = state.get("pending")
        if pending and setting(agent_dir):
            client = (client_factory or SoleonMcpClient)(server_url)
            status, result = _await_answer(client, slug, pending["callId"], clock, nap, FINISH_WAIT_S)
            state["pending"] = None
            if status == "done":
                said = apply_result(agent_dir, state, result, clock(), opener)
                if said:
                    lines.append(said[0])
        files: List[Dict[str, Any]] = [{"source": "local", "id": h["id"], "name": h["name"]}
                                       for h in handed_files(agent_dir, turn_id)]
        # The newest version of each local name is the one the answer carries.
        newest: Dict[str, Dict[str, Any]] = {}
        for item in files:
            newest[item["name"]] = item
        files = list(newest.values())
        paths = deliver(agent_dir, state, files, opener)
        if not platform_tools_used:
            checked()
        if platform_tools_used:
            try:
                client = client or (client_factory or SoleonMcpClient)(server_url)
                envelope = client.call_tool("check_agent_progress", {
                    "slug": slug, "app_env": "dev", "conversation": conversation, "turn": turn_id, "final": True})
                while isinstance(envelope, dict) and envelope.get("state") == "pending" and envelope.get("call_id"):
                    nap(1.0)
                    envelope = client.call_tool("get_agent_tool_result",
                                                {"slug": slug, "app_env": "dev", "call_id": envelope["call_id"]})
                checked()
                status, result = _result_of(envelope)
                if status == "done":
                    paths += deliver(agent_dir, state,
                                     [f for f in result.get("files") or [] if isinstance(f, dict)], opener)
            except SoleonClientError as exc:
                _stderr("soleon: the turn's platform-made files could not be fetched — {}".format(exc))
        if paths:
            lines.append("\n".join(["Files delivered with {}'s answer:".format(agent_name(agent_dir))]
                                   + ["  File: {}".format(p) for p in paths]))
    except SoleonClientError as exc:
        # Not shown to the person: a server without check_agent_progress (an
        # environment the feature has not reached) answers every turn this way.
        _stderr("soleon: the turn's platform-made files could not be fetched — {}".format(exc))
    finally:
        checked()
        leftovers = [agent_dir / STATE_FILE, agent_dir / HANDED_FILE, agent_dir / READY_FILE, agent_dir / LOCK_FILE]
        try:
            leftovers += [q for q in agent_dir.iterdir() if q.name.startswith(JOB_PREFIX)]
        except OSError:
            pass
        for path in leftovers:
            try:
                path.unlink()
            except OSError:
                pass
        shutil.rmtree(str(agent_dir / OUTBOX_DIR / turn_id), ignore_errors=True)
    return lines


def main(argv: Optional[List[str]] = None, stream=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "work":
        job_path = Path(argv[1])
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job_path.unlink()
            work(job)
        except Exception as exc:  # noqa: BLE001 — a detached worker has nobody to raise to
            _stderr("soleon: progress worker failed — {}: {}".format(type(exc).__name__, exc))
        return 0
    if argv != ["post-tool"]:
        _stderr("usage: soleon_progress.py post-tool  (hook JSON on stdin) | work <job file>")
        return 2
    raw = (stream or sys.stdin).read()
    try:
        hook = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        hook = {}
    try:
        output = post_tool(hook if isinstance(hook, dict) else {})
    except Exception as exc:  # noqa: BLE001 — a progress check never fails the agent's tool call
        _stderr("soleon: progress check failed — {}: {}".format(type(exc).__name__, exc))
        return 0
    if output:
        sys.stdout.write(json.dumps(output) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
