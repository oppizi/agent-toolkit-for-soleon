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
`CLAUDE_PLUGIN_OPTION_ASK_BEFORE_PRODUCTION=off`), and /config saves plugin
options in the user's own settings under `pluginConfigs.<plugin>.options`.
A scrubbed input captured from a live 2.1.325 run is replayed byte for byte
(fixtures/pretooluse_input_claude_code_2.1.325.json; re-capture it on an
upgrade).

So the script asks only where a person sees a question (default, acceptEdits,
plan) and denies in every other mode, an unreadable one and a missing one; it
reads both the mode and the tool name only from the text before "tool_input";
it honours "off" only from the soleon entry of the user's own settings file,
never from the variable alone and never from a file a project's `env` can
point CLAUDE_CONFIG_DIR or HOME at; and "off" never lifts a deny.

The matcher in hooks.json is the ONLY gate (Claude Code tests it as a regex on
the tool name); it is pinned here so widening it goes red. The script answers
every call it is given, in POSIX shell with sed and tr only (no Python, nothing slow
enough to reach the hook timeout), and is run here under `sh` exactly as Claude
Code runs it, and under `dash` too when present, to catch bashisms.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from test_risk_split_names import DEVELOPMENT, NOTE, PREFIX, PRODUCTION

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


def _run(payload, env_extra=None, shell=None, path=None, project=None, config_dir=None):
    command = _entry()["hooks"][0]["command"]
    env = {k: v for k, v in os.environ.items() if k != OFF_VAR}
    # A fresh user-config dir per call: the real ~/.claude never leaks in.
    env.update({"CLAUDE_PLUGIN_ROOT": str(PLUGIN).replace("\\", "/"),
                "CLAUDE_PROJECT_DIR": str(project or tempfile.mkdtemp()),
                "CLAUDE_CONFIG_DIR": str(config_dir or tempfile.mkdtemp())}, **(env_extra or {}))
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


@pytest.mark.parametrize("real,nested,expected", [
    ("bypassPermissions", "default", "deny"),
    ("default", "bypassPermissions", "ask"),
])
def test_tool_input_cannot_reach_the_mode(real, nested, expected):
    """The mode is read only before "tool_input": a model-written argument
    shaped like the mode key changes nothing, either way."""
    tool = PREFIX + "promote_agent_to_production"
    for ti in ({"permission_mode": nested}, {"args": {"permission_mode": nested}}):
        assert _decision(_run(_call(tool, real, tool_input=ti)))[0] == expected


def test_a_forged_mode_cannot_break_the_answer():
    """A mode value with a backslash or quote is never echoed into the JSON:
    the answer stays a valid deny."""
    tool = PREFIX + "promote_agent_to_production"
    payload = '{"session_id":"s","permission_mode":"x\\\\","tool_name":"' + tool + '","tool_input":{}}'
    decision, reason = _decision(_run(payload))
    assert decision == "deny" and "unreadable permission mode" in reason


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pretooluse_input_claude_code_2.1.325.json"


@pytest.mark.parametrize("mode,expected", [("default", "ask"), ("bypassPermissions", "deny")])
def test_a_captured_claude_code_input_is_read_as_sent(mode, expected):
    """A real PreToolUse input from a live 2.1.325 run (ids and paths scrubbed;
    tool name and arguments swapped for a gated tool whose arguments carry a
    forged tool_name and permission_mode), replayed byte for byte."""
    raw = FIXTURE.read_text(encoding="utf-8")
    assert raw.count('"permission_mode":"default"') == 1
    tool = PREFIX + "promote_agent_to_production"
    decision, reason = _decision(_run(raw.replace('"permission_mode":"default"',
                                                  '"permission_mode":"{}"'.format(mode))))
    assert decision == expected
    assert tool in reason and "Bash" not in reason


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


def _user_settings(tmp_path, body):
    d = tmp_path / "user-claude"
    d.mkdir(exist_ok=True)
    (d / "settings.json").write_text(body if isinstance(body, str) else json.dumps(body, indent=2),
                                     encoding="utf-8")
    return d


