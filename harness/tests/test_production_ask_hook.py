"""AHP-1477 WP-H T3 (spec §9 G7 / AC-26) — the `soleon` plugin asks before
every production-tier tool, and before no other tool.

Every plugin token holds every scope (one plugin, G1 withdrawn), so the token
no longer keeps a session out of production. A plugin cannot ship permission
rules, but it can ship hooks: a `PreToolUse` hook answers `ask` for every
`mcp__plugin_soleon_soleon__*_production` / `*_everywhere` tool and the exact
`call_approved_agent_tool` (G4), with a reason naming the tool and the off
switch.

VERIFIED (2026-11-16, Claude Code 2.1.325, live `claude -p` runs against a
stub server under the plugin's matcher): in `claude -p` with `--allowedTools`
allowing the tool, a `PreToolUse` `ask` does NOT run it — the call lands in
`permission_denials` and the model is handed the hook's reason verbatim. The
hook input carries `"permission_mode"` first, then `"tool_name"`, then
`"tool_input"`, on one line; no mode flag and `--permission-mode manual` both
send `"default"`; and a `deny` is honoured (the tool never reached the stub,
one `permission_denials` entry for it) under `--permission-mode`
bypassPermissions, auto, dontAsk and acceptEdits. A project's
`.claude/settings.json` `env` entry for the off variable reaches the hook when
the user never set the option (the hook saw
`CLAUDE_PLUGIN_OPTION_ASK_BEFORE_PRODUCTION=off`).

So the script asks only where a person sees a question (default, acceptEdits,
plan) and denies in every other mode, an unreadable one and a missing one; it
ignores "off" when the project's settings name the option, and "off" never
lifts a deny.

The matcher in hooks.json is the ONLY gate (Claude Code tests it as a regex on
the tool name); it is pinned here so widening it goes red. The script answers
every call it is given, in POSIX shell with sed and tr only (no Python, nothing slow
enough to reach the hook timeout), and is run here under `sh` exactly as Claude
Code runs it, and under `dash` too when present, to catch bashisms.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from test_risk_split_names import DEVELOPMENT, NEW, NOTE, PREFIX, PRODUCTION

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "soleon"
HOOKS = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
SCRIPT = "hooks/soleon_ask_before_production.sh"
OPTION = "ask_before_production"
OFF_VAR = "CLAUDE_PLUGIN_OPTION_ASK_BEFORE_PRODUCTION"  # Claude Code: CLAUDE_PLUGIN_OPTION_<KEY upper>
OFF_HINT = "plugin option {} to off".format(OPTION)
SH = shutil.which("sh")
assert SH, "the plugin's hook commands run under `sh`; install a POSIX sh to run these tests"
SHELLS = [SH] + [d for d in [shutil.which("dash")] if d]

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


def _run(payload, env_extra=None, shell=None, path=None, project=None):
    command = _entry()["hooks"][0]["command"]
    env = {k: v for k, v in os.environ.items() if k not in (OFF_VAR, "SOLEON_ASK_BEFORE_PRODUCTION")}
    env.update({"CLAUDE_PLUGIN_ROOT": str(PLUGIN).replace("\\", "/"),
                "CLAUDE_PROJECT_DIR": str(project or tempfile.mkdtemp())}, **(env_extra or {}))
    if path is not None:
        env["PATH"] = path
    data = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run([shell or SH, "-c", command], input=data, capture_output=True,
                          text=True, timeout=30, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _decision(out: str):
    assert out.strip(), "the hook answered nothing"
    spec = json.loads(out)["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    return spec["permissionDecision"], spec["permissionDecisionReason"]


def _call(tool, mode="default", compact=False, tool_input=None):
    """Claude Code's key order (verified on 2.1.325): permission_mode, then
    tool_name, then tool_input."""
    call = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": tool_input or {"slug": "demo-agent", "app_env": "prod"}}
    if mode is not None:
        call = {"session_id": "s", "permission_mode": mode, **call}
    return json.dumps(call, separators=(",", ":")) if compact else call


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("tool", PRODUCTION)
def test_script_asks_naming_the_tool_and_the_off_switch(tool, shell):
    decision, reason = _decision(_run(_call(PREFIX + tool), shell=shell))
    assert decision == "ask"
    assert PREFIX + tool in reason
    assert OFF_HINT in reason


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk", "auto", "someFutureMode"])
def test_script_denies_where_nobody_sees_a_question(mode, compact):
    tool = PREFIX + "promote_agent_to_production"
    decision, reason = _decision(_run(_call(tool, mode, compact)))
    assert decision == "deny"
    assert tool in reason and mode in reason


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan"])
def test_script_asks_where_a_person_answers(mode):
    assert _decision(_run(_call(PREFIX + "patch_tool_policy_everywhere", mode)))[0] == "ask"


@pytest.mark.parametrize("spelling,expected", [
    ('"permission_mode" : "bypassPermissions"', "deny"),
    ('"permission_mode"\t:\t"bypassPermissions"', "deny"),
    ('"permission_mode":\n "dontAsk"', "deny"),
    ('"permission_mode" : "default"', "ask"),
    ('"permission_mode":\n\t"acceptEdits"', "ask"),
])
def test_mode_is_read_whatever_the_spacing(spelling, expected):
    tool = PREFIX + "promote_agent_to_production"
    payload = '{"session_id":"s",' + spelling + ',"tool_name":"' + tool + '","tool_input":{}}'
    assert _decision(_run(payload))[0] == expected


def test_a_missing_mode_is_denied():
    """An allowlist, not a denylist: no mode means nobody is known to answer."""
    decision, reason = _decision(_run(_call(PREFIX + "promote_agent_to_production", mode=None)))
    assert decision == "deny" and "unreadable permission mode" in reason


def test_tool_input_cannot_turn_a_deny_into_an_ask():
    """A model-written argument shaped like the mode key cannot downgrade the
    refusal: every mode in the input must be a person mode."""
    call = _call(PREFIX + "promote_agent_to_production", "bypassPermissions",
                 tool_input={"permission_mode": "default"})
    assert _decision(_run(call))[0] == "deny"
    stricter = _call(PREFIX + "promote_agent_to_production", "default",
                     tool_input={"args": {"permission_mode": "bypassPermissions"}})
    assert _decision(_run(stricter))[0] == "deny"


def test_a_forged_mode_cannot_break_the_answer():
    """A mode value with a backslash or quote is never echoed into the JSON:
    the answer stays a valid deny."""
    call = json.dumps(_call(PREFIX + "promote_agent_to_production", "default"))
    call = call.replace('"tool_input": {', '"tool_input": {"permission_mode": "x\\\\", ')
    decision, reason = _decision(_run(call))
    assert decision == "deny" and "unreadable permission mode" in reason


@pytest.mark.parametrize("inner", [PREFIX + "x_production", PREFIX + "list_agents", "Bash"])
def test_tool_input_cannot_choose_the_tool_the_reason_names(inner):
    """call_approved_agent_tool forwards model-written args; a nested
    tool_name in them never replaces the real one in the reason."""
    real = PREFIX + "call_approved_agent_tool"
    for nested in ({"tool_name": inner}, {"args": {"tool_name": inner}}):
        decision, reason = _decision(_run(_call(real, tool_input={"slug": "a", **nested})))
        assert decision == "ask"
        assert real in reason and "requires-approval as already approved" in reason
        assert inner not in reason.replace(real, "")


def test_matcher_is_the_only_gate():
    """Negative control: the script does not re-check the name, so a tool the
    matcher would never send it is still asked about, with the generic reason
    (it names no tool it cannot vouch for)."""
    decision, reason = _decision(_run(_call(PREFIX + "list_agents")))
    assert decision == "ask"
    assert "list_agents" not in reason and OFF_HINT in reason


@pytest.mark.parametrize("payload", ['{"permission_mode":"default","tool_input": {}}',
                                     '{"permission_mode":"default","tool_name": 7}',
                                     '{"permission_mode":"default","tool_name": "' + PREFIX + 'x\\"_production"}'])
def test_script_asks_when_it_cannot_read_the_tool(payload):
    """The matcher already selected a production tool, so a name the script
    cannot read is asked about with the generic reason, never waved through."""
    decision, reason = _decision(_run(payload))
    assert decision == "ask"
    assert OFF_HINT in reason


@pytest.mark.parametrize("payload", ["", "not json", '{"tool_name": "' + PREFIX + 'x_production"}'])
def test_script_denies_when_it_cannot_read_the_mode(payload):
    """Fail closed: no readable mode is refused, and the answer is still valid JSON."""
    assert _decision(_run(payload))[0] == "deny"


def _bin(tmp_path, tools):
    d = tmp_path / "bin"
    d.mkdir()
    for t in tools:
        src = shutil.which(t)
        assert src, "{} is needed to build the restricted PATH".format(t)
        (d / t).symlink_to(src)
    return str(d)


def test_script_needs_no_python(tmp_path):
    """No Python on the machine: the full answer, tool name included."""
    path = _bin(tmp_path, ["sh", "cat", "sed", "head", "tr"])
    decision, reason = _decision(_run(_call(PREFIX + "launch_ticket_production"), path=path))
    assert decision == "ask"
    assert PREFIX + "launch_ticket_production" in reason


def test_script_carries_no_python():
    text = (PLUGIN / SCRIPT).read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "python" not in code


def test_off_switch_turns_it_off_only_on_the_exact_value_and_says_so():
    tool = PREFIX + "promote_agent_to_production"
    out = json.loads(_run(_call(tool), {OFF_VAR: "off"}))
    assert "hookSpecificOutput" not in out
    assert "OFF" in out["systemMessage"] and tool in out["systemMessage"]
    for value in ("", "0", "false", "OFF", " off", "no", "ask"):
        assert _decision(_run(_call(tool), {OFF_VAR: value}))[0] == "ask", value


@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk", "auto", None])
def test_off_never_lifts_a_deny(mode):
    """Off removes the desk question only: where nobody answers it still denies."""
    tool = PREFIX + "promote_agent_to_production"
    assert _decision(_run(_call(tool, mode), {OFF_VAR: "off"}))[0] == "deny"


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
@pytest.mark.parametrize("body", [
    {"env": {OFF_VAR: "off"}},
    {"pluginConfigs": {"soleon": {"options": {OPTION: "off"}}}},
])
def test_a_project_setting_cannot_turn_it_off(tmp_path, name, body):
    """Verified on 2.1.325: a project's settings env entry reaches the hook.
    The hook ignores off when the project's .claude settings name the option."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / name).write_text(json.dumps(body), encoding="utf-8")
    tool = PREFIX + "promote_agent_to_production"
    decision, reason = _decision(_run(_call(tool), {OFF_VAR: "off"}, project=tmp_path))
    assert decision == "ask" and "honoured only from your user settings" in reason


