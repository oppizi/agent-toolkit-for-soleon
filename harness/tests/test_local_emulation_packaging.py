"""Local agent emulation ships whole, and works from the plugin alone.

`pull-agent`, `run-local-eval`, the `bin/` scripts and `hooks/hooks.json` only
work together: the skill writes a subagent whose stdio servers are
`${CLAUDE_PLUGIN_ROOT}/bin/*.py`, the hooks call those same scripts, and the
refresh hook calls back into the skill's `pull_agent.py`. A plugin missing one
of them installs cleanly and then fails at the first pull or save.

So this checks every hook command resolves inside the plugin, and runs a
materialize from the plugin copied ALONE to a temp dir, the way
`test_packaging.py` proves the converter has no hidden dependency on the repo.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _local_emulation_fixtures import SERVER_URL, SLUG, make_pulled_dir

BET_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = BET_ROOT / "plugins" / "soleon"


def test_plugin_ships_the_whole_kit():
    missing = [f"skills/{s}" for s in ("pull-agent", "run-local-eval")
               if not (PLUGIN / "skills" / s / "SKILL.md").is_file()]
    if not (PLUGIN / "hooks" / "hooks.json").is_file():
        missing.append("hooks/hooks.json")
    assert not missing, f"the plugin ships part of local emulation; missing {missing}"


def test_every_hook_command_resolves_inside_the_plugin():
    text = (PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8")
    paths = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"\\\s]+)", text)
    assert paths, "no ${CLAUDE_PLUGIN_ROOT} paths found — hook format changed?"
    dangling = sorted({p for p in paths if not (PLUGIN / p).is_file()})
    assert not dangling, f"hook commands point at missing files {dangling}"


def test_plugin_alone_materializes_a_pulled_agent(tmp_path):
    """Copy the plugin by itself, resolve the plugin root the way Claude Code
    does (CLAUDE_PLUGIN_ROOT, no --plugin-root), and check the subagent and the
    permission rules point at THIS copy — its own bin/ servers and its own
    `mcp__plugin_soleon_soleon__*` rule."""
    plugin = tmp_path / "plugin"
    shutil.copytree(PLUGIN, plugin, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    project = tmp_path / "project"
    project.mkdir()
    agent_dir = make_pulled_dir(project)
    home = tmp_path / "home"
    home.mkdir()

    proc = subprocess.run(
        [sys.executable, str(plugin / "skills" / "pull-agent" / "assets" / "pull_agent.py"),
         "materialize", "--slug", SLUG, "--dir", str(agent_dir),
         "--server-url", SERVER_URL, "--model", "sonnet"],
        capture_output=True, text=True, timeout=60, cwd=str(project),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "CLAUDE_PLUGIN_ROOT": str(plugin)},
    )
    assert proc.returncode == 0, proc.stderr
    # The refresh hook re-materializes from the root recorded here, so it must
    # be the copy that did the pull.
    pull = json.loads((agent_dir / "pull.json").read_text(encoding="utf-8"))
    assert pull["pluginRoot"] == str(plugin.resolve())

    subagent = (home / ".claude" / "agents" / f"{SLUG}.md").read_text(encoding="utf-8")
    for server in ("soleon_workspace_mcp.py", "soleon_agent_tools_mcp.py"):
        assert str(plugin.resolve() / "bin" / server) in subagent, f"subagent does not run {server} from the copied plugin"

    settings = json.loads((project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert "mcp__plugin_soleon_soleon__*" in settings["permissions"]["allow"]
