"""Packaging boundary — the ship boundary must be real, textually AND
behaviorally.

The repo publishes ONE plugin, `soleon` (`plugins/soleon/`). Everything outside
it is harness or telemetry and never ships.

1. The plugin references no harness/, samples/, preflight/, runs, or transcripts
   (bet telemetry never ships).
2. Isolation smoke (F3): the plugin copied ALONE to a temp dir still converts a
   spec end-to-end — proving no hidden dependency on the bet tree.
3. Every shipped .py is stdlib-only and parses with the Python 3.9 grammar.
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
PLUGIN = PLUGINS_DIR / "soleon"

# What the plugin is expected to ship. Asserted as an exact set so a LOST skill
# fails as loudly as an unexpected one — discovery alone would silently pass a
# plugin whose skill vanished.
EXPECTED_SKILLS = {
    "attach-tool-server",
    "browse-knowledge",
    "change-a-setting",
    "compare-environments",
    "connect-channel",
    "cost-check",
    "create-agent",
    "deploy-agent",
    "deploy-to-dev",
    "edit-identity",
    "edit-skill",
    "failures-check",
    "find-an-agent",
    "pause-agent",
    "promote",
    "pull-agent",
    "read-a-conversation",
    "read-eval-results",
    "read-identity",
    "read-skills",
    "release-check",
    "review-draft",
    "roll-back",
    "run-an-eval",
    "run-local-eval",
    "see-agent-tools",
    "start-an-agent",
    "test-draft-in-chat",
    "tool-switches",
    "usage-check",
    "write-an-eval",
    "write-evals",
    "write-skill-scripts",
}

FORBIDDEN_REFS = ("harness/", "samples/", "preflight/", "runs.jsonl", "transcripts/")

STDLIB_OK = {
    "__future__", "annotations", "argparse", "ast", "json", "os", "platform",
    "re", "shutil", "subprocess", "sys", "pathlib", "datetime", "typing",
    # added with the local-emulation tooling (bin/*.py + skills/pull-agent)
    "urllib", "http", "zipfile", "hashlib", "time", "threading", "io", "socket",
    "select", "stat", "tempfile", "uuid", "base64", "mimetypes", "fnmatch",
    "dataclasses", "functools", "textwrap", "difflib",
    # the local message budget (bin/soleon_message_budget.py)
    "contextlib", "fcntl", "shlex",
    # write-skill-scripts: the local runner, the script template and its example fakes
    "asyncio", "importlib", "csv",
    # the macOS Keychain read in bin/soleon_mcp_client.py
    "getpass",
}
# The plugin's OWN modules that its scripts import from a sibling path (they ship
# together, so they are not third-party): the deploy-agent engine seam plus every
# bin/ and skill-asset module the stdio servers, the hooks and pull_agent.py share.
# DERIVED from what the plugin actually ships — a hand-written list went stale the
# moment a bin/ server imported a new sibling (`soleon_turn_hooks` importing
# `soleon_agent_tools_mcp` is precisely the "sibling plugin module" the assertion
# below says it allows, and it was reported as a third-party dependency instead).
def _plugin_local_modules():
    names = {p.stem for p in (PLUGIN / "bin").glob("*.py")}
    names |= {p.stem for p in (PLUGIN / "skills").glob("*/assets/*.py")}
    assert names, "no plugin Python found — layout changed?"
    return names | {"engine"}


PLUGIN_LOCAL_MODULES = _plugin_local_modules()


def _plugin_python_files():
    files = list((PLUGIN / "skills").glob("*/assets/*.py"))
    files += list((PLUGIN / "bin").glob("*.py"))
    assert files, "no plugin Python found — layout changed?"
    return sorted(files)


def _telemetry_offenders(root):
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".md", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for ref in FORBIDDEN_REFS:
            for line in text.splitlines():
                if ref in line and "maintainers:" not in line.lower():
                    offenders.append(f"{path.relative_to(root)}: {ref}")
    return offenders


def test_no_telemetry_references_in_plugin():
    offenders = _telemetry_offenders(PLUGIN)
    assert not offenders, f"the plugin references bet telemetry: {offenders}"


def test_plugins_dir_holds_only_the_plugin_and_its_vendored_pin():
    """One plugin, no catalogue: a stray directory next to it would be content that
    looks shipped but is not (the marketplace installs `plugins/soleon/` only)."""
    assert sorted(p.name for p in PLUGINS_DIR.iterdir()) == ["scope_pin.json", "soleon"]


@pytest.mark.parametrize("path", _plugin_python_files(), ids=lambda p: str(p.relative_to(PLUGIN)))
def test_plugin_python_is_stdlib_only(path):
    """Every shipped .py (deploy-agent + pull-agent assets, bin/ servers and hook)
    imports only the standard library or a sibling plugin module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module.split(".")[0]]
        for mod in mods:
            assert mod in STDLIB_OK or mod in PLUGIN_LOCAL_MODULES, (
                f"{path.relative_to(PLUGIN)} imports non-stdlib {mod!r}"
            )


