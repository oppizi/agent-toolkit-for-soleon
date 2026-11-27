#!/usr/bin/env python3
"""SubagentStart / SubagentStop hooks: one run of a pulled agent's subagent
becomes ONE turn on the platform trace — the prompt it received, every tool
call it made, and its final answer — instead of a bare list of tool calls.

Wired by the plugin's `hooks/hooks.json`:

    python3 soleon_turn_hooks.py start   # SubagentStart
    python3 soleon_turn_hooks.py stop    # SubagentStop

Both read the hook payload on stdin and exit 0 immediately when the subagent
is not a pulled Soleon agent (no `<cwd>/.soleon/agents/<agent_type>/` dir),
so every other subagent in the project is untouched.

`start` mints a turn id and writes `<agent dir>/.local-turn.json`
(`{turn, agentId, startedAt, source: "hook"}`). The agent's inline tool
server (`soleon_agent_tools_mcp.py`) adopts that id on its first call, so the
platform files every tool call of this run under the same TURN row.

`stop` reads the subagent's own transcript (`agent_transcript_path`), takes
the prompt (the first user message), the answer (`last_assistant_message`),
the tool calls it made — the ones served locally (workspace tools, anything
not routed through the platform) become steps of the turn; the platform-run
ones are already steps — and sends `record_agent_turn`. The platform then
shows the turn as: prompt, tool steps, answer.

A failed record never blocks the session: the reason goes to stderr and to
the user as a systemMessage (exit 0). Token handling is
`soleon_mcp_client.py`'s (Claude Code's own OAuth token; never logged).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from soleon_agent_tools_mcp import (  # noqa: E402
    EXTRACTION_FILE,
    TURN_FILE_NAME,
    ConversationTracker,
    await_control,
    extraction_args,
    mint_turn_id,
)
from soleon_mcp_client import (  # noqa: E402
    DEFAULT_SERVER_URL,
    SoleonClientError,
    SoleonMcpClient,
    describe_error,
    is_platform_error,
)

EXIT_OK = 0

#: The tool servers a pulled agent's file declares inline (pull_agent.py).
PLATFORM_TOOL_PREFIX = "mcp__soleon-agent-tools__"
LOCAL_TOOL_PREFIX = "mcp__soleon-workspace__"

#: Caps mirrored from the platform's turn_record (its own limits are higher;
#: these keep the report a summary and the SQS envelope well under 256 KiB).
TEXT_MAX = 32_000
STEP_RESULT_MAX = 4_000
STEP_ARGS_MAX = 4_000
STEPS_MAX = 60


def _stderr(msg: str) -> None:
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.stderr.flush()


def read_hook_input(stream=None) -> Dict[str, Any]:
    raw = (stream or sys.stdin).read()
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def agent_dir_of(hook: Dict[str, Any]) -> Optional[Path]:
    """`<cwd>/.soleon/agents/<agent_type>` when this subagent is a pulled
    Soleon agent, else None (the hook must be free for every other agent)."""
    agent_type = hook.get("agent_type")
    cwd = hook.get("cwd") or os.getcwd()
    if not isinstance(agent_type, str) or not agent_type or "/" in agent_type or agent_type.startswith("."):
        return None
    candidate = Path(cwd) / ".soleon" / "agents" / agent_type
    if not (candidate / "pull.json").is_file():
        return None
    return candidate


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def sent_at_line(when: Optional[datetime] = None) -> str:
    """The platform's ``Sent at:`` line, exactly as maverick writes it at the top of every user message
    (`maverick/utils/helpers.py::current_time_str`): local time, weekday, zone and UTC offset."""
    now = (when or datetime.now(timezone.utc)).astimezone()
    offset = now.strftime("%z")
    offset_fmt = "{}:{}".format(offset[:3], offset[3:]) if len(offset) == 5 else offset
    tz_name = time.strftime("%Z") or "UTC"
    return "Sent at: {} ({}, UTC{})".format(now.strftime("%Y-%m-%d %H:%M (%A)"), tz_name, offset_fmt)


def start(hook: Dict[str, Any], now=None, out=None) -> int:
    agent_dir = agent_dir_of(hook)
    if agent_dir is None:
        return EXIT_OK
    now = now or time.time
    record = {"turn": mint_turn_id(), "agentId": hook.get("agent_id"), "startedAt": now(), "source": "hook"}
    try:
        (agent_dir / TURN_FILE_NAME).write_text(json.dumps(record), encoding="utf-8")
    except OSError as exc:
        _stderr("soleon: could not write the turn file: {}".format(exc))
    # On Soleon every user message opens with a "Sent at:" line, and agents rely on it: the code tool's
    # request_id takes its time, a skill names a job's folder by it. A local run had no such line, so the agent
    # guessed a time or made digits up, which repeat from one run to the next (flyer-builder 2026-11-27: three
    # builds refused as "Request ID already belongs to different code or scope"). Give the subagent the same
    # line before its first prompt (SubagentStart additionalContext reaches the subagent's own context).
    (out or sys.stdout).write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SubagentStart",
        "additionalContext": sent_at_line(datetime.fromtimestamp(now(), timezone.utc))}}) + "\n")
    return EXIT_OK


# ---------------------------------------------------------------------------
# stop — transcript → turn record
# ---------------------------------------------------------------------------

def _iso(ts: Any) -> Optional[str]:
    """Claude Code stamps `2026-09-19T16:50:58.375Z`; the platform stores
    `+00:00` offsets — normalise so a turn's duration parses either way."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def load_transcript(path: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("message"), dict):
                records.append(rec)
    return records