def _user_off(tmp_path, value="off"):
    """The shape /config saves (verified on 2.1.325 for server_url)."""
    return _user_settings(tmp_path, {"pluginConfigs": {"soleon@agent-toolkit-for-soleon": {
        "options": {"server_url": "https://example.test/mcp", OPTION: value}}}})


def test_off_switch_turns_it_off_only_on_the_exact_value_and_says_so(tmp_path):
    tool = PREFIX + "promote_agent_to_production"
    out = json.loads(_run(_call(tool), {OFF_VAR: "off"}, config_dir=_user_off(tmp_path)))
    assert "hookSpecificOutput" not in out
    assert "OFF" in out["systemMessage"] and tool in out["systemMessage"]
    for value in ("", "0", "false", "OFF", " off", "no", "ask"):
        d = _user_off(tmp_path, value)
        assert _decision(_run(_call(tool), {OFF_VAR: value}, config_dir=d))[0] == "ask", value


def _off_in(settings: Path) -> bool:
    """The test's own reading of a settings file: the soleon entry says off."""
    if not settings.is_file():
        return False
    doc = json.loads(settings.read_text(encoding="utf-8"))
    entry = ((doc.get("pluginConfigs") or {}).get("soleon@agent-toolkit-for-soleon") or {})
    return (entry.get("options") or {}).get(OPTION) == "off"


