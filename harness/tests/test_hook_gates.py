"""hooks/hooks.json — the hooks that fire on every tool call start Python only
when the call can concern a pulled agent, and nothing runs before a prompt.

2026-11-04 (agent-infra): with one agent pulled into the project, every
prompt of every session waited on the platform (~1 s, ~50 s when the draft
had changed) and every tool call started Python — in sessions that never
called the agent. These run the real hook commands under bash, with the
plugin's scripts swapped for one that records it was started."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from _local_emulation_fixtures import PLUGIN

HOOKS = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
BASH = shutil.which("bash")


def _command(event: str, script: str) -> str:
    for entry in HOOKS[event]:
        for hook in entry["hooks"]:
            if script in hook["command"]:
                return hook["command"]
    raise AssertionError("no {} hook runs {}".format(event, script))


def _started(tmp_path: Path, command: str, payload: dict, project: Path) -> bool:
    """Run `command` as Claude Code would; True when it started the script."""
    plugin = tmp_path / "plugin"
    marker = tmp_path / "started"
    (plugin / "bin").mkdir(parents=True, exist_ok=True)
    stub = "import sys, pathlib; sys.stdin.read(); pathlib.Path({!r}).write_text('1')\n".format(str(marker))
    for name in ("soleon_message_budget.py", "soleon_pull_refresh.py", "soleon_draft_sync.py"):
        (plugin / "bin" / name).write_text(stub, encoding="utf-8")
    if marker.exists():
        marker.unlink()
    proc = subprocess.run([BASH, "-c", command], input=json.dumps(payload), capture_output=True, text=True, timeout=60,
                          env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin).replace("\\", "/"),
                               "CLAUDE_PROJECT_DIR": str(project).replace("\\", "/")})
    assert proc.returncode == 0, proc.stderr
    return marker.exists()


pytestmark = pytest.mark.skipif(BASH is None, reason="bash runs the plugin's hook commands")


def test_nothing_runs_before_a_prompt():
    assert "UserPromptSubmit" not in HOOKS


def test_the_refresh_runs_only_when_a_subagent_starts_in_a_project_with_a_pulled_agent(tmp_path):
    (entry,) = [e for e in HOOKS["PreToolUse"] if any("soleon_pull_refresh.py" in h["command"] for h in e["hooks"])]
    assert entry["matcher"] == "Agent|Task"
    cmd = _command("PreToolUse", "soleon_pull_refresh.py")
    project = tmp_path / "project"
    project.mkdir()
    call = {"tool_name": "Agent", "tool_input": {"subagent_type": "demo-agent", "prompt": "hi"}}
    assert not _started(tmp_path, cmd, call, project)  # no pulled agent here
    (project / ".soleon" / "agents").mkdir(parents=True)
    assert _started(tmp_path, cmd, call, project)


def test_the_budget_check_starts_python_only_inside_a_subagent_run(tmp_path):
    cmd = _command("PreToolUse", "soleon_message_budget.py")
    main_thread = {"tool_name": "Read", "tool_input": {"file_path": "x"}, "cwd": str(tmp_path)}
    assert not _started(tmp_path, cmd, main_thread, tmp_path)
    assert _started(tmp_path, cmd, {**main_thread, "agent_id": "a1", "agent_type": "demo-agent"}, tmp_path)


def test_the_save_sync_starts_python_only_for_a_save_under_soleon(tmp_path):
    cmd = _command("PostToolUse", "soleon_draft_sync.py")
    assert not _started(tmp_path, cmd, {"tool_name": "Edit", "tool_input": {"file_path": "/repo/src/app.py"}}, tmp_path)
    assert _started(tmp_path, cmd, {"tool_name": "Edit",
                                    "tool_input": {"file_path": "/repo/.soleon/agents/demo-agent/SOUL.md"}}, tmp_path)