def turn_from_transcript(records: List[Dict[str, Any]], last_assistant_message: Optional[str] = None) -> Dict[str, Any]:
    """The turn a subagent transcript describes: prompt, answer, timing, the
    locally-run tool calls (as steps) and every tool name used."""
    prompt = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    tools_used: List[str] = []
    steps: List[Dict[str, Any]] = []
    pending: Dict[str, Dict[str, Any]] = {}
    last_text = ""
    for rec in records:
        msg = rec["message"]
        role = msg.get("role")
        ts = rec.get("timestamp")
        if started_at is None and ts:
            started_at = _iso(ts)
        if ts:
            completed_at = _iso(ts) or completed_at
        content = msg.get("content")
        if role == "user":
            if not prompt and not any(isinstance(b, dict) and b.get("type") == "tool_result"
                                      for b in (content if isinstance(content, list) else [])):
                prompt = _text_of(content)
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    step = pending.pop(str(block.get("tool_use_id")), None)
                    if step is None:
                        continue
                    result_text = _text_of(block.get("content"))
                    step["result"] = result_text[:STEP_RESULT_MAX]
                    step["completed_at"] = _iso(ts)
                    if block.get("is_error"):
                        step["status"] = "error"
                        step["error"] = result_text[:2000]
                    if not step.pop("_platform"):
                        steps.append(step)
        elif role == "assistant":
            text = _text_of(content)
            if text.strip():
                last_text = text
            for block in content if isinstance(content, list) else []:
                if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                    continue
                full = str(block.get("name") or "")
                if full.startswith(PLATFORM_TOOL_PREFIX):
                    name, platform = full[len(PLATFORM_TOOL_PREFIX):], True
                elif full.startswith(LOCAL_TOOL_PREFIX):
                    name, platform = full[len(LOCAL_TOOL_PREFIX):], False
                else:
                    name, platform = full, False
                if name and name not in tools_used:
                    tools_used.append(name)
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                args_json = json.dumps(args, default=str)
                if len(args_json) > STEP_ARGS_MAX:
                    args = {"_truncated": args_json[:STEP_ARGS_MAX]}
                pending[str(block.get("id"))] = {
                    "name": name, "args": args, "result": "", "status": "success",
                    "started_at": _iso(ts), "_platform": platform,
                }
    # a call the transcript never answered (the run was cut short)
    for step in pending.values():
        if not step.pop("_platform"):
            step["status"] = "error"
            step["error"] = "no result recorded before the agent stopped"
            steps.append(step)
    response = (last_assistant_message if isinstance(last_assistant_message, str) and last_assistant_message.strip()
                else last_text)
    return {
        "prompt": prompt[:TEXT_MAX],
        "response": response[:TEXT_MAX],
        "status": "completed" if response.strip() else "failed",
        "started_at": started_at,
        "completed_at": completed_at,
        "steps": steps[:STEPS_MAX],
        "tools_used": tools_used[:STEPS_MAX],
    }


