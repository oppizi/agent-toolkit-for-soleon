"""Packaging boundary — the ship boundary must be real, textually AND
behaviorally.

1. plugin/ contains no reference to harness/, samples/, preflight/, runs, or
   transcripts (bet telemetry never ships).
2. Isolation smoke (F3): plugin/ copied ALONE to a temp dir still converts a
   spec end-to-end — proving no hidden dependency on the bet tree or repo.
3. Converter + engine are stdlib-only (no third-party imports).
"""
import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

BET_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = BET_ROOT / "plugin"

FORBIDDEN_REFS = ("harness/", "samples/", "preflight/", "runs.jsonl", "transcripts/")

STDLIB_OK = {
    "__future__", "annotations", "argparse", "ast", "json", "os", "platform",
    "re", "shutil", "subprocess", "sys", "pathlib", "datetime", "engine", "typing",
}


def test_no_telemetry_references_in_plugin():
    offenders = []
    for path in PLUGIN.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".md", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for ref in FORBIDDEN_REFS:
            for line in text.splitlines():
                if ref in line and "maintainers:" not in line.lower():
                    offenders.append(f"{path.relative_to(PLUGIN)}: {ref}")
    assert not offenders, f"plugin references bet telemetry: {offenders}"


def test_plugin_python_is_stdlib_only():
    for path in (PLUGIN / "skills/deploy-agent/assets").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                assert mod in STDLIB_OK, f"{path.name} imports non-stdlib {mod!r}"


def test_isolation_smoke_plugin_alone_converts(tmp_path):
    """Copy plugin/ alone to a temp dir; run the converter there end-to-end."""
    dest = tmp_path / "installed-plugin"
    shutil.copytree(PLUGIN, dest)

    soul = "You are a careful watcher.\n"
    spec = tmp_path / "spec.allium"
    spec.write_text(
        '-- allium: 3\n\nenum Framework { maverick | nanobot | openclaw }\n\n'
        'config {\n'
        '    slug: String = "iso-smoke-agent"\n'
        '    display_name: String = "Iso Smoke"\n'
        '    framework: Framework = maverick\n'
        '    visibility: String = "private"\n'
        f'    soul: String = {json.dumps(soul)}\n'
        '}\n',
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(dest / "skills/deploy-agent/assets/allium_to_json.py"),
         str(spec), "--app-env", "dev", "--out-dir", str(out_dir)],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(dest)},
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads((out_dir / "iso-smoke-agent.request_body.json").read_text())
    assert body["slug"] == "iso-smoke-agent"
    assert (out_dir / "iso-smoke-agent.ddb_projection.json").is_file()
    assert (out_dir / "iso-smoke-agent.report.json").is_file()


def test_plugin_manifest_and_discovery_surface():
    """DX-1/DX-10: manifest, marketplace entry, README, and SKILL description
    strings all exist — the install + discovery story is shippable."""
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] and manifest["description"] and manifest["version"]
    market = json.loads((PLUGIN / ".claude-plugin/marketplace.json").read_text())
    assert market["plugins"], "marketplace.json must list the plugin"
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    for required in ("Install", "does NOT", "darwin-arm64", "ALLIUM_ENGINE_UNPINNED"):
        assert required in readme, f"README missing required section/term: {required}"
    skill = (PLUGIN / "skills/deploy-agent/SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---"), "SKILL.md needs frontmatter with description"
    assert "selfcheck" in skill.lower()


def test_vendored_binary_provenance_recorded():
    notice = (PLUGIN / "LICENSES/allium-tools-MIT.txt").read_text(encoding="utf-8")
    assert "MIT License" in notice
    assert "sha256:" in notice and "Source commit:" in notice
