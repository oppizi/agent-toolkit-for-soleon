"""bin/soleon_freshness.py + its two hooks — say, once per change, when a session
runs something older than what exists (2026-11-20).

No network: GitHub is a fake opener, the tool list a fake reader, the live
checkout's git state a fake. The prompt gate runs under bash as Claude Code runs
it, with the script swapped for a stub that records it was started."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from _local_emulation_fixtures import BIN, PLUGIN

import sys
sys.path.insert(0, str(BIN))
import soleon_freshness as fr  # noqa: E402

HOOKS = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
BASH = shutil.which("bash")
SESSION = "3383ca9a-5356-413a-bd78-8e080c993de0"
NOW = 1_800_000_000.0


def _plugin(root: Path, name="soleon", version="1.0.30", url="${user_config.server_url}") -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": name, "version": version,
        "userConfig": {"server_url": {"default": "https://mcp-dev.oppizi.com/mcp"}}}), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"soleon": {"url": url}}}), encoding="utf-8")
    return root


def _copy_install(tmp_path: Path, version="1.0.30", others=()) -> Path:
    base = tmp_path / "cache" / "agent-toolkit-for-soleon" / "soleon"
    for v in others:
        _plugin(base / v, version=v)
    return _plugin(base / version, version=version)


class Opener:
    """A fake urlopen answering the published manifest; counts its calls."""

    def __init__(self, version):
        self.version, self.calls = version, 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        if isinstance(self.version, Exception):
            raise self.version
        return io.BytesIO(json.dumps({"version": self.version}).encode())


def _start(data: Path, root: Path, source="startup", execpath=""):
    hook = {"session_id": SESSION, "source": source, "hook_event_name": "SessionStart"}
    return fr.session_start(hook, data, root, {"CLAUDE_CODE_EXECPATH": execpath}, now=NOW)


def _work(data, tools=None, opener=None, live=None, now=NOW, runs=()):
    return fr.work(data, SESSION, now=now, tools_reader=lambda _url: tools,
                   opener=opener or Opener("1.0.30"), live=live or (lambda _root: None),
                   runs_reader=lambda: list(runs))


def test_a_failed_daily_knowledge_base_refresh_is_said_once(tmp_path):
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path))
    failed = {"kb": "2ebb3c747a88", "ok": False, "startedAt": NOW - 600, "finishedAt": NOW - 60,
              "reason": "REFRESH FAILED github sign-in expired", "log": "C:/logs/kb.log"}
    (said,) = _work(data, runs=[failed])
    assert said.startswith("The daily refresh of knowledge base 2ebb3c747a88 failed "
                           "(REFRESH FAILED github sign-in expired)")
    assert said.endswith("Its log: C:/logs/kb.log")
    assert _work(data, runs=[failed]) == []
    assert _work(data, runs=[{**failed, "ok": True}]) == []
    assert _work(data, runs=[{**failed, "startedAt": NOW + 10}]) != [], "a new failure is a new change"


def test_a_failed_refresh_whose_daily_task_is_off_says_nothing(tmp_path):
    """Dan 2026-11-23: a paused refresh kept saying this morning's failure in every session."""
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path))
    failed = {"kb": "2ebb3c747a88", "ok": False, "startedAt": NOW - 600, "finishedAt": NOW - 60,
              "reason": "REFRESH FAILED operator access revoked", "log": "C:/logs/kb.log"}
    assert _work(data, runs=[{**failed, "on": False}]) == []
    (said,) = _work(data, runs=[{**failed, "on": True}])
    assert said.startswith("The daily refresh of knowledge base 2ebb3c747a88 failed")


def test_a_running_or_good_refresh_says_nothing(tmp_path):
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path))
    assert _work(data, runs=[{"kb": "k", "running": True, "startedAt": NOW}]) == []
    assert _work(data, runs=[{"kb": "k", "ok": True, "startedAt": NOW, "finishedAt": NOW}]) == []


# --- what the session runs -------------------------------------------------