def resolve_turn_id(agent_dir: Path, agent_id: Optional[str], started_at_epoch: Optional[float]) -> str:
    """The id this run's tool calls were sent with: the hook/server file when
    it is this run's (same agent id, or written for this run's window); else
    a fresh id (a turn with no platform tool calls still gets recorded)."""
    path = agent_dir / TURN_FILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    try:
        path.unlink()
    except OSError:
        pass
    if isinstance(data, dict) and isinstance(data.get("turn"), str) and data["turn"]:
        if agent_id and data.get("agentId") == agent_id:
            return data["turn"]
        written = data.get("startedAt")
        if data.get("agentId") is None and isinstance(written, (int, float)) and started_at_epoch is not None \
                and written >= started_at_epoch - 60:
            return data["turn"]
    return mint_turn_id()


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def stop(hook: Dict[str, Any], client_factory=SoleonMcpClient, now=None) -> int:
    agent_dir = agent_dir_of(hook)
    if agent_dir is None:
        return EXIT_OK
    # AHP-1834 — no answer while a background call is open (as on Soleon):
    # the agent is sent back to wait for it or stop it. The turn is recorded
    # when it really ends.
    import soleon_background
    reason = soleon_background.hold_answer(agent_dir)
    if reason:
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
        sys.stdout.flush()
        return EXIT_OK
    transcript_path = hook.get("agent_transcript_path")
    if not isinstance(transcript_path, str) or not os.path.isfile(transcript_path):
        _stderr("soleon: SubagentStop carried no readable agent_transcript_path — turn not recorded")
        return EXIT_OK
    try:
        records = load_transcript(transcript_path)
    except OSError as exc:
        _stderr("soleon: could not read the subagent transcript: {}".format(exc))
        return EXIT_OK
    turn = turn_from_transcript(records, hook.get("last_assistant_message"))
    turn_id = resolve_turn_id(agent_dir, hook.get("agent_id"), _epoch(turn.get("started_at")))
    try:
        pull = json.loads((agent_dir / "pull.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pull = {}
    slug = str(pull.get("slug") or hook.get("agent_type"))
    server_url = str(pull.get("serverUrl") or DEFAULT_SERVER_URL)
    conversation = ConversationTracker(str(agent_dir / ".local-conversation.json"), now=now).current()
    # Before the answer is shown: a progress message still being decided, and
    # every file the agent handed over this turn (soleon_progress.py).
    local_names = {s["name"] for s in turn["steps"]}
    shown: List[str] = []
    clients: List[Any] = []
    clients_lock = threading.Lock()

    def shared_client(url: str = server_url):
        # One client (one token read) for the turn's files and its record.
        with clients_lock:
            if not clients:
                clients.append(client_factory(url))
            return clients[0]

    payload = {
        "slug": slug, "app_env": "dev", "conversation": conversation, "turn": turn_id,
        "prompt": turn["prompt"], "response": turn["response"], "status": turn["status"],
        "steps": turn["steps"], "tools_used": turn["tools_used"],
    }
    if turn.get("started_at"):
        payload["started_at"] = turn["started_at"]
    if turn.get("completed_at"):
        payload["completed_at"] = turn["completed_at"]
    # Under a LOCAL extraction job the turn's calls ran on the job's session;
    # the record goes there too.
    payload.update(extraction_args(str(agent_dir / EXTRACTION_FILE)))
    # The record goes as soon as the platform's final check has answered, and
    # runs while the turn's files download (soleon_progress.finish on_checked):
    # it was the last 3 s of every turn, after the downloads.
    recorded: Dict[str, Any] = {}

    def record() -> None:
        try:
            client = shared_client()
            envelope = client.call_tool("record_agent_turn", payload)
            recorded["envelope"] = await_control(client, slug, envelope, app_env="dev", interval_s=1.0)
        except SoleonClientError as exc:
            recorded["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 — reported as not recorded, never raised into the hook
            recorded["error"] = "{}: {}".format(type(exc).__name__, exc)

    recorder = threading.Thread(target=record, name="soleon-record-turn")
    try:
        import soleon_progress
        shown = soleon_progress.finish(
            agent_dir, turn_id, conversation, slug, server_url,
            # An extraction job's calls ran on the job's own session, which has
            # no files to hand to a person.
            platform_tools_used=(any(name not in local_names for name in turn["tools_used"])
                                 and not (agent_dir / EXTRACTION_FILE).exists()),
            client_factory=shared_client, now=now, on_checked=recorder.start)
    except Exception as exc:  # noqa: BLE001 — never block the turn's record
        _stderr("soleon: could not finish the turn's progress messages and files: {}".format(exc))
    if not recorder.is_alive() and not recorded:
        try:
            recorder.start()
        except RuntimeError:  # already started and already done
            pass
    recorder.join()
    if "error" in recorded:
        return _not_recorded(recorded["error"], shown)
    envelope = recorded.get("envelope")
    if not isinstance(envelope, dict) or is_platform_error(envelope) or envelope.get("state") != "done":
        detail = describe_error(envelope) if isinstance(envelope, dict) else str(envelope)
        return _not_recorded(detail, shown)
    if shown:
        sys.stdout.write(json.dumps({"systemMessage": "\n".join(shown)}) + "\n")
        sys.stdout.flush()
    return EXIT_OK


def _not_recorded(reason: str, shown: Optional[List[str]] = None) -> int:
    msg = "Soleon: this turn was NOT recorded on the platform trace — {}".format(reason)
    _stderr(msg)
    sys.stdout.write(json.dumps({"systemMessage": "\n".join(list(shown or []) + [msg])}) + "\n")
    sys.stdout.flush()
    return EXIT_OK


def main(argv: Optional[List[str]] = None, stream=None, client_factory=SoleonMcpClient) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in ("start", "stop"):
        _stderr("usage: soleon_turn_hooks.py start|stop  (hook JSON on stdin)")
        return 2
    hook = read_hook_input(stream)
    if argv[0] == "start":
        return start(hook)
    return stop(hook, client_factory=client_factory)


if __name__ == "__main__":
    sys.exit(main())
