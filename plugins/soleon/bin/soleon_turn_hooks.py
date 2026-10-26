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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from soleon_agent_tools_mcp import (  # noqa: E402
    TURN_FILE_NAME,
    ConversationTracker,
    await_control,
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

def start(hook: Dict[str, Any], now=None) -> int:
    agent_dir = agent_dir_of(hook)
    if agent_dir is None:
        return EXIT_OK
    now = now or time.time
    record = {"turn": mint_turn_id(), "agentId": hook.get("agent_id"), "startedAt": now(), "source": "hook"}
    try:
        (agent_dir / TURN_FILE_NAME).write_text(json.dumps(record), encoding="utf-8")
    except OSError as exc:
        _stderr("soleon: could not write the turn file: {}".format(exc))
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
    payload = {
        "slug": slug, "app_env": "dev", "conversation": conversation, "turn": turn_id,
        "prompt": turn["prompt"], "response": turn["response"], "status": turn["status"],
        "steps": turn["steps"], "tools_used": turn["tools_used"],
    }
    if turn.get("started_at"):
        payload["started_at"] = turn["started_at"]
    if turn.get("completed_at"):
        payload["completed_at"] = turn["completed_at"]
    try:
        client = client_factory(server_url)
        envelope = client.call_tool("record_agent_turn", payload)
        envelope = await_control(client, slug, envelope, app_env="dev", interval_s=1.0)
    except SoleonClientError as exc:
        return _not_recorded(str(exc))
    if not isinstance(envelope, dict) or is_platform_error(envelope) or envelope.get("state") != "done":
        detail = describe_error(envelope) if isinstance(envelope, dict) else str(envelope)
        return _not_recorded(detail)
    return EXIT_OK


def _not_recorded(reason: str) -> int:
    msg = "Soleon: this turn was NOT recorded on the platform trace — {}".format(reason)
    _stderr(msg)
    sys.stdout.write(json.dumps({"systemMessage": msg}) + "\n")
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