def test_plugin_python_is_python39_compatible():
    """The README promises Python 3.9+: no `match`, no `X | Y` unions evaluated at
    runtime, no parenthesized context managers. Checked by parsing with the 3.9
    grammar via `ast.parse(feature_version=(3, 9))`."""
    for path in _plugin_python_files():
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))


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
    """DX-1/DX-10: manifest + README carry the install and discovery story."""
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] and manifest["description"] and manifest["version"]
    assert manifest["name"] == PLUGIN.name, (
        f"the manifest name {manifest['name']!r} must match its directory"
    )
    assert "hooks" not in manifest, (
        "hooks/hooks.json is auto-discovered; listing it in the manifest too "
        "registers every hook twice"
    )
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    for term in ("Install", "does NOT", "darwin-arm64", "ALLIUM_ENGINE_UNPINNED"):
        assert term in readme, f"plugin README missing required term: {term}"


def test_root_marketplace_lists_the_plugin():
    """The ROOT manifest is the distribution surface — `/plugin marketplace add
    oppizi/agent-toolkit-for-soleon` resolves it.

    A nested `plugins/*/.claude-plugin/marketplace.json` must stay deleted: it would
    declare the SAME marketplace name as this one.
    """
    market = json.loads((BET_ROOT / ".claude-plugin/marketplace.json").read_text())
    (entry,) = market["plugins"]
    assert entry["name"] == PLUGIN.name
    assert (BET_ROOT / entry["source"]).resolve() == PLUGIN.resolve()
    assert entry["description"].strip(), "the marketplace entry needs a description"
    assert not list(PLUGINS_DIR.glob("*/.claude-plugin/marketplace.json")), (
        "the nested marketplace manifest must stay deleted — one marketplace, one name"
    )
    skill = (PLUGIN / "skills/deploy-agent/SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---"), "SKILL.md needs frontmatter with description"
    assert "selfcheck" in skill.lower()


def _assert_skill_frontmatter(path):
    """Claude Code discovers a skill by its frontmatter ``name`` + ``description``.
    A skill missing either installs fine and then never triggers, and nothing else
    in this suite would notice."""
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(BET_ROOT)
    assert text.startswith("---\n"), f"{rel} has no frontmatter block"
    front = text.split("---\n", 2)[1]
    for key in ("name:", "description:"):
        assert key in front, f"{rel} frontmatter is missing `{key}`"
    declared = next(
        ln.split(":", 1)[1].strip()
        for ln in front.splitlines()
        if ln.startswith("name:")
    )
    assert declared == path.parent.name, (
        f"{rel} declares name `{declared}` but lives in `{path.parent.name}/` — "
        "Claude Code resolves the directory, so the two must match"
    )


def test_every_shipped_skill_has_frontmatter():
    skills = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
    found = {p.parent.name for p in skills}
    assert found == EXPECTED_SKILLS, (
        f"the plugin ships {sorted(found)}, expected {sorted(EXPECTED_SKILLS)} — "
        "did a skill land or vanish without updating EXPECTED_SKILLS?"
    )
    for path in skills:
        _assert_skill_frontmatter(path)


def test_vendored_binary_provenance_recorded():
    notice = (PLUGIN / "LICENSES/allium-tools-MIT.txt").read_text(encoding="utf-8")
    assert "MIT License" in notice
    assert "sha256:" in notice and "Source commit:" in notice
