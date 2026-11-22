#!/usr/bin/env python3
"""The agent's Per Message Token Budget, enforced on a LOCAL run the way
Soleon enforces it: one allowance for the whole message — the agent's own
model calls AND every helper (integration worker) it starts.

Soleon counts every input token (cached reads and writes included) plus the
output of every model call in the turn (`proxy._accumulate_dispatch_tokens`).
The agent stops using tools once the turn has spent WIND_DOWN_FRACTION of the
budget (`token_budget.wind_down_needed`, the loop's wind-down and fan-out
gates) and writes its final answer from what it has. Locally that is a
PreToolUse hook: once the message is at that point the next tool call goes to
the PERSON as Claude Code's approval prompt (Dan 2026-11-22: a local run spends
no Soleon tokens, so a person may waive the budget). Approving waives it for
the rest of the message, helpers included (`agent_posttool` sees the asked
call run); refusing — or a run with no one to ask — refuses the call with the
reason, and the model answers. A final answer needs no tool, so it is never
blocked, exactly like Soleon's tool-free wind-down exemption.

Where the numbers come from (each verified against Claude Code 2.1.x):
  * the agent and each configured subagent run for it — its own transcript, `<session transcript minus .jsonl>/
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

The ledger (`<agent dir>/.local-budget.json`) holds each live message's state
— see `Ledger` for what a message covers.

Not mirrored: Soleon can lower the wind-down point for a simple request (its
per-turn "soft budget" comes from a complexity estimate this run cannot
reproduce). Locally the wind-down sits at the full budget's 85%, the most any
Soleon turn gets — so a local run never refuses what Soleon would allow.

Hooks:
    python3 soleon_message_budget.py agent-pretool          # plugin PreToolUse
    python3 soleon_message_budget.py helper-pretool --agent-dir D --message M --run R
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

#: shared/budget_contract.py
DEFAULT_LIMIT = 500_000
#: shared/token_budget.py — the loop stops using tools at this share.
WIND_DOWN_FRACTION = 0.85
LEDGER_NAME = ".local-budget.json"
TURN_FILE_NAME = ".local-turn.json"
#: soleon_agent_tools_mcp.EXTRACTION_FILE — present while the agent runs a
#: knowledge-base extraction job (`soleon_kb_local_ingest.py prepare-extractor`).
EXTRACTION_FILE = ".local-extraction.json"


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


#: Written by soleon_kb_local_ingest.prepare_extractor while the pulled
#: kb-extractor runs an extraction job (its ``tokenBudget`` is the job's).
EXTRACTION_FILE = ".local-extraction.json"


def budget_of(agent_dir: Path) -> int:
    """The budget the agent works to now: an extraction job's own budget while
    it runs one (the platform enforces the source's ``tokenBudgetMax`` on an
    extraction, never the agent's per-message setting), else its Per Message
    Token Budget."""
    try:
        job = json.loads((agent_dir / EXTRACTION_FILE).read_text(encoding="utf-8"))
        raw = job.get("tokenBudget") if isinstance(job, dict) else None
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            return raw
    except (OSError, ValueError):
        pass
    try:
        return message_budget(json.loads((agent_dir / "config.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return DEFAULT_LIMIT


def wind_down_at(budget: int) -> int:
    return int(budget * WIND_DOWN_FRACTION)


# ---------------------------------------------------------------------------
# counting
# ---------------------------------------------------------------------------

#: Soleon's cache weighting (agent-infra `shared.daily_budget.cache_weighted_input`):
#: the budget counts a cache read at 10% of an input token and a cache write at
#: 125%, the same figure as its daily ledger and Traces. Counting cached tokens
#: in full made a local run wind down after ~9 calls that Soleon would allow.
CACHE_READ_DISCOUNT = 0.9
CACHE_WRITE_PREMIUM = 0.25


def _num(usage: Dict[str, Any], key: str) -> int:
    v = usage.get(key)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _weighted(fresh: int, cache_read: int, cache_write: int, output: int) -> int:
    """Budget tokens of one call. `fresh` excludes the cached tokens (Anthropic's
    `input_tokens`); Soleon's input INCLUDES them, then discounts reads."""
    total_input = fresh + cache_read + cache_write
    weighted = total_input - CACHE_READ_DISCOUNT * cache_read + CACHE_WRITE_PREMIUM * cache_write
    return max(0, int(weighted + 0.5)) + output


def _usage_total(usage: Dict[str, Any]) -> int:
    return _weighted(_num(usage, "input_tokens"), _num(usage, "cache_read_input_tokens"),
                     _num(usage, "cache_creation_input_tokens"), _num(usage, "output_tokens"))


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
        total += _weighted(_num(per_model, "inputTokens"), _num(per_model, "cacheReadInputTokens"),
                           _num(per_model, "cacheCreationInputTokens"), _num(per_model, "outputTokens"))
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

#: A helper started this soon after a pulled agent's tool call belongs to that
#: call's message: the wrapper call's own PreToolUse touches the ledger
#: milliseconds before the call reaches the tools server that starts it.
JOIN_WINDOW_S = 30.0


#: A message nothing has touched for this long is dropped from the ledger.
KEEP_S = 24 * 3600.0
#: How long a hook waits for the ledger's lock, or for a file Windows holds
#: open in another process, before it gives up (the plugin's hook timeout is
#: longer).
LOCK_WAIT_S = 5.0
_RETRY_S = 0.02


@contextlib.contextmanager
def _file_lock(path: str):
    """An exclusive lock across processes. Every tool call of a run starts its
    own hook process, and Claude Code runs a turn's parallel calls at once: on
    Windows an unlocked read-modify-write lost updates and collided in
    `os.replace` (2026-11-22, `[WinError 32]` on most calls of one extraction
    job, so the budget went unchecked)."""
    with open(path, "a+") as fh:
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(_RETRY_S)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _retrying(fn):
    """`fn()`, retried while Windows refuses a file another process holds open
    (a reader outside the lock while a writer replaces the file)."""
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            return fn()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_RETRY_S)


