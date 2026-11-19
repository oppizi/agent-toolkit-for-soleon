#!/usr/bin/env python3
"""Progress messages for a pulled Soleon agent that runs locally.

On Soleon, an agent whose Progress messages setting is on sends the person
short, kept messages while it is still working: after each finished step the
platform asks the agent's decision model whether to say something now, and
on a yes the agent's own model writes the message; a file the agent handed
over goes with it when the decision picks that file. A local run has no
conversation to post into, so this module does the same thing for it:

* `post-tool` (PostToolUse hook, every tool call inside a pulled agent's
  run): picks up the answer of the previous check and SHOWS it (the message
  in the terminal, its files saved under `<agent dir>/delivered/`), tells the
  agent what was sent so its answer does not repeat it, then sends the next
  check with the steps finished since the last one. The check is never waited
  for, so the agent is not held up: a message appears one step later.
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

STATE_FILE = ".local-progress.json"
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
#: A check younger than this is left for the next tool call: reading it would
#: make the agent wait for the decision.
COLLECT_AFTER_S = 2.0
#: How long the end of the turn waits for a check still in flight.
FINISH_WAIT_S = 25.0
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
    data.setdefault("handed", [])         # local files the agent handed over
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
    state = load_state(agent_dir, turn)
    file_id = "local_{}".format(len(state["handed"]) + 1)
    staged = agent_dir / OUTBOX_DIR / turn / "{}-{}".format(file_id, display)
    try:
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(resolved), str(staged))
    except OSError as exc:
        return True, "Error attaching file: {}".format(exc)
    state["handed"].append({"id": file_id, "name": display, "path": str(staged)})
    save_state(agent_dir, state)
    return True, ("Delivered {} to the user as a downloadable attachment. It appears in the chat "
                  "automatically — do not paste the file contents.".format(display))


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
    handed = {h["id"]: h for h in state.get("handed") or []}
    paths: List[str] = []
    for item in files or []:
        if not isinstance(item, dict):
            continue
        if item.get("source") == "local":
            entry = handed.get(item.get("id"))
            saved = _deliver_local(agent_dir, entry) if entry else None
        else:
            target = agent_dir / DELIVERED_DIR / safe_name(item.get("name") or "file")
            saved = str(target) if _download(item.get("url") or "", target, opener) else None
        if saved:
            paths.append(saved)
    return paths


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
        "files": [{"id": h["id"], "name": h["name"]} for h in state["handed"]][-FILES_MAX:],
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


def post_tool(hook: Dict[str, Any], client_factory=None, now: Optional[Callable[[], float]] = None,
              opener: Optional[Callable[[str], Any]] = None) -> Optional[Dict[str, Any]]:
    """One PostToolUse call. Returns the hook's JSON output, or None."""
    ctx = _context(hook)
    if ctx is None:
        return None
    import soleon_message_budget as budget
    from soleon_mcp_client import SoleonClientError, SoleonMcpClient

    agent_dir, turn_id, conversation, slug, server_url = ctx
    clock = now or time.time
    state = load_state(agent_dir, turn_id)
    client = None
    output: Optional[Dict[str, Any]] = None
    try:
        pending = state.get("pending")
        if pending:
            if clock() - float(pending.get("at") or 0) < COLLECT_AFTER_S:
                return None
            client = (client_factory or SoleonMcpClient)(server_url)
            envelope = client.call_tool("get_agent_tool_result",
                                        {"slug": slug, "app_env": "dev", "call_id": pending["callId"]})
            status, result = _result_of(envelope)
            if status == "pending":
                return None                # still deciding: one check at a time
            state["pending"] = None
            if status == "done":
                said = apply_result(agent_dir, state, result, clock(), opener)
                if said:
                    output = {"systemMessage": said[0],
                              "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": said[1]}}
        turn = read_turn(load_records(budget.subagent_transcript(hook.get("transcript_path"), str(hook.get("agent_id")))),
                         current_call(hook))
        check = build_check(agent_dir, state, turn, clock())
        if check is not None:
            covers = check.pop("_covers")
            client = client or (client_factory or SoleonMcpClient)(server_url)
            envelope = client.call_tool("check_agent_progress", dict(
                check, slug=slug, app_env="dev", conversation=conversation, turn=turn_id, wait=False))
            status, result = _result_of(envelope)
            state["covered"] = (state["covered"] + covers)[-2000:]
            state["stepsSoFar"] = check["steps_so_far"]
            if status == "pending" and envelope.get("call_id"):
                state["pending"] = {"callId": envelope["call_id"], "at": clock()}
            elif status == "done":
                said = apply_result(agent_dir, state, result, clock(), opener)
                if said and output is None:
                    output = {"systemMessage": said[0],
                              "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": said[1]}}
    except SoleonClientError as exc:
        _stderr("soleon: progress check failed — {}".format(exc))
    finally:
        save_state(agent_dir, state)
    return output


def finish(agent_dir: Path, turn_id: str, conversation: str, slug: str, server_url: str, *,
           platform_tools_used: bool, client_factory=None, now: Optional[Callable[[], float]] = None,
           sleep: Optional[Callable[[float], None]] = None,
           opener: Optional[Callable[[str], Any]] = None) -> List[str]:
    """The end of a turn: show a check still in flight, then deliver every
    file the agent handed over (the final answer carries them all). Returns
    the lines to show the person. Never raises."""
    from soleon_mcp_client import SoleonClientError, SoleonMcpClient

    clock, nap = now or time.time, sleep or time.sleep
    state = load_state(agent_dir, turn_id)
    lines: List[str] = []
    client = None
    try:
        pending = state.get("pending")
        if pending and setting(agent_dir):
            client = (client_factory or SoleonMcpClient)(server_url)
            deadline = clock() + FINISH_WAIT_S
            while True:
                envelope = client.call_tool("get_agent_tool_result",
                                            {"slug": slug, "app_env": "dev", "call_id": pending["callId"]})
                status, result = _result_of(envelope)
                if status != "pending" or clock() >= deadline:
                    break
                nap(1.0)
            state["pending"] = None
            if status == "done":
                said = apply_result(agent_dir, state, result, clock(), opener)
                if said:
                    lines.append(said[0])
        files: List[Dict[str, Any]] = [{"source": "local", "id": h["id"], "name": h["name"]}
                                       for h in state.get("handed") or []]
        # The newest version of each local name is the one the answer carries.
        newest: Dict[str, Dict[str, Any]] = {}
        for item in files:
            newest[item["name"]] = item
        files = list(newest.values())
        paths = deliver(agent_dir, state, files, opener)
        if platform_tools_used:
            try:
                client = client or (client_factory or SoleonMcpClient)(server_url)
                envelope = client.call_tool("check_agent_progress", {
                    "slug": slug, "app_env": "dev", "conversation": conversation, "turn": turn_id, "final": True})
                while isinstance(envelope, dict) and envelope.get("state") == "pending" and envelope.get("call_id"):
                    nap(1.0)
                    envelope = client.call_tool("get_agent_tool_result",
                                                {"slug": slug, "app_env": "dev", "call_id": envelope["call_id"]})
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
        try:
            (agent_dir / STATE_FILE).unlink()
        except OSError:
            pass
        shutil.rmtree(str(agent_dir / OUTBOX_DIR / turn_id), ignore_errors=True)
    return lines


def main(argv: Optional[List[str]] = None, stream=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["post-tool"]:
        _stderr("usage: soleon_progress.py post-tool  (hook JSON on stdin)")
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
