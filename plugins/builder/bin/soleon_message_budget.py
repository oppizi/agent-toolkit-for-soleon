#!/usr/bin/env python3
"""The agent's Per Message Token Budget, enforced on a LOCAL run the way
Soleon enforces it: one allowance for the whole message — the agent's own
model calls AND every helper (integration worker) it starts.

Soleon counts every input token (cached reads and writes included) plus the
output of every model call in the turn (`proxy._accumulate_dispatch_tokens`).
The agent stops using tools once the turn has spent WIND_DOWN_FRACTION of the
budget (`token_budget.wind_down_needed`, the loop's wind-down and fan-out
gates) and writes its final answer from what it has. Locally that is a
PreToolUse hook: once the message is at that point every further tool call is
refused with the reason, and the model answers — a final answer needs no tool,
so it is never blocked, exactly like Soleon's tool-free wind-down exemption.

Where the numbers come from (each verified against Claude Code 2.1.x):
  * the agent — its own transcript, `<session transcript minus .jsonl>/
    subagents/agent-<agent_id>.jsonl`; a model call's `usage` is written before
    its tool call's PreToolUse fires, once per content block, so calls are
    de-duplicated by message id;
  * a helper while it runs — the runner reads its `stream-json` output and
    books each model call as it arrives (a headless run writes nothing to its
    transcript until it ends, so the helper's own hook cannot read it); the
    helper's hook then decides from the ledger. The stream line and the hook
    fire within milliseconds, so the model call that crosses the line may still
    run its tool — as on Soleon, which checks before each MODEL call;
  * a helper once it finished — `modelUsage` of its result (the `usage` block
    covers only its LAST model call).

The ledger (`<agent dir>/.local-budget.json`) holds one message's state:
`{turn, agentTranscript, helpers: {run: tokens}}`.

Not mirrored: Soleon can lower the wind-down point for a simple request (its
per-turn "soft budget" comes from a complexity estimate this run cannot
reproduce). Locally the wind-down sits at the full budget's 85%, the most any
Soleon turn gets — so a local run never refuses what Soleon would allow.

Hooks:
    python3 soleon_message_budget.py agent-pretool          # plugin PreToolUse
    python3 soleon_message_budget.py helper-pretool --agent-dir D --turn T --run R
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

#: shared/budget_contract.py
DEFAULT_LIMIT = 500_000
#: shared/token_budget.py — the loop stops using tools at this share.
WIND_DOWN_FRACTION = 0.85
LEDGER_NAME = ".local-budget.json"
TURN_FILE_NAME = ".local-turn.json"


# ---------------------------------------------------------------------------
# the budget
# ---------------------------------------------------------------------------

def message_budget(config: Any) -> int:
    """The effective Per Message Token Budget of a pulled `config.json`; 0 =
    switched off. Mirrors `budget_contract.budget_setting` for `tokenBudget`:
    no flag = on, a legacy 0 = the 500,000 default, an explicit
    `tokenBudgetEnabled: false` = off."""
    config = config if isinstance(config, dict) else {}
    loop = dict(config.get("loop")) if isinstance(config.get("loop"), dict) else {}
    for key in ("tokenBudget", "tokenBudgetEnabled"):
        if key not in loop and key in config:
            loop[key] = config[key]
    if loop.get("tokenBudgetEnabled") is False:
        return 0
    raw = loop.get("tokenBudget")
    value = int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0 else 0
    return value or DEFAULT_LIMIT


def budget_of(agent_dir: Path) -> int:
    try:
        return message_budget(json.loads((agent_dir / "config.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return DEFAULT_LIMIT


def wind_down_at(budget: int) -> int:
    return int(budget * WIND_DOWN_FRACTION)


# ---------------------------------------------------------------------------
# counting
# ---------------------------------------------------------------------------

def _usage_total(usage: Dict[str, Any]) -> int:
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
        v = usage.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total += int(v)
    return total


def transcript_tokens(path: Optional[str]) -> int:
    """Tokens of every model call a Claude Code transcript records. Claude
    Code writes one line per content block, each carrying the whole call's
    usage, so a call counts once (its last line wins)."""
    if not path or not os.path.isfile(path):
        return 0
    by_call: Dict[str, int] = {}
    anon = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            n = _usage_total(msg["usage"])
            if msg.get("id"):
                by_call[str(msg["id"])] = n
            else:
                anon += n
    return sum(by_call.values()) + anon


def result_tokens(result: Any) -> int:
    """Everything a finished `claude -p --output-format json` run spent:
    `modelUsage` sums every model call of the run."""
    usage = result.get("modelUsage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        return _usage_total(result.get("usage") or {}) if isinstance(result, dict) else 0
    total = 0
    for per_model in usage.values():
        if not isinstance(per_model, dict):
            continue
        for key in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens"):
            v = per_model.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total += int(v)
    return total


def subagent_transcript(session_transcript: Optional[str], agent_id: Optional[str]) -> Optional[str]:
    if not session_transcript or not agent_id or not session_transcript.endswith(".jsonl"):
        return None
    return os.path.join(session_transcript[:-len(".jsonl")], "subagents", "agent-{}.jsonl".format(agent_id))


def stream_call_tokens(event: Any) -> Optional[Tuple[str, int]]:
    """(message id, tokens) of an `assistant` event of `--output-format
    stream-json`, else None."""
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return None
    msg = event.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict) or not msg.get("id"):
        return None
    return str(msg["id"]), _usage_total(msg["usage"])


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------

class Ledger:
    """One message's spend, shared by the agent's hook, the runner and each
    helper's hook — separate processes, so every change is read-modify-write
    under an exclusive lock."""

    def __init__(self, agent_dir: Path):
        self.path = Path(agent_dir) / LEDGER_NAME

    @contextlib.contextmanager
    def _locked(self, turn: str):
        with open(str(self.path) + ".lock", "a+") as lock:
            try:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            except ImportError:  # Windows: best effort, same-process safety only
                pass
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            if not isinstance(state, dict) or state.get("turn") != turn:
                state = {"turn": turn, "agentTranscript": None, "helpers": {}}
            yield state
            tmp = str(self.path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, self.path)

    def note_agent(self, turn: str, transcript: Optional[str]) -> Dict[str, Any]:
        with self._locked(turn) as state:
            if transcript:
                state["agentTranscript"] = transcript
            return dict(state)

    def note_helper(self, turn: str, run: str, tokens: int) -> Dict[str, Any]:
        with self._locked(turn) as state:
            state["helpers"][run] = max(int(tokens), 0)
            return dict(state)

    def read(self, turn: str) -> Dict[str, Any]:
        with self._locked(turn) as state:
            return dict(state)


def spent(state: Dict[str, Any], agent_tokens: Optional[int] = None) -> int:
    agent = transcript_tokens(state.get("agentTranscript")) if agent_tokens is None else agent_tokens
    return agent + sum(int(v) for v in (state.get("helpers") or {}).values())


def refusal(used: int, budget: int, *, helper: bool) -> str:
    who = "this helper" if helper else "the agent"
    return (
        "per-message token budget exhausted: used={used} budget={budget} — this message has used "
        "{used:,} of its {budget:,}-token Per Message Token Budget (the agent and every helper share it), "
        "and Soleon stops tool use at {pct}% of it. Do not call any more tools: {who} must answer now "
        "from what it has already gathered, and say plainly what it could not get to."
    ).format(used=used, budget=budget, pct=int(WIND_DOWN_FRACTION * 100), who=who)


def _deny(reason: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------

def _pulled_agent_dir(hook: Dict[str, Any]) -> Optional[Path]:
    agent_type = hook.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type or "/" in agent_type or agent_type.startswith("."):
        return None
    candidate = Path(hook.get("cwd") or os.getcwd()) / ".soleon" / "agents" / agent_type
    return candidate if (candidate / "pull.json").is_file() else None


def _turn_of(agent_dir: Path, agent_id: str) -> str:
    """This run's turn id — the SubagentStart hook's, when the file is this
    run's; else a key of the run itself (its helpers then cannot be joined,
    and only the agent's own spend counts)."""
    try:
        data = json.loads((agent_dir / TURN_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("agentId") == agent_id and isinstance(data.get("turn"), str):
        return data["turn"]
    return "agent:" + agent_id


def agent_pretool(hook: Dict[str, Any]) -> int:
    """PreToolUse for the whole session: acts only inside a pulled agent's
    subagent run (every other agent, and the main conversation, pass)."""
    agent_id = hook.get("agent_id")
    agent_dir = _pulled_agent_dir(hook) if agent_id else None
    if agent_dir is None:
        return 0
    budget = budget_of(agent_dir)
    if budget <= 0:
        return 0
    transcript = subagent_transcript(hook.get("transcript_path"), str(agent_id))
    state = Ledger(agent_dir).note_agent(_turn_of(agent_dir, str(agent_id)), transcript)
    used = spent(state)
    if used >= wind_down_at(budget):
        _deny(refusal(used, budget, helper=False))
    return 0


def helper_pretool(hook: Dict[str, Any], agent_dir: Path, turn: str, run: str) -> int:
    """PreToolUse inside a helper the runner started: the message's ledger —
    which the runner keeps current with this helper's calls — decides."""
    budget = budget_of(agent_dir)
    if budget <= 0:
        return 0
    used = spent(Ledger(agent_dir).read(turn))
    if used >= wind_down_at(budget):
        _deny(refusal(used, budget, helper=True))
    return 0


def helper_hook_settings(agent_dir: str, turn: str, run: str) -> Dict[str, Any]:
    """`--settings` for a helper run: this module as its PreToolUse hook."""
    cmd = " ".join(_quote(p) for p in [sys.executable, os.path.abspath(__file__), "helper-pretool",
                                          "--agent-dir", agent_dir, "--turn", turn, "--run", run])
    return {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]}}


def _quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _read_hook(stream=None) -> Dict[str, Any]:
    raw = (stream or sys.stdin).read()
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def main(argv: Optional[Iterable[str]] = None, stream=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=("agent-pretool", "helper-pretool"))
    ap.add_argument("--agent-dir")
    ap.add_argument("--turn")
    ap.add_argument("--run")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    hook = _read_hook(stream)
    try:
        if args.mode == "agent-pretool":
            return agent_pretool(hook)
        if not (args.agent_dir and args.turn and args.run):
            sys.stderr.write("helper-pretool needs --agent-dir, --turn and --run\n")
            return 0
        return helper_pretool(hook, Path(args.agent_dir), args.turn, args.run)
    except OSError as exc:
        # A ledger the hook cannot read or write must not wedge the run; say
        # so where the person sees it instead of refusing every tool.
        sys.stdout.write(json.dumps({"systemMessage": "Soleon: the local token budget could not be checked — {}".format(exc)}) + "\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