def _fresh(message: str) -> Dict[str, Any]:
    return {"message": message, "runs": {}, "helpers": {}, "touchedAt": None,
            "asked": [], "waived": False}


class Ledger:
    """Each message's spend, shared by the agents' hooks, the runner and each
    helper's hook — separate processes, so every change is read-modify-write
    under an exclusive lock (`_file_lock`).

    A MESSAGE is one run of the pulled agent for one prompt of the person
    (resumes included), every configured subagent the conversation runs for
    it (`<slug>--<id>`, workflows' members included), and every integration
    helper any of them starts. Claude Code stamps one `prompt_id` on every
    tool call of a prompt, subagents' included; the key is that id plus the
    agent run's id (`_message_of`).

    The file holds every live message (`{messages: {key: state}}`), so two runs
    of one agent at once — an eval suite, a refresh's two extraction jobs —
    never reset each other. State: `{message, runs: {agent_id: transcript},
    helpers: {run: tokens}, touchedAt, seenAt, asked, waived}`."""

    def __init__(self, agent_dir: Path, now=None):
        self.path = Path(agent_dir) / LEDGER_NAME
        self._now = now or time.time

    def _read(self) -> Any:
        try:
            return json.loads(_retrying(lambda: self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return {}
        except ValueError:
            return {}

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """Every message's state, by key. A file of 1.0.34 or earlier held one
        message's state at its top level; it reads as that one message."""
        raw = self._read()
        if not isinstance(raw, dict):
            return {}
        messages = raw.get("messages")
        if isinstance(messages, dict):
            return {k: v for k, v in messages.items() if isinstance(k, str) and isinstance(v, dict)}
        if isinstance(raw.get("message"), str):
            return {raw["message"]: raw}
        return {}

    def _peek(self) -> Dict[str, Dict[str, Any]]:
        """`_load` for a reader outside the lock: a ledger it cannot read
        right now reads as empty."""
        try:
            return self._load()
        except OSError:
            return {}

    def state(self, message: str) -> Dict[str, Any]:
        """One message's state (a fresh one when the ledger has none)."""
        return dict(self._peek().get(message) or _fresh(message))

    def _write(self, messages: Dict[str, Dict[str, Any]]) -> None:
        tmp = "{}.{}.tmp".format(self.path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"messages": messages}, fh)
        _retrying(lambda: os.replace(tmp, self.path))

    @contextlib.contextmanager
    def _locked(self, message: str, touch: bool = False, create: bool = True):
        with _file_lock(str(self.path) + ".lock"):
            messages = self._load()
            state = messages.get(message)
            if state is None:
                if not create:  # no such message: nothing to change
                    yield None
                    return
                state = _fresh(message)
            now = self._now()
            if touch:
                state["touchedAt"] = now
            state["seenAt"] = now
            yield state
            messages[message] = state
            self._write({k: v for k, v in messages.items()
                         if k == message or now - _seen(v, now) <= KEEP_S})

    def note_run(self, message: str, agent_id: str, transcript: Optional[str]) -> Dict[str, Any]:
        with self._locked(message, touch=True) as state:
            # The run is recorded even without a transcript: `_message_of`
            # finds the message a run belongs to by its id.
            state["runs"][agent_id] = transcript or state["runs"].get(agent_id)
            return dict(state)

    def note_helper(self, message: str, run: str, tokens: int) -> Dict[str, Any]:
        with self._locked(message) as state:
            state["helpers"][run] = max(int(tokens), 0)
            return dict(state)

    def read(self, message: str) -> Dict[str, Any]:
        with self._locked(message) as state:
            return dict(state)

    def note_ask(self, message: str, tool_use_id: str) -> None:
        """The hook asked the person to waive the budget on this tool call."""
        with self._locked(message) as state:
            asked = [a for a in (state.get("asked") or []) if isinstance(a, str)]
            if tool_use_id not in asked:
                asked.append(tool_use_id)
            state["asked"] = asked

    def confirm_ask(self, message: str, tool_use_id: str) -> bool:
        """The asked call RAN, so the person approved it: the budget is waived
        for the rest of this message. A call the hook never asked about (or one
        of another message) waives nothing."""
        with self._locked(message, create=False) as state:
            if state is None:
                return False
            if tool_use_id in (state.get("asked") or []):
                state["waived"] = True
            return bool(state.get("waived"))

    def message_for_helper(self, fallback: str) -> str:
        """The message a helper starting NOW belongs to: the one a pulled
        agent's tool call just touched, else `fallback` (hooks not installed —
        the helper then only answers to its own spend)."""
        best: Optional[Tuple[str, float]] = None
        for key, state in self._peek().items():
            touched = state.get("touchedAt")
            if isinstance(touched, (int, float)) and self._now() - touched <= JOIN_WINDOW_S \
                    and (best is None or touched > best[1]):
                best = (key, touched)
        return best[0] if best else fallback

    def message_of_run(self, agent_id: str) -> Optional[str]:
        """The message an agent run already belongs to, if any (the most
        recently touched one, should several name it)."""
        best: Optional[Tuple[str, float]] = None
        for key, state in self._peek().items():
            if agent_id in (state.get("runs") or {}):
                touched = state.get("touchedAt")
                touched = touched if isinstance(touched, (int, float)) else 0.0
                if best is None or touched > best[1]:
                    best = (key, touched)
        return best[0] if best else None

    def latest_with_prefix(self, prefix: str) -> Optional[str]:
        best: Optional[Tuple[str, float]] = None
        for key, state in self._peek().items():
            if key.startswith(prefix):
                touched = state.get("touchedAt")
                touched = touched if isinstance(touched, (int, float)) else 0.0
                if best is None or touched > best[1]:
                    best = (key, touched)
        return best[0] if best else None


def _seen(state: Dict[str, Any], default: float) -> float:
    for key in ("seenAt", "touchedAt"):
        value = state.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return default


def approved_in_transcripts(paths: Iterable[Optional[str]], asked: Iterable[str]) -> Optional[str]:
    """The first asked call whose result a transcript holds as a SUCCESS, else
    None. A call the person refused comes back `is_error: true` with the
    refusal; one they approved ran and has its real result. A tool that failed
    after an approval also reads as an error, so it waives nothing and the next
    call asks again (never the other way round).

    This is the approval's second witness, beside the PostToolUse hook: Claude
    Code reads a plugin's hook LIST only when a conversation starts, so a
    conversation that began before the hook was added never runs it."""
    wanted = {a for a in asked if isinstance(a, str) and a}
    if not wanted:
        return None
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if '"tool_result"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message") if isinstance(rec, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_result" \
                            and block.get("tool_use_id") in wanted and block.get("is_error") is not True:
                        return block["tool_use_id"]
    return None


def spent(state: Dict[str, Any]) -> int:
    runs = sum(transcript_tokens(p) for p in (state.get("runs") or {}).values())
    return runs + sum(int(v) for v in (state.get("helpers") or {}).values())


def refusal(used: int, budget: int, *, who: str, helper: bool = False) -> str:
    text = (
        "per-message token budget exhausted: used={used} budget={budget} — this message has used "
        "{used:,} of its {budget:,}-token Per Message Token Budget (the agent, its subagents and every "
        "helper share it), and Soleon stops tool use at {pct}% of it. Do not call any more tools: {who} "
        "must answer now from what it has already gathered, and say plainly what it could not get to."
    ).format(used=used, budget=budget, pct=int(WIND_DOWN_FRACTION * 100), who=who)
    if helper:
        # A helper runs headless and cannot ask the person; the agent's next
        # tool call does, and an approval lets the agent hand the rest out again.
        text += (" Say that the local token budget stopped you and list what is left: the person can "
                 "waive the budget when the agent's next tool call asks, and the agent can then hand "
                 "the rest out again.")
    return text


def waiver_request(used: int, budget: int, *, extraction: bool, who: str) -> str:
    """The reason on the person's approval prompt. In a run nobody can answer
    (`claude -p`) Claude Code denies the call and the model reads this text
    instead, so it also says what to do when refused."""
    what, scope = (("extraction job's token budget", "extraction job") if extraction
                   else ("Per Message Token Budget", "message"))
    return (
        "Soleon token budget: this {scope} has used {used:,} of its {budget:,}-token {what} "
        "(used={used} budget={budget}; the agent, its subagents and every helper share it), and "
        "Soleon stops tool use at {pct}% of it. This run is local, so you may waive the budget: "
        "approve this call and the budget is off for the rest of this {scope}, and any task a helper "
        "left unfinished because of the budget can be handed out again. If this call is "
        "refused, do not call any more tools: {who} must answer now from what it has already "
        "gathered, and say plainly what it could not get to."
    ).format(used=used, budget=budget, what=what, scope=scope,
             pct=int(WIND_DOWN_FRACTION * 100), who=who)


def _decide(decision: str, reason: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}) + "\n")
    sys.stdout.flush()


