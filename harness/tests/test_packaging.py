"""Packaging boundary — the ship boundary must be real, textually AND
behaviorally.

The ship boundary is now THREE boundaries: the repo publishes one plugin per role
bundle (`soleon-observer` / `soleon-builder` / `soleon-admin`). The telemetry ban
and the manifest/discovery surface apply to all three; the Python-specific checks
apply to **builder** only, because it is the sole bundle carrying the converter,
the engine, and the vendored allium binary.

1. No plugin references harness/, samples/, preflight/, runs, or transcripts (bet
   telemetry never ships).
2. Isolation smoke (F3): the builder plugin copied ALONE to a temp dir still
   converts a spec end-to-end — proving no hidden dependency on the bet tree.
3. Converter + engine are stdlib-only (no third-party imports).
"""
import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BET_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = BET_ROOT / "plugins"
# Every shipped bundle. Sorted so parametrized ids are stable.
PLUGINS = sorted(p for p in PLUGINS_DIR.iterdir() if p.is_dir())
BUNDLE_NAMES = [p.name for p in PLUGINS]
# The one bundle that ships Python + the vendored binary.
PLUGIN = PLUGINS_DIR / "builder"

FORBIDDEN_REFS = ("harness/", "samples/", "preflight/", "runs.jsonl", "transcripts/")

STDLIB_OK = {
    "__future__", "annotations", "argparse", "ast", "json", "os", "platform",
    "re", "shutil", "subprocess", "sys", "pathlib", "datetime", "engine", "typing",
}


@pytest.mark.parametrize("plugin", PLUGINS, ids=BUNDLE_NAMES)
def test_no_telemetry_references_in_plugin(plugin):
    offenders = []
    for path in plugin.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".md", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for ref in FORBIDDEN_REFS:
            for line in text.splitlines():
                if ref in line and "maintainers:" not in line.lower():
                    offenders.append(f"{path.relative_to(plugin)}: {ref}")
    assert not offenders, f"{plugin.name} references bet telemetry: {offenders}"


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


@pytest.mark.parametrize("plugin", PLUGINS, ids=BUNDLE_NAMES)
def test_plugin_manifest_and_discovery_surface(plugin):
    """DX-1/DX-10: manifest + README exist for EVERY bundle — the install and
    discovery story has to be shippable for all three, not just the one with skills."""
    manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] and manifest["description"] and manifest["version"]
    assert manifest["name"] == f"soleon-{plugin.name}", (
        f"{plugin.name}'s manifest name {manifest['name']!r} must match its directory"
    )
    readme = (plugin / "README.md").read_text(encoding="utf-8")
    required = ["Install", "does NOT"]
    if plugin.name == "builder":
        # Only builder ships the vendored engine, so only its README documents it.
        required += ["darwin-arm64", "ALLIUM_ENGINE_UNPINNED"]
    for term in required:
        assert term in readme, f"{plugin.name} README missing required term: {term}"


def test_root_marketplace_lists_every_bundle():
    """The ROOT manifest is the distribution surface — `/plugin marketplace add
    oppizi/agent-toolkit-for-soleon` resolves it.

    The nested `plugin/.claude-plugin/marketplace.json` was deleted: it declared the
    SAME marketplace name as this one, so in a three-plugin repo it would advertise
    one of three plugins under a colliding name, and its `./` source could not
    address sibling bundles anyway.
    """
    market = json.loads((BET_ROOT / ".claude-plugin/marketplace.json").read_text())
    entries = {e["name"]: e for e in market["plugins"]}
    assert set(entries) == {f"soleon-{n}" for n in BUNDLE_NAMES}, (
        f"root marketplace lists {sorted(entries)} but the repo ships {BUNDLE_NAMES}"
    )
    for name, entry in entries.items():
        source = (BET_ROOT / entry["source"]).resolve()
        assert source.is_dir(), f"{name} source {entry['source']} does not resolve"
        assert (source / ".claude-plugin/plugin.json").is_file(), (
            f"{name} source {entry['source']} has no plugin manifest"
        )
        assert entry["description"].strip(), f"{name} needs a description"
    assert not (BET_ROOT / "plugins/builder/.claude-plugin/marketplace.json").exists(), (
        "the nested marketplace manifest must stay deleted — one marketplace, one name"
    )
    skill = (PLUGIN / "skills/deploy-agent/SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---"), "SKILL.md needs frontmatter with description"
    assert "selfcheck" in skill.lower()


def test_vendored_binary_provenance_recorded():
    notice = (PLUGIN / "LICENSES/allium-tools-MIT.txt").read_text(encoding="utf-8")
    assert "MIT License" in notice
    assert "sha256:" in notice and "Source commit:" in notice