def test_the_running_build_comes_from_the_extension_folder_in_its_binary_path(tmp_path):
    ext = tmp_path / "extensions"
    exe = ext / "anthropic.claude-code-2.1.319-win32-x64" / "resources" / "native-binary" / "claude.exe"
    assert fr.running_build(str(exe)) == {"version": "2.1.319", "platform": "win32-x64", "extensions": str(ext)}
    assert fr.running_build("") is None
    assert fr.running_build("/home/me/.local/share/claude/versions/2.1.328") is None  # a terminal install: skip


def test_the_newest_build_is_the_highest_installed_for_the_same_platform(tmp_path):
    for name in ("anthropic.claude-code-2.1.319-win32-x64", "anthropic.claude-code-2.1.328-win32-x64",
                 "anthropic.claude-code-2.1.400-darwin-arm64", "anthropic.claude-code-2.1.999-win32-x64"):
        (tmp_path / name).mkdir()
    for name in ("anthropic.claude-code-2.1.319-win32-x64", "anthropic.claude-code-2.1.328-win32-x64",
                 "anthropic.claude-code-2.1.400-darwin-arm64"):
        (tmp_path / name / "resources").mkdir()  # 2.1.999 is half-written: no resources yet
    assert fr.newest_build(str(tmp_path), "win32-x64") == "2.1.328"


def test_the_server_is_the_plugin_setting_default_unless_mcp_json_names_one(tmp_path):
    assert fr.plugin_info(_plugin(tmp_path / "a"))["server"] == "https://mcp-dev.oppizi.com/mcp"
    prod = _plugin(tmp_path / "b", name="soleon-prod", url="https://mcp.oppizi.com/mcp")
    info = fr.plugin_info(prod)
    assert info["server"] == "https://mcp.oppizi.com/mcp" and info["kind"] == "generated"


def test_a_cache_install_is_a_copy(tmp_path):
    assert fr.plugin_info(_copy_install(tmp_path))["kind"] == "copy"


# --- the plugin check ------------------------------------------------------

def test_a_copy_behind_the_published_version_is_told_to_update_then_reload_once(tmp_path):
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path))
    opener = Opener("1.0.31")
    said = _work(data, opener=opener)
    assert said == ["Soleon toolkit 1.0.31 is published; this session runs 1.0.30. Run "
                    "`/plugin marketplace update agent-toolkit-for-soleon`, then /reload-plugins."]
    assert _work(data, opener=opener) == []        # once per change
    assert opener.calls == 1                       # the published version is cached an hour
    assert _work(data, opener=Opener("1.0.32"), now=NOW + 3601) != []  # a newer one is a new change


def test_a_copy_with_a_newer_version_installed_is_told_to_reload_only(tmp_path):
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path, "1.0.30", others=("1.0.31",)))
    assert _work(data, opener=Opener("1.0.31")) == [
        "Soleon toolkit 1.0.31 is installed; this session still runs 1.0.30. Run /reload-plugins."]


def test_an_unreadable_published_version_says_nothing(tmp_path):
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path))
    assert _work(data, opener=Opener(OSError("offline"))) == []


def test_a_live_checkout_behind_origin_main_is_told_to_sync(tmp_path, monkeypatch):
    data = tmp_path / "data"
    root = _plugin(tmp_path / "toolkit-live" / "plugins" / "soleon")
    monkeypatch.setattr(fr, "install_kind", lambda _r, _n: "live")
    _start(data, root)
    live = {"top": "C:/Soleon/toolkit-live", "branch": "live", "behind": 3, "tip": "abc1234"}
    assert _work(data, live=lambda _r: live) == [
        "Soleon toolkit: your live checkout is 3 commit(s) behind origin/main. Run "
        "`python3 C:/Soleon/toolkit-live/harness/dev_live.py sync`, then /reload-plugins."]
    assert _work(data, live=lambda _r: live) == []
    # A developer's own branch is theirs: no advice to throw it away.
    assert _work(data, live=lambda _r: {**live, "branch": "feat/x", "tip": "def5678"}) == []