def _deny(reason: str) -> None:
    _decide("deny", reason)


def _ask(reason: str) -> None:
    """Claude Code's own approval prompt: only the person can answer it. Tested
    on Claude Code 2.1.328 (2026-11-22): a hook's "ask" is never approved by
    bypassPermissions, an allow rule or --allowedTools, and a `-p` run with no
    one to answer denies it."""
    _decide("ask", reason)


#: Tools an extraction job saves its work with: never held back by the budget,
#: or a refused waiver would leave the job with nothing saved.
EXTRACTION_SAVE_TOOLS = ("emit_document",)


def _is_save_tool(tool_name: Any) -> bool:
    name = tool_name if isinstance(tool_name, str) else ""
    return any(name == t or name.endswith("__" + t) for t in EXTRACTION_SAVE_TOOLS)


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------

def _pulled_agent_of(hook: Dict[str, Any]) -> Optional[Tuple[Path, bool]]:
    """(the pulled agent's dir, whether this run is one of its configured
    subagents) — `<slug>` is the agent itself, `<slug>--<id>` a configured
    subagent (pull_agent.py names them so); None for anything else."""
    agent_type = hook.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type or "/" in agent_type or agent_type.startswith("."):
        return None
    slug, sep, _ = agent_type.partition("--")
    candidate = Path(hook.get("cwd") or os.getcwd()) / ".soleon" / "agents" / slug
    if not slug or not (candidate / "pull.json").is_file():
        return None
    return candidate, bool(sep)


