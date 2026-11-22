"""harness/dev_live.py — the live checkout's sync and the generated prod copy (2026-11-20)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import dev_live

PLUGIN = Path(dev_live.PLUGIN)


def _files(root: Path):
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")


def test_the_prod_copy_is_the_plugin_renamed_and_pointed_at_prod(tmp_path):
    target = dev_live.build_prod_copy(tmp_path)
    assert target == tmp_path / "plugins" / "soleon-prod"
    # The copy is the plugin WITHOUT its hooks (they would act twice on every
    # pulled agent); everything else is verbatim.
    assert _files(target) == [f for f in _files(PLUGIN) if not f.startswith("hooks/")]
    assert not (target / "hooks").exists()

    manifest = json.loads((target / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    source = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "soleon-prod"
    assert manifest["version"] == source["version"]
    assert manifest["userConfig"]["server_url"]["default"] == "https://mcp.oppizi.com/mcp"

    mcp = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
    assert {e["url"] for e in mcp["mcpServers"].values()} == {"https://mcp.oppizi.com/mcp"}
    source_mcp = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    assert [e["oauth"] for e in mcp["mcpServers"].values()] == [
        e["oauth"] for e in source_mcp["mcpServers"].values()]

    market = json.loads((tmp_path / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    assert market["name"] == "soleon-prod"
    # A relative source is what makes Claude Code load the copy in place.
    assert market["plugins"] == [{"name": "soleon-prod", "source": "./plugins/soleon-prod",
                                  "description": "Soleon plugin pointed at PROD"}]

    skill = "skills/pull-agent/SKILL.md"
    assert (target / skill).read_bytes() == (PLUGIN / skill).read_bytes()


def test_a_rebuild_drops_files_the_plugin_no_longer_has(tmp_path):
    target = dev_live.build_prod_copy(tmp_path)
    stale = target / "skills" / "retired-skill" / "SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")
    dev_live.build_prod_copy(tmp_path)
    assert not stale.exists()


@pytest.mark.parametrize("state, says", [
    ({"branch": "feat/x", "local_changes": False}, "not 'live'"),
    ({"branch": "live", "local_changes": True}, "local changes"),
])
def test_sync_never_moves_a_checkout_it_could_lose_work_in(monkeypatch, state, says):
    base = {"checkout": "/x", "head": "abc", "version": "1.0.0", "behind_origin_main": 3}
    monkeypatch.setattr(dev_live, "status", lambda: {**base, **state})
    moved = []
    monkeypatch.setattr(dev_live, "git", lambda *a, **k: moved.append(a))
    with pytest.raises(SystemExit) as exc:
        dev_live.sync(None)
    assert says in str(exc.value) and moved == []