# --- the tool check --------------------------------------------------------

def test_the_first_tool_list_is_the_baseline_and_a_change_is_said_once(tmp_path):
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path))
    before = {"get_agent": "a", "list_agents": "b"}
    assert _work(data, tools=before) == []
    after = {"get_agent": "a2", "list_agents": "b", "search_knowledge_base": "c"}
    assert _work(data, tools=after) == [
        "Soleon tools changed since this session connected (added `search_knowledge_base`; changed "
        "`get_agent`). Run /mcp, pick plugin:soleon:soleon, choose Reconnect."]
    assert _work(data, tools=after) == []


def test_an_unreadable_tool_list_says_nothing_and_keeps_the_baseline(tmp_path):
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path))
    _work(data, tools={"a": "1"})
    assert _work(data, tools=None) == []
    assert fr.load_state(data, SESSION)["tools"] == {"a": "1"}


def test_a_compact_keeps_the_tool_baseline_and_a_new_start_resets_it(tmp_path):
    data = tmp_path / "data"
    root = _copy_install(tmp_path)
    _start(data, root)
    _work(data, tools={"a": "1"})
    _start(data, root, source="compact")
    assert fr.load_state(data, SESSION)["tools"] == {"a": "1"}
    _start(data, root, source="resume")
    assert fr.load_state(data, SESSION)["tools"] is None


def test_the_fingerprint_sees_a_changed_description_or_schema():
    one = fr.fingerprint([{"name": "t", "description": "d", "inputSchema": {"type": "object"}}])
    assert one != fr.fingerprint([{"name": "t", "description": "d2", "inputSchema": {"type": "object"}}])
    assert one != fr.fingerprint([{"name": "t", "description": "d", "inputSchema": {"type": "string"}}])


# --- the build check -------------------------------------------------------

def _extensions(tmp_path, *versions):
    for v in versions:
        (tmp_path / "ext" / "anthropic.claude-code-{}-win32-x64".format(v) / "resources").mkdir(parents=True)
    return str(tmp_path / "ext" / "anthropic.claude-code-{}-win32-x64".format(versions[0])
               / "resources" / "native-binary" / "claude.exe")


def test_an_old_claude_code_build_is_told_to_reload_the_window_once(tmp_path):
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path), execpath=_extensions(tmp_path, "2.1.319", "2.1.328"))
    assert _work(data) == ["This conversation runs Claude Code 2.1.319; 2.1.328 is installed. Run "
                           "Developer: Reload Window in VS Code, then reopen the conversation."]
    assert _work(data) == []


def test_the_generated_prod_copy_leaves_the_build_and_version_to_the_main_plugin(tmp_path):
    data = tmp_path / "data"
    prod = _plugin(tmp_path / "prod", name="soleon-prod", url="https://mcp.oppizi.com/mcp")
    _start(data, prod, execpath=_extensions(tmp_path, "2.1.319", "2.1.328"))
    assert _work(data, opener=Opener("9.9.9")) == []


# --- the hand-off to the next prompt ---------------------------------------

def test_the_message_is_shown_to_the_person_and_told_to_the_model(tmp_path):
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path))
    _work(data, opener=Opener("1.0.31"))
    _work(data, tools={"a": "1"})
    _work(data, tools={"a": "1", "b": "2"})
    out = json.loads(fr.ready_path(data, SESSION).read_text(encoding="utf-8"))
    lines = out["systemMessage"].split("\n")
    assert len(lines) == 2 and "1.0.31" in lines[0] and "`b`" in lines[1]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert out["hookSpecificOutput"]["additionalContext"].endswith(out["systemMessage"])


def test_a_second_worker_does_not_run_while_one_holds_the_lock(tmp_path):
    data = tmp_path / "data"
    _start(data, _copy_install(tmp_path))
    lock = data / "fresh-{}.lock".format(SESSION)
    lock.write_text(str(NOW), encoding="ascii")
    assert _work(data, opener=Opener("1.0.31")) == []
    assert _work(data, opener=Opener("1.0.31"), now=NOW + fr.LOCK_STALE_S + 1) != []  # a stale lock is taken