def _message_of(hook: Dict[str, Any], agent_dir: Path, agent_id: str, is_subagent: bool = False) -> str:
    """The message this run belongs to. Claude Code stamps `prompt_id` on
    every tool call; without it (an older build), the agent run's own turn —
    its configured subagents then cannot be joined.

    Each run of the agent ITSELF is its own message (`prompt:<id>:<run>`), as
    each message to it is on Soleon: several runs for one prompt — an eval
    suite — do not share one allowance (2026-11-17: four evals run for one
    prompt were each refused after ~98k tokens, together past the 85% mark).
    A resumed run keeps its agent id, so it stays in its message. A configured
    subagent (`<slug>--<id>`) is run by the conversation FOR the agent, so it
    joins the agent run of the same prompt the ledger holds.

    A run stays in the message it started in. Claude Code stamps a call with
    the conversation's CURRENT prompt id, so a run that outlives its prompt —
    one in the background while the person writes again, or one whose
    approval prompt was answered later — would otherwise start a new message
    at zero and lose its approval (2026-11-22: an extraction job's approved
    call came back under a new prompt id; the waiver was never recorded)."""
    prompt_id = hook.get("prompt_id")
    if isinstance(prompt_id, str) and prompt_id:
        ledger = Ledger(agent_dir)
        known = ledger.message_of_run(agent_id)
        if known:
            return known
        prompt = "prompt:" + prompt_id
        if not is_subagent:
            return prompt + ":" + agent_id
        return ledger.latest_with_prefix(prompt + ":") or prompt
    try:
        data = json.loads((agent_dir / TURN_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("agentId") == agent_id and isinstance(data.get("turn"), str):
        return "turn:" + data["turn"]
    return "agent:" + agent_id


def agent_pretool(hook: Dict[str, Any]) -> int:
    """PreToolUse for the whole session: acts only inside a run of a pulled
    agent or one of its configured subagents (every other agent, and the main
    conversation, pass)."""
    agent_id = hook.get("agent_id")
    found = _pulled_agent_of(hook) if agent_id else None
    if found is None:
        return 0
    agent_dir, is_subagent = found
    budget = budget_of(agent_dir)
    if budget <= 0:
        return 0
    # A knowledge-base extraction job works to the job's own budget
    # (`budget_of`), shared by the agent and every helper it starts, as on
    # Soleon. Its save tool always passes: holding it back saves nothing.
    extraction = (agent_dir / EXTRACTION_FILE).exists()
    ledger = Ledger(agent_dir)
    message = _message_of(hook, agent_dir, str(agent_id), is_subagent)
    transcript = subagent_transcript(hook.get("transcript_path"), str(agent_id))
    state = ledger.note_run(message, str(agent_id), transcript)
    if state.get("waived") or (extraction and _is_save_tool(hook.get("tool_name"))):
        return 0
    approved = approved_in_transcripts((state.get("runs") or {}).values(), state.get("asked") or [])
    if approved and ledger.confirm_ask(message, approved):
        return 0
    used = spent(state)
    if used < wind_down_at(budget):
        return 0
    who = "this subagent" if is_subagent else "the agent"
    tool_use_id = hook.get("tool_use_id")
    if isinstance(tool_use_id, str) and tool_use_id and hook.get("permission_mode") != "dontAsk":
        # The person decides: approving waives the budget for the rest of the
        # message (`agent_posttool` sees the call run). dontAsk would deny
        # an ask without showing the reason, so it gets the plain refusal.
        ledger.note_ask(message, tool_use_id)
        _ask(waiver_request(used, budget, extraction=extraction, who=who))
    else:
        _deny(refusal(used, budget, who=who))
    return 0


def agent_posttool(hook: Dict[str, Any]) -> int:
    """PostToolUse for the whole session: a call the budget hook asked about
    ran, so the person approved it, and the budget is waived for the rest of
    that message (its helpers included)."""
    agent_id = hook.get("agent_id")
    tool_use_id = hook.get("tool_use_id")
    found = _pulled_agent_of(hook) if agent_id else None
    if found is None or not isinstance(tool_use_id, str) or not tool_use_id:
        return 0
    agent_dir, is_subagent = found
    ledger = Ledger(agent_dir)
    message = _message_of(hook, agent_dir, str(agent_id), is_subagent)
    if ledger.confirm_ask(message, tool_use_id):
        sys.stdout.write(json.dumps({
            "systemMessage": "Soleon: you approved going past the local token budget, so it is off for "
                             "the rest of this run.",
            # The model's copy: without it the agent still believes the budget
            # is spent and stops researching (2026-11-22).
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "The person approved going past the local token budget: it is off "
                                     "for the rest of this run, for you and every helper. Carry on with "
                                     "the work, and hand out again any task a helper left unfinished "
                                     "because of the budget.",
            },
        }) + "\n")
    return 0


def helper_pretool(hook: Dict[str, Any], agent_dir: Path, message: str, run: str) -> int:
    """PreToolUse inside a helper the runner started: the message's ledger —
    which the runner keeps current with this helper's calls — decides. A
    helper runs headless, so it cannot ask; it follows a waiver the person
    gave on the agent's own call."""
    budget = budget_of(agent_dir)
    if budget <= 0:
        return 0
    state = Ledger(agent_dir).read(message)
    if state.get("waived"):
        return 0
    used = spent(state)
    if used >= wind_down_at(budget):
        _deny(refusal(used, budget, who="this helper", helper=True))
    return 0


def helper_hook_settings(agent_dir: str, message: str, run: str) -> Dict[str, Any]:
    """`--settings` for a helper run: this module as its PreToolUse hook."""
    cmd = " ".join(_quote(p) for p in [sys.executable, os.path.abspath(__file__), "helper-pretool",
                                          "--agent-dir", agent_dir, "--message", message, "--run", run])
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
    ap.add_argument("mode", choices=("agent-pretool", "agent-posttool", "helper-pretool"))
    ap.add_argument("--agent-dir")
    ap.add_argument("--message")
    ap.add_argument("--run")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    hook = _read_hook(stream)
    try:
        if args.mode == "agent-pretool":
            return agent_pretool(hook)
        if args.mode == "agent-posttool":
            return agent_posttool(hook)
        if not (args.agent_dir and args.message and args.run):
            sys.stderr.write("helper-pretool needs --agent-dir, --message and --run\n")
            return 0
        return helper_pretool(hook, Path(args.agent_dir), args.message, args.run)
    except OSError as exc:
        # A ledger the hook cannot read or write must not wedge the run; say
        # so where the person sees it instead of refusing every tool.
        sys.stdout.write(json.dumps({"systemMessage": "Soleon: the local token budget could not be checked — {}".format(exc)}) + "\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
