"""Local agent emulation ships whole, in every bundle that ships it.

`pull-agent`, `run-local-eval`, the `bin/` scripts and the `local-emulation`
hook are four catalogue kinds that only work together: the skill writes a
subagent whose stdio servers are `${CLAUDE_PLUGIN_ROOT}/bin/*.py`, the hook
calls those same scripts, and the refresh hook calls back into the skill's
`pull_agent.py`. A bundle carrying some of them installs cleanly and then
fails at the first pull or save — which `--check` cannot see, because every
copy it does have matches the catalogue.

So this asserts the set per bundle, and runs a materialize from each bundle
copied ALONE to a temp dir, the way `test_packaging.py` proves the builder
converter has no hidden dependency on the repo.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _local_emulation_fixtures import SERVER_URL, SLUG, make_pulled_dir

BET_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = BET_ROOT / "plugins"
BUNDLES = sorted(p for p in PLUGINS_DIR.iterdir() if (p / ".claude-plugin/plugin.json").is_file())

SKILLS = ("pull-agent", "run-local-eval")
HOOK = "local-emulation"
CATALOGUE_BIN = sorted(p.name for p in (PLUGINS_DIR / "bin").glob("*.py"))

# Exact, so losing the kit from a bundle fails as loudly as gaining it.
EXPECTED = {"builder", "admin"}


def _ships_any(bundle: Path) -> bool:
    return (
        any((bundle / "skills" / s).is_dir() for s in SKILLS)
        or (bundle / "hooks" / HOOK).is_dir()
        or any((bundle / "bin" / n).is_file() for n in CATALOGUE_BIN)
    )


def test_the_bundles_that_ship_local_emulation():
    assert CATALOGUE_BIN, "plugins/bin/ is empty — layout changed?"
    assert {b.name for b in BUNDLES if _ships_any(b)} == EXPECTED


@pytest.mark.parametrize("bundle", [b for b in BUNDLES if b.name in EXPECTED], ids=lambda b: b.name)
def test_bundle_ships_the_whole_kit(bundle):
    missing = [f"skills/{s}" for s in SKILLS if not (bundle / "skills" / s / "SKILL.md").is_file()]
    missing += [f"bin/{n}" for n in CATALOGUE_BIN if not (bundle / "bin" / n).is_file()]
    if not (bundle / "hooks" / HOOK / "hooks.json").is_file():
        missing.append(f"hooks/{HOOK}/hooks.json")
    assert not missing, f"{bundle.name} ships part of local emulation; missing {missing}"

    manifest = json.loads((bundle / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert f"./hooks/{HOOK}/hooks.json" in manifest.get("hooks", []), (
        f"{bundle.name}: hooks/{HOOK}/hooks.json is copied but not declared in plugin.json, "
        "so Claude Code never loads it"
    )


@pytest.mark.parametrize("bundle", [b for b in BUNDLES if b.name in EXPECTED], ids=lambda b: b.name)
def test_every_hook_command_resolves_inside_the_bundle(bundle):
    text = (bundle / "hooks" / HOOK / "hooks.json").read_text(encoding="utf-8")
    paths = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"\\\s]+)", text)
    assert paths, "no ${CLAUDE_PLUGIN_ROOT} paths found — hook format changed?"
    dangling = sorted({p for p in paths if not (bundle / p).is_file()})
    assert not dangling, f"{bundle.name}: hook commands point at missing files {dangling}"


@pytest.mark.parametrize("bundle", [b for b in BUNDLES if b.name in EXPECTED], ids=lambda b: b.name)
def test_bundle_alone_materializes_a_pulled_agent(bundle, tmp_path):
    """Copy the bundle by itself, resolve the plugin root the way Claude Code
    does (CLAUDE_PLUGIN_ROOT, no --plugin-root), and check the subagent and the
    permission rules point at THIS bundle — its own bin/ servers and its own
    `mcp__plugin_<name>_soleon__*` rule."""
    plugin = tmp_path / "plugin"
    shutil.copytree(bundle, plugin, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
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
    # be the bundle that did the pull.
    pull = json.loads((agent_dir / "pull.json").read_text(encoding="utf-8"))
    assert pull["pluginRoot"] == str(plugin.resolve())

    subagent = (home / ".claude" / "agents" / f"{SLUG}.md").read_text(encoding="utf-8")
    for server in ("soleon_workspace_mcp.py", "soleon_agent_tools_mcp.py"):
        assert str(plugin.resolve() / "bin" / server) in subagent, f"subagent does not run {server} from {bundle.name}"

    settings = json.loads((project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert f"mcp__plugin_{bundle.name}_soleon__*" in settings["permissions"]["allow"]
