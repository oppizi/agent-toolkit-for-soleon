"""AHP-1477 WP-H T3 (spec §9 G7 / AC-26) — the `soleon` plugin asks before
every production-tier tool, and before no other tool.

Every plugin token holds every scope (one plugin, G1 withdrawn), so the token
no longer keeps a session out of production. A plugin cannot ship permission
rules, but it can ship hooks: a `PreToolUse` hook answers `ask` for every
`mcp__plugin_soleon_soleon__*_production` / `*_everywhere` tool and the exact
`call_approved_agent_tool` (G4), with a reason naming the tool and the off
switch.

VERIFIED (2026-11-16, Claude Code 2.1.325): in `claude -p` with
`--allowedTools` allowing the tool, a `PreToolUse` `ask` does NOT run it — the
call lands in `permission_denials` and the model is handed the hook's reason
verbatim. So `ask` is the right answer in `-p` too; no `deny` fallback is
needed.

The gate is two layers, and each is pinned here so widening either goes red:
1. the matcher in hooks.json (Claude Code tests it as a regex on the tool name);
2. the script re-checks the name itself, and asks when it cannot tell.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_risk_split_names import DEVELOPMENT, NEW, NOTE, PREFIX, PRODUCTION

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "soleon"
HOOKS = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
SCRIPT = "hooks/soleon_ask_before_production.sh"
OFF_VAR = "SOLEON_ASK_BEFORE_PRODUCTION"
BASH = shutil.which("bash")

# Tools that must never be asked about: every development-tier name, the cost
# split, the kept unapproved call, everyday reads/writes, another server's or
# another plugin's tool with a production-looking name, and near-misses.
NOT_GATED = sorted(set(DEVELOPMENT) | {"run_eval_checks", "run_eval_suite", "call_agent_tool",
                                        "get_agent_draft", "list_agents", "patch_agent_draft",
                                        "deploy_agent_draft", "set_ticket_stage"})
FOREIGN = [
    "Bash", "Edit", "Read",
    "promote_agent_to_production",                                  # not an MCP tool
    "mcp__plugin_other_soleon__promote_agent_to_production",        # another plugin
    "mcp__plugin_soleon_other__promote_agent_to_production",        # another server
    "mcp__soleon__promote_agent_to_production",                     # a user-added server
    PREFIX + "promote_agent_to_production_notes",                   # suffix not at the end
    PREFIX + "call_approved_agent_tool_history",
    "x" + PREFIX + "call_approved_agent_tool",
]


def _entry():
    entries = [e for e in HOOKS["PreToolUse"] if any(SCRIPT in h["command"] for h in e["hooks"])]
    assert len(entries) == 1, "exactly one PreToolUse entry runs {}".format(SCRIPT)
    return entries[0]


def _fires(matcher: str, tool: str) -> bool:
    # Claude Code tests a matcher as a regex against the tool name (search, not
    # full match), so the matcher has to carry its own anchors.
    return re.search(matcher, tool) is not None


def test_matcher_fires_for_every_production_tool():
    matcher = _entry()["matcher"]
    assert len(PRODUCTION) == 16  # 15 _production/_everywhere names + call_approved_agent_tool
    missed = [t for t in PRODUCTION if not _fires(matcher, PREFIX + t)]
    assert not missed, missed


@pytest.mark.parametrize("tool", [PREFIX + t for t in NOT_GATED] + FOREIGN)
def test_matcher_fires_for_no_other_tool(tool):
    assert not _fires(_entry()["matcher"], tool)


def test_matcher_negative_control():
    """The checks above can go red: a widened matcher fails them."""
    widened = PREFIX + ".*"
    assert any(_fires(widened, PREFIX + t) for t in NOT_GATED)
    unanchored = PREFIX + ".*_production"
    assert _fires(unanchored, PREFIX + "promote_agent_to_production_notes")


def _run(payload, env_extra=None):
    command = _entry()["hooks"][0]["command"]
    env = {k: v for k, v in os.environ.items() if k != OFF_VAR}
    env.update({"CLAUDE_PLUGIN_ROOT": str(PLUGIN).replace("\\", "/")}, **(env_extra or {}))
    data = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run([BASH, "-c", command], input=data, capture_output=True, text=True,
                          timeout=30, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _decision(out: str):
    if not out.strip():
        return None, ""
    spec = json.loads(out)["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    return spec["permissionDecision"], spec["permissionDecisionReason"]


def _call(tool):
    return {"hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {"slug": "demo-agent", "app_env": "prod"}}


needs_bash = pytest.mark.skipif(BASH is None, reason="bash runs the plugin's hook commands")


@needs_bash
@pytest.mark.parametrize("tool", PRODUCTION)
def test_script_asks_naming_the_tool_and_the_off_switch(tool):
    decision, reason = _decision(_run(_call(PREFIX + tool)))
    assert decision == "ask"
    assert tool in reason
    assert "{}=off".format(OFF_VAR) in reason


@needs_bash
@pytest.mark.parametrize("tool", [PREFIX + t for t in NOT_GATED] + FOREIGN)
def test_script_stays_silent_for_any_other_tool(tool):
    """Defence in depth: even if the matcher were widened, the script answers
    nothing for a tool outside the production set."""
    assert _decision(_run(_call(tool))) == (None, "")


@needs_bash
@pytest.mark.parametrize("payload", ["", "not json", '{"tool_input": {}}', '{"tool_name": 7}',
                                     '{"tool_name": "' + PREFIX + 'x\\"_production"}'])
def test_script_asks_when_it_cannot_tell(payload):
    """Fail closed: the matcher already selected a production tool, so a
    payload the script cannot read is asked about, never waved through."""
    decision, reason = _decision(_run(payload))
    assert decision == "ask"
    assert "{}=off".format(OFF_VAR) in reason


@needs_bash
def test_off_switch_turns_it_off_only_on_the_exact_value():
    tool = PREFIX + "promote_agent_to_production"
    assert _decision(_run(_call(tool), {OFF_VAR: "off"})) == (None, "")
    for value in ("", "0", "false", "OFF", " off", "no"):
        assert _decision(_run(_call(tool), {OFF_VAR: value}))[0] == "ask", value


def test_every_gated_name_is_a_spec_name():
    """The gated set is exactly the §4 production tier: nothing invented."""
    assert set(PRODUCTION) <= set(NEW)


def test_release_note_documents_the_hook_and_its_off_switch():
    text = NOTE.read_text(encoding="utf-8")
    assert "## Ask before production (plugin hook)" in text
    section = text.split("## Ask before production (plugin hook)", 1)[1].split("\n## ", 1)[0]
    assert "{}=off".format(OFF_VAR) in section
    assert "claude -p" in section
    assert "call_approved_agent_tool" in section