def test_the_hook_entry_never_fails_the_session(tmp_path):
    spawned = []
    code = fr.main(["session-start", "--data", str(tmp_path / "d"), "--loaded", str(tmp_path / "missing")],
                   stdin=io.StringIO(json.dumps({"session_id": SESSION, "source": "startup"})),
                   spawn=lambda d, s: spawned.append(s))
    assert code == 0 and spawned == []
    assert fr.main(["check", "../../etc", "--data", str(tmp_path)], spawn=lambda d, s: spawned.append(s)) == 0
    assert spawned == []  # a session id that is not an id is never a path


def test_session_start_starts_the_worker(tmp_path):
    spawned = []
    fr.main(["session-start", "--data", str(tmp_path / "d"), "--loaded", str(_copy_install(tmp_path))],
            stdin=io.StringIO(json.dumps({"session_id": SESSION, "source": "startup"})),
            spawn=lambda d, s: spawned.append(s))
    assert spawned == [SESSION]


# --- the prompt gate, run as Claude Code runs it ---------------------------

def _gate(tmp_path: Path, data: Path):
    plugin = tmp_path / "gate-plugin"
    marker = tmp_path / "started"
    (plugin / "bin").mkdir(parents=True, exist_ok=True)
    (plugin / "bin" / "soleon_freshness.py").write_text(
        "import pathlib\npathlib.Path({!r}).write_text('1')\n".format(str(marker)), encoding="utf-8")
    (plugin / "bin" / "soleon_newest.py").write_text((BIN / "soleon_newest.py").read_text(encoding="utf-8"),
                                                     encoding="utf-8")
    if marker.exists():
        marker.unlink()
    (entry,) = HOOKS["UserPromptSubmit"]
    (hook,) = entry["hooks"]
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin).replace("\\", "/")}
    env.pop("CLAUDE_PLUGIN_DATA", None)
    if data is not None:
        env["CLAUDE_PLUGIN_DATA"] = str(data).replace("\\", "/")
    payload = {"session_id": SESSION, "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
    started = time.time()
    proc = subprocess.run([BASH, "-c", hook["command"]], input=json.dumps(payload), capture_output=True,
                          text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, marker.exists(), time.time() - started


needs_bash = pytest.mark.skipif(BASH is None, reason="bash runs the plugin's hook commands")


@needs_bash
def test_the_prompt_gate_starts_python_at_most_every_ten_minutes(tmp_path):
    data = tmp_path / "data"
    out, started, _ = _gate(tmp_path, data)
    assert out == "" and started          # no stamp yet: the worker is started
    out, started, _ = _gate(tmp_path, data)
    assert out == "" and not started      # stamped a moment ago: shell only


@needs_bash
def test_the_prompt_gate_shows_a_waiting_message_exactly_once(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "fresh-{}.stamp".format(SESSION)).write_text("", encoding="utf-8")
    message = {"systemMessage": "Soleon toolkit 1.0.31 is published", "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": "x"}}
    fr.ready_path(data, SESSION).write_text(json.dumps(message), encoding="utf-8")
    out, started, _ = _gate(tmp_path, data)
    assert json.loads(out) == message and not started
    out, _, _ = _gate(tmp_path, data)
    assert out == ""


@needs_bash
def test_the_prompt_gate_does_nothing_without_a_plugin_data_folder(tmp_path):
    out, started, _ = _gate(tmp_path, None)
    assert out == "" and not started


def test_only_the_freshness_gate_runs_before_a_prompt():
    """2026-11-04: every prompt of every session waited on the platform. The one
    prompt hook may read a file in shell; Python starts only for the 10-minute
    check, and that only starts a detached worker (no network in the hook)."""
    (entry,) = HOOKS["UserPromptSubmit"]
    (hook,) = entry["hooks"]
    assert "soleon_freshness.py" in hook["command"] and " check " in hook["command"]
    assert hook["command"].index("-mmin -10") < hook["command"].index("python")