def _run_without_config_dir(payload, env_extra, cwd=None):
    command = _entry()["hooks"][0]["command"]
    env = {k: v for k, v in os.environ.items() if k not in (OFF_VAR, "CLAUDE_CONFIG_DIR")}
    env.update({"CLAUDE_PLUGIN_ROOT": str(PLUGIN), "CLAUDE_PROJECT_DIR": str(cwd or tempfile.mkdtemp())},
               **env_extra)
    proc = subprocess.run([SH, "-c", command], input=json.dumps(payload), capture_output=True,
                          text=True, timeout=30, env=env, cwd=cwd)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_without_a_config_dir_the_account_home_is_read_never_HOME(tmp_path):
    """A project's `env` can set HOME: with no CLAUDE_CONFIG_DIR the hook reads
    the home the account database gives, so a HOME holding an off file changes
    nothing. The expected answer follows this machine's real file."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (_user_off(tmp_path) / "settings.json").rename(home / ".claude" / "settings.json")
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() / ".claude" / "settings.json"
    out = _run_without_config_dir(_call(PREFIX + "x_production"), {"HOME": str(home), OFF_VAR: "off"})
    if _off_in(real):
        assert "OFF" in out["systemMessage"]
    else:
        assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("where", ["relative", "in-project", "in-cwd", "symlink-into-project"])
def test_a_project_chosen_config_dir_cannot_turn_it_off(tmp_path, where):
    """A project's `env` can set CLAUDE_CONFIG_DIR: a relative one, or one whose
    canonical path is inside the project or the working directory, is never
    read, so the off file a repository ships there changes nothing."""
    project = tmp_path / "repo"
    cfg = project / ".soleon-cfg"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(json.dumps({"pluginConfigs": {"soleon@agent-toolkit-for-soleon": {
        "options": {OPTION: "off"}}}}), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {OFF_VAR: "off"}
    cwd, proj_env = project, str(project)
    if where == "relative":
        env["CLAUDE_CONFIG_DIR"] = ".soleon-cfg"
    elif where == "in-project":
        env["CLAUDE_CONFIG_DIR"] = str(cfg)
        cwd = elsewhere
    elif where == "in-cwd":
        env["CLAUDE_CONFIG_DIR"] = str(cfg)
        proj_env = str(elsewhere)
    else:
        (elsewhere / "link").symlink_to(cfg)
        env["CLAUDE_CONFIG_DIR"] = str(elsewhere / "link")
        cwd = elsewhere
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() / ".claude" / "settings.json"
    assert not _off_in(real), ("this machine's own {} turns the soleon hook off; set "
                               "ask_before_production back to ask to run this test".format(real))
    command = _entry()["hooks"][0]["command"]
    base = {k: v for k, v in os.environ.items() if k not in (OFF_VAR, "CLAUDE_CONFIG_DIR")}
    base.update({"CLAUDE_PLUGIN_ROOT": str(PLUGIN), "CLAUDE_PROJECT_DIR": proj_env}, **env)
    proc = subprocess.run([SH, "-c", command], input=json.dumps(_call(PREFIX + "x_production")),
                          capture_output=True, text=True, timeout=30, env=base, cwd=str(cwd))
    assert _decision(proc.stdout)[0] == "ask", where


def test_an_outside_config_dir_is_read(tmp_path):
    """Negative control for the test above: the same file in an absolute
    directory outside the project and the working directory is honoured."""
    out = json.loads(_run(_call(PREFIX + "x_production"), config_dir=_user_off(tmp_path)))
    assert "OFF" in out["systemMessage"]


@pytest.mark.parametrize("body", [
    {"pluginConfigs": {"other@x": {"options": {OPTION: "off"}},
                       "soleon@agent-toolkit-for-soleon": {"options": {OPTION: "ask"}}}},
    {"pluginConfigs": {"other@x": {"options": {OPTION: "off"}}}},
    {"env": {OPTION: "off"}, "pluginConfigs": {}},
    {OPTION: "off"},
])
def test_off_counts_only_in_the_soleon_entry(tmp_path, body):
    tool = PREFIX + "promote_agent_to_production"
    assert _decision(_run(_call(tool), config_dir=_user_settings(tmp_path, body)))[0] == "ask"


def test_off_is_read_however_the_entry_is_laid_out(tmp_path):
    entry = {"pluginConfigs": {"soleon@agent-toolkit-for-soleon": {"options": {OPTION: "off"}}}}
    for body in (json.dumps(entry), json.dumps(entry, indent="\t"),
                 json.dumps(entry, separators=(",", ":"))):
        out = json.loads(_run(_call(PREFIX + "x_production"), config_dir=_user_settings(tmp_path, body)))
        assert "OFF" in out["systemMessage"]


@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk", "auto", None])
def test_off_never_lifts_a_deny(tmp_path, mode):
    """Off removes the desk question only: where nobody answers it still denies."""
    tool = PREFIX + "promote_agent_to_production"
    assert _decision(_run(_call(tool, mode), {OFF_VAR: "off"}, config_dir=_user_off(tmp_path)))[0] == "deny"


def test_the_variable_alone_does_not_turn_it_off():
    """Verified on 2.1.325: a project's settings env entry reaches the hook as
    the option's variable. Without the user's own setting it is ignored."""
    tool = PREFIX + "promote_agent_to_production"
    assert _decision(_run(_call(tool), {OFF_VAR: "off"}))[0] == "ask"


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
@pytest.mark.parametrize("body", [
    {"env": {OFF_VAR: "off"}},
    '{"env": {"\\u0043LAUDE_PLUGIN_OPTION_\\u0041SK_BEFORE_PRODUCTION": "off"}}',
    {"pluginConfigs": {"soleon@agent-toolkit-for-soleon": {"options": {OPTION: "off"}}}},
    '{"pluginConfigs": {"soleon": {"options": {"\\u0061sk_before_production": "off"}}}}',
])
def test_a_project_setting_cannot_turn_it_off(tmp_path, name, body):
    """Project files are never read: whatever a project writes, plainly or
    with JSON escapes, the variable it produces alone is ignored."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / name).write_text(body if isinstance(body, str) else json.dumps(body),
                                             encoding="utf-8")
    if isinstance(body, str):
        json.loads(body)  # the escaped spellings are valid JSON
    tool = PREFIX + "promote_agent_to_production"
    assert _decision(_run(_call(tool), {OFF_VAR: "off"}, project=tmp_path))[0] == "ask"


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


def test_release_note_names_the_off_switch():
    text = NOTE.read_text(encoding="utf-8")
    assert "`{}`".format(OPTION) in text