def test_an_unrelated_project_setting_leaves_off_alone(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"env": {"OTHER": "1"}}', encoding="utf-8")
    out = json.loads(_run(_call(PREFIX + "promote_agent_to_production"), {OFF_VAR: "off"}, project=tmp_path))
    assert "hookSpecificOutput" not in out and "OFF" in out["systemMessage"]


def test_old_env_var_no_longer_turns_it_off():
    """The off switch left the shared env namespace: a repository's settings
    env block setting the old variable changes nothing."""
    tool = PREFIX + "promote_agent_to_production"
    assert _decision(_run(_call(tool), {"SOLEON_ASK_BEFORE_PRODUCTION": "off"}))[0] == "ask"


def test_plugin_declares_the_off_switch_option():
    meta = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    opt = meta["userConfig"][OPTION]
    assert opt["type"] == "string" and opt["options"] == ["ask", "off"] and opt["default"] == "ask"
    assert OFF_VAR == "CLAUDE_PLUGIN_OPTION_" + OPTION.upper()


def test_matcher_prefix_is_the_shipped_plugin_and_server():
    """Claude Code names plugin tools mcp__plugin_<plugin>_<server>__<tool>;
    renaming either would make the matcher fire for nothing."""
    meta = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    servers = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert len(servers) == 1
    derived = "mcp__plugin_{}_{}__".format(meta["name"], next(iter(servers)))
    assert derived == PREFIX
    assert _entry()["matcher"].startswith("^" + derived + "(")


def test_every_gated_name_is_a_spec_name():
    """The gated set is exactly the §4 production tier: nothing invented."""
    assert set(PRODUCTION) <= set(NEW)


def test_release_note_documents_the_hook_and_its_off_switch():
    text = NOTE.read_text(encoding="utf-8")
    assert "## Ask before production (plugin hook)" in text
    section = text.split("## Ask before production (plugin hook)", 1)[1].split("\n## ", 1)[0]
    assert "`{}`".format(OPTION) in section
    assert "SOLEON_ASK_BEFORE_PRODUCTION" not in text
    for needle in ("claude -p", "call_approved_agent_tool", "bypassPermissions", "dontAsk",
                   "user settings", "permission rules", "asserted by the model", "2.1.325"):
        assert needle in section, needle
