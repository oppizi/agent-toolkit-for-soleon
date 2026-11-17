"""bin/soleon_newest.py runs a plugin script from the newest installed version (2026-11-17).

Claude Code keeps a running session on the plugin version it started with; every hook, pulled-agent
tool server and skill script starts through the launcher so a toolkit update reaches running sessions."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from _local_emulation_fixtures import BIN, PLUGIN

LAUNCHER = BIN / "soleon_newest.py"
SCRIPT = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "here = Path(__file__).resolve()\n"
    "version = next(p.name for p in here.parents if (p / '.claude-plugin').is_dir() or p.name == 'checkout')\n"
    "print(json.dumps({'version': version, 'argv': sys.argv[1:],\n"
    "                  'root': Path(os.environ.get('CLAUDE_PLUGIN_ROOT', '')).name, 'stdin': sys.stdin.read(),\n"
    "                  'path0': Path(sys.path[0]).parents[0].name}))\n"
)


def _install(cache: Path, version: str, scripts=("bin/x.py",)) -> Path:
    root = cache / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "soleon", "version": version}))
    (root / "bin").mkdir()
    (root / "bin" / "soleon_newest.py").write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    for rel in scripts:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(SCRIPT, encoding="utf-8")
    return root


def _run(launcher: Path, *args: str, stdin: str = "") -> dict:
    p = subprocess.run([sys.executable, str(launcher), *args], input=stdin, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_the_newest_version_runs_by_number_not_by_name(tmp_path):
    old = _install(tmp_path, "1.0.2")
    _install(tmp_path, "1.0.9")
    _install(tmp_path, "1.0.10")
    out = _run(old / "bin" / "soleon_newest.py", str(old / "bin" / "x.py"), "--a", "1", stdin="hook input")
    assert out == {"version": "1.0.10", "argv": ["--a", "1"], "root": "1.0.10", "stdin": "hook input",
                   "path0": "1.0.10"}


def test_a_relative_script_path_resolves_from_the_plugin_root(tmp_path):
    old = _install(tmp_path, "1.0.19")
    _install(tmp_path, "1.0.20")
    assert _run(old / "bin" / "soleon_newest.py", "bin/x.py")["version"] == "1.0.20"


def test_a_script_newer_versions_dropped_runs_from_the_newest_that_ships_it(tmp_path):
    old = _install(tmp_path, "1.0.18", scripts=("bin/x.py", "skills/s/assets/old.py"))
    _install(tmp_path, "1.0.19", scripts=("bin/x.py", "skills/s/assets/old.py"))
    _install(tmp_path, "1.0.20", scripts=("bin/x.py",))
    out = _run(old / "bin" / "soleon_newest.py", str(old / "skills/s/assets/old.py"))
    assert out["version"] == "1.0.19"


def test_a_folder_without_a_plugin_manifest_is_never_chosen(tmp_path):
    old = _install(tmp_path, "1.0.19")
    half = tmp_path / "1.0.30" / "bin"  # a half-written install: no .claude-plugin/plugin.json
    half.mkdir(parents=True)
    (half / "x.py").write_text("raise SystemExit('never')")
    assert _run(old / "bin" / "soleon_newest.py", str(old / "bin" / "x.py"))["version"] == "1.0.19"


def test_a_source_checkout_runs_the_script_as_given(tmp_path):
    script = tmp_path / "checkout" / "bin" / "x.py"
    script.parent.mkdir(parents=True)
    script.write_text(SCRIPT, encoding="utf-8")
    assert _run(LAUNCHER, str(script))["version"] == "checkout"


def test_every_hook_and_pulled_tool_server_starts_through_the_launcher():
    hooks = (PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8")
    commands = [h["command"] for group in json.loads(hooks)["hooks"].values() for entry in group for h in entry["hooks"]]
    assert commands and all("/bin/soleon_newest.py" in c for c in commands)
    pull = (PLUGIN / "skills" / "pull-agent" / "assets" / "pull_agent.py").read_text(encoding="utf-8")
    assert "[newest, ws_py," in pull and "[newest, tools_py," in pull
    for skill in ("create-agent", "deploy-agent", "pull-agent", "pull-knowledge-base"):
        text = (PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "python3 $ASSETS/" not in text and 'python3 "$PLUGIN/bin/soleon_newest.py" $ASSETS/' in text
