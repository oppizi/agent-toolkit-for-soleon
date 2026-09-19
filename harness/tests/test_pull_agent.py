"""pull_agent.py materialize — the deterministic core of /pull-agent.

Against an inline draft document, tool list and assembled prompt: the files it
writes, the subagent frontmatter, the Local Tool Routing section (every external
tool, the approval rule, workspace tools), the container→local path rewrites,
helper subagents + workflow skills from `config.subagents`, and the small
subcommands (default-model, adopt-etag)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _local_emulation_fixtures import (
    BIN, DOCUMENT, PLUGIN, PULL_ASSETS, SERVER_URL, SLUG, SOUL, make_pulled_dir,
)

sys.path.insert(0, str(PULL_ASSETS))
sys.path.insert(0, str(BIN))
import pull_agent  # noqa: E402
import soleon_agent_document as doc  # noqa: E402


def _run(*args: str, cwd: Path):
    return subprocess.run(
        [sys.executable, str(PULL_ASSETS / "pull_agent.py"), *args],
        capture_output=True, text=True, timeout=60, cwd=str(cwd),
        # HOME → the tmp root, so the DEFAULT user-scope path (~/.claude/agents)
        # is what the tests exercise; root/.claude/agents below IS that path.
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd)},
    )


@pytest.fixture()
def pulled(tmp_path):
    agent_dir = make_pulled_dir(tmp_path)
    proc = _run("materialize", "--slug", SLUG, "--dir", str(agent_dir), "--server-url", SERVER_URL,
                "--model", "sonnet", "--plugin-root", str(PLUGIN), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    return tmp_path, agent_dir, json.loads(proc.stdout)


def _frontmatter(text: str) -> str:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, text[:200]
    return m.group(1)


def test_materialize_writes_the_layout(pulled):
    root, agent_dir, summary = pulled
    assert (agent_dir / "SOUL.md").read_text(encoding="utf-8") == SOUL
    cfg = json.loads((agent_dir / "config.json").read_text(encoding="utf-8"))
    # flat → nested (the platform's own lift) over the deployed config
    assert cfg["agents"]["defaults"]["model"] == DOCUMENT["model"]
    assert cfg["loop"]["effort"] == {"default": "thorough", "ceiling": "exhaustive"}
    assert cfg["loop"]["dimensions"] == {"thinking": "auto"}
    assert cfg["loop"]["tokenBudget"] == 500000
    assert cfg["loop"]["dailyTokenBudget"] == 2000000  # deployed-only key survives
    assert "guardrails" not in cfg  # draft says False → the deployed block is dropped
    assert cfg["schedules"][0]["id"] == "s1"  # platform-only, kept read-only
    assert cfg["promptCaching"] is True and cfg["promptCacheTtl"] == "5m"
    assert cfg["evals"]["standardEvals"][0]["id"] == "terse"
    assert cfg["subagents"]["schemaVersion"] == 1
    # skills
    assert (agent_dir / "skills/cite-sources/SKILL.md").read_text(encoding="utf-8") == "# Cite Sources\n\nCite every claim.\n"
    meta = json.loads((agent_dir / "skills/cite-sources/skill.json").read_text())
    assert meta == {"id": "cite-sources", "name": "Cite Sources", "description": "Always cite", "enabled": True}
    assert (agent_dir / "skills/cite-sources/refs/style.md").read_text() == "APA.\n"
    assert not (agent_dir / "skills/cite-sources/assets/logo.png").exists()  # binaries are not inlined
    assert json.loads((agent_dir / "skills/off-skill/skill.json").read_text())["enabled"] is False
    # evals — one file per standard eval
    assert sorted(p.name for p in (agent_dir / "evals").glob("*.json")) == ["cites.json", "terse.json"]
    assert json.loads((agent_dir / "evals/terse.json").read_text())["expectedOutput"] == "Paris"
    # workspace — extracted safely
    assert (agent_dir / "workspace/memory/MEMORY.md").read_text() == "# Memory\n- likes tea\n"
    assert (agent_dir / "workspace/notes/todo.txt").exists()
    assert not (agent_dir.parent / "escape.txt").exists() and not (agent_dir / "escape.txt").exists()
    assert summary["workspaceFiles"] == 2
    # pull.json
    pull = json.loads((agent_dir / "pull.json").read_text())
    assert pull["slug"] == SLUG and pull["source"] == "draft" and pull["draftEtag"] == "1726560000000"
    assert pull["model"] == "sonnet" and pull["serverUrl"] == SERVER_URL and pull["namespace"] == "soleon_abc"
    assert pull["pulledAt"].endswith("Z")


def test_subagent_frontmatter_and_body(pulled):
    root, agent_dir, summary = pulled
    path = root / ".claude" / "agents" / f"{SLUG}.md"
    assert path.is_file() and summary["subagentFile"] == str(path)
    text = path.read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert f"name: {SLUG}\n" in fm
    assert 'description: "Soleon agent \\"Demo Agent\\" — local emulation (pulled ' in fm
    assert "Use when the user wants to talk to or test Demo Agent." in fm
    assert "model: sonnet\n" in fm
    assert "effort: high\n" in fm  # thorough → high
    # An EMPTY tools list, on purpose: a subagent's `tools:` is resolved against
    # the parent session's pool before its inline servers connect, so naming the
    # inline servers there makes Claude Code refuse the spawn ("would be spawned
    # with zero tools"). Empty = exactly the inline servers' tools, nothing else.
    assert "\ntools: []\n" in fm
    assert "mcp__soleon-workspace__*" not in fm and "mcp__soleon-agent-tools__*" not in fm
    assert "mcpServers:" in fm and "  - soleon-workspace:" in fm and "  - soleon-agent-tools:" in fm
    ws_args = re.search(r"soleon-workspace:\n\s+type: stdio\n\s+command: python3\n\s+args: (\[.*?\])\n", fm)
    assert ws_args, fm
    ws = json.loads(ws_args.group(1))
    assert ws[0] == str(PLUGIN / "bin" / "soleon_workspace_mcp.py")
    assert ws[1] == str(agent_dir.resolve() / "workspace")
    # Claude Code's project data folder is a READ-ONLY root, so the agent can
    # read the overflow copy of a large tool result (…/tool-results/*.txt)
    # that Claude Code writes there instead of returning it inline.
    readable = [ws[i + 1] for i, a in enumerate(ws) if a == "--readable"]
    assert readable == [str(agent_dir.resolve()),
                        str(root / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9-]", "-", str(root.resolve())))]
    assert "**Large tool results**" in text and "tool-results/" in text
    tools_args = re.search(r"soleon-agent-tools:\n\s+type: stdio\n\s+command: python3\n\s+args: (\[.*?\])(?:\n|$)", fm)
    assert tools_args, fm
    ta = json.loads(tools_args.group(1))
    assert ta == [str(PLUGIN / "bin" / "soleon_agent_tools_mcp.py"), "--slug", SLUG,
                  "--tools", str(agent_dir.resolve() / "tools.json"), "--server-url", SERVER_URL]
    body = text[len(fm) + 8:]
    # the platform prompt survives byte-identical except for the path rewrites
    assert SOUL in body
    assert "Follow the personality and instructions in SOUL.md below." in body
    assert "/mnt/workspace" not in body and "/app/agent-config" not in body
    assert f"Your workspace is at: {agent_dir.resolve() / 'workspace'}" in body
    assert f"{agent_dir.resolve()}/skills/cite-sources/SKILL.md" in body
    assert f"Everything under {agent_dir.resolve()} is readable" in body
    assert "/mnt/workspace -> " in " ".join(summary["pathRewrites"])


def test_local_tool_routing_section(pulled):
    root, agent_dir, summary = pulled
    body = (root / ".claude" / "agents" / f"{SLUG}.md").read_text(encoding="utf-8")
    assert body.count("## Local Tool Routing") == 1
    routing = body.split("## Local Tool Routing", 1)[1]
    ext_line = next(l for l in routing.splitlines() if "**External tools**" in l)
    for name in ("web_search", "custom_echo-server_read", "custom_echo-server_write"):
        assert f"`{name}`" in ext_line
    assert "`read_file`" not in ext_line
    ws_line = next(l for l in routing.splitlines() if "**Workspace tools**" in l)
    assert "`read_file`" in ws_line and "`write_file`" in ws_line and "soleon-workspace" in ws_line
    approval_line = next(l for l in routing.splitlines() if "**Approval rule**" in l)
    assert "`custom_echo-server_write`" in approval_line and "`approved: true`" in approval_line
    assert "`custom_echo-server_read`" not in approval_line
    assert "BEFORE calling" in approval_line
    assert "`attach_file`" in routing  # in the prompt's toolNames but not routable locally
    assert "channels, budgets, schedules, guardrails, online eval sampling" in routing
    assert summary["approvalGated"] == ["custom_echo-server_write"]
    assert summary["externalTools"] == ["custom_echo-server_read", "custom_echo-server_write", "web_search"]


def test_helper_subagents_and_workflows(pulled):
    root, agent_dir, summary = pulled
    agents = root / ".claude" / "agents"
    res = agents / f"{SLUG}--subagent_res1.md"
    wri = agents / f"{SLUG}--subagent_wri1.md"
    assert res.is_file() and wri.is_file()
    assert not (agents / f"{SLUG}--subagent_off1.md").exists()  # disabled → no file
    fm = _frontmatter(res.read_text(encoding="utf-8"))
    assert f"name: {SLUG}--subagent_res1\n" in fm
    assert "model: sonnet\n" in fm  # inherit → the chosen model
    assert "effort: low\n" in fm  # its own band: swift → low
    # The helper's tool SUBSET rides the inline server's `--only`, never the
    # `tools:` line (which cannot see inline servers — see the main-agent test).
    assert "\ntools: []\n" in fm
    only = re.search(r"soleon-agent-tools:\n\s+type: stdio\n\s+command: python3\n\s+args: (\[.*?\])(?:\n|$)", fm)
    assert only, fm
    h_args = json.loads(only.group(1))
    assert h_args[-2] == "--only"
    assert set(h_args[-1].split(",")) == {"custom_echo-server_read", "web_search"}  # sys_web_prompt → the web_ family
    assert "custom_echo-server_write" not in h_args[-1]
    body = res.read_text(encoding="utf-8")
    assert "Research carefully." in body and "find a source" in body and "write copy" in body
    fm_w = _frontmatter(wri.read_text(encoding="utf-8"))
    assert "model: opus\n" in fm_w  # its own opus model id → opus
    assert "effort: high\n" in fm_w  # no own band → the agent's
    assert "\ntools: []\n" in fm_w
    # no external tools → the workspace server only; the tool server is omitted
    assert "soleon-agent-tools:" not in fm_w and "  - soleon-workspace:" in fm_w
    assert "Response contract" in wri.read_text(encoding="utf-8") and "`draft`" in wri.read_text(encoding="utf-8")
    # workflows
    mgr = agent_dir / "workflows/workflow_mgr1/SKILL.md"
    peer = agent_dir / "workflows/workflow_peer1/SKILL.md"
    assert mgr.is_file() and peer.is_file()
    m = mgr.read_text(encoding="utf-8")
    assert m.startswith("---\nname: demo-agent-workflow_mgr1\n")
    assert "**Assign.**" in m and "**Review.**" in m and "**Next decision.**" in m
    assert "at most 2 concrete assignments" in m and "at most 3 assignment rounds" in m
    assert "Every question answered." in m
    assert f"`{SLUG}--subagent_res1`" in m and f"`{SLUG}--subagent_wri1`" in m
    p = peer.read_text(encoding="utf-8")
    assert "For up to 2 rounds" in p and "Agreement." in p and "reviewed draft" in p
    # the main prompt points at them
    main = (root / ".claude" / "agents" / f"{SLUG}.md").read_text(encoding="utf-8")
    assert f"`subagent_res1` → local subagent `{SLUG}--subagent_res1`" in main
    assert "`workflow_mgr1` → workflow skill" in main and "manager mode" in main
    assert set(summary["helpers"]) == {str(res), str(wri)}


def test_not_emulated_summary_lists_the_platform_only_settings(pulled):
    _, _, summary = pulled
    ne = summary["notEmulated"]
    assert set(ne) == {"channels", "budgets", "schedules", "guardrails", "onlineEvalSampling"}
    assert ne["schedules"] == 1
    assert ne["budgets"] == {"tokenBudget": 500000, "dailyTokenBudget": 2000000}
    assert ne["guardrails"] is False
    assert ne["onlineEvalSampling"]["scoring"] == {"frequency": "every_10"}


def test_materialize_without_draft_records_deployed_source(tmp_path):
    agent_dir = make_pulled_dir(tmp_path, has_draft=False, with_zip=False)
    proc = _run("materialize", "--slug", SLUG, "--dir", str(agent_dir), "--server-url", SERVER_URL,
                "--model", "haiku", "--plugin-root", str(PLUGIN), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    pull = json.loads((agent_dir / "pull.json").read_text())
    assert pull["source"] == "deployed" and pull["draftEtag"] is None
    assert (agent_dir / "workspace" / "memory").is_dir()  # empty snapshot still yields a workspace root


def test_materialize_refuses_a_pending_tools_envelope(tmp_path):
    agent_dir = make_pulled_dir(tmp_path)
    (agent_dir / "tools.json").write_text(json.dumps({"state": "pending", "call_id": "lc_" + "c" * 24}))
    proc = _run("materialize", "--slug", SLUG, "--dir", str(agent_dir), "--server-url", SERVER_URL,
                "--model", "sonnet", "--plugin-root", str(PLUGIN), cwd=tmp_path)
    assert proc.returncode != 0
    assert "pending" in proc.stderr and "get_agent_tool_result" in proc.stderr


@pytest.mark.parametrize("platform_model, suggested, warns", [
    ("us.anthropic.claude-opus-4-6-v1", "opus", False),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "sonnet", False),
    ("global.anthropic.claude-haiku-4-5-20251001-v1:0", "haiku", False),
    ("moonshot.kimi-k2-thinking", None, True),
    ("us.amazon.nova-pro-v1:0", None, True),
    ("", None, True),
])
def test_default_model_mapping(platform_model, suggested, warns):
    alias, warning = pull_agent.suggest_local_model(platform_model)
    assert alias == suggested
    assert bool(warning) is warns
    if warns and platform_model:
        assert "no local equivalent" in warning


def test_default_model_subcommand_reads_the_prompt_model(tmp_path):
    agent_dir = make_pulled_dir(tmp_path)
    proc = _run("default-model", "--dir", str(agent_dir), "--plugin-root", str(PLUGIN), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out == {"platformModel": DOCUMENT["model"], "suggested": "sonnet", "warning": None,
                   "choices": ["opus", "sonnet", "haiku"]}


def test_adopt_etag_takes_the_conflicting_drafts_etag(pulled):
    root, agent_dir, _ = pulled
    (agent_dir / ".pull" / "conflict.json").write_text(json.dumps({"agent": {}, "draftEtag": "999"}))
    proc = _run("adopt-etag", "--dir", str(agent_dir), "--plugin-root", str(PLUGIN), cwd=root)
    assert proc.returncode == 0, proc.stderr
    assert json.loads((agent_dir / "pull.json").read_text())["draftEtag"] == "999"
    assert not (agent_dir / ".pull" / "conflict.json").exists()


def test_effort_mapping_covers_the_platform_ladder():
    for stop, effort in (("swift", "low"), ("balanced", "medium"), ("thorough", "high"), ("exhaustive", "xhigh")):
        assert pull_agent.effort_of({"loop": {"effort": {"default": stop, "ceiling": "exhaustive"}}}) == effort
    assert pull_agent.effort_of({}) is None


def test_config_roundtrip_flat_to_nested_to_flat():
    """The two directions in soleon_agent_document agree on every behavioural field."""
    nested = doc.flat_document_to_config(DOCUMENT, {})
    flat_again = doc.config_to_flat_changes(nested)
    for key in ("model", "computeMode", "access", "tools", "promptCaching", "promptCacheTtl",
                "effort", "loopDimensions", "tokenBudget", "evals", "subagents", "userSchedulesEnabled"):
        assert flat_again[key] == DOCUMENT[key], key
    assert flat_again["guardrails"] is False
    assert "name" not in flat_again and "soul" not in flat_again and "skills" not in flat_again


def test_subagent_lands_in_user_scope_and_a_stale_project_copy_is_removed(tmp_path):
    """User scope (~/.claude/agents) on purpose: a PROJECT agent file starts its
    inline MCP servers only in a trusted folder (VS Code does not always ask →
    the agent spawned with no tools, 2026-09-18); user-scope files skip the
    trust check and hot-load. A copy left by an older pull in the project's
    .claude/agents/ would make Claude Code see the agent twice — it is removed."""
    agent_dir = make_pulled_dir(tmp_path)
    stale = tmp_path / ".claude" / "agents" / f"{SLUG}.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.run(
        [sys.executable, str(PULL_ASSETS / "pull_agent.py"), "materialize", "--slug", SLUG, "--dir", str(agent_dir),
         "--server-url", SERVER_URL, "--model", "sonnet", "--plugin-root", str(PLUGIN)],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["agentsDir"] == str(home / ".claude" / "agents")
    assert summary["agentsDirCreated"] is True  # did not exist before ⇒ the report says restart
    assert (home / ".claude" / "agents" / f"{SLUG}.md").is_file()
    assert summary["subagentFile"] == str(home / ".claude" / "agents" / f"{SLUG}.md")
    assert not stale.exists()
    assert summary["projectRoot"] == str(tmp_path)


def test_agents_dir_created_is_false_when_it_already_existed(tmp_path):
    agent_dir = make_pulled_dir(tmp_path)
    (tmp_path / ".claude" / "agents").mkdir(parents=True)  # HOME == tmp_path in _run
    proc = _run("materialize", "--slug", SLUG, "--dir", str(agent_dir), "--server-url", SERVER_URL,
                "--model", "sonnet", "--plugin-root", str(PLUGIN), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["agentsDir"] == str(tmp_path / ".claude" / "agents")
    assert summary["agentsDirCreated"] is False


def test_permission_allow_rules_merged_into_project_local_settings(pulled):
    """Claude Code would otherwise prompt before every tool call the subagent
    makes; the two server-level allow rules are merged into the project's
    settings.local.json (never replacing what is there)."""
    root, agent_dir, summary = pulled
    path = root / ".claude" / "settings.local.json"
    assert summary["permissions"]["settingsFile"] == str(path)
    assert summary["permissions"]["permissionRulesAdded"] == ["mcp__soleon-workspace", "mcp__soleon-agent-tools"]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"permissions": {"allow": ["mcp__soleon-workspace", "mcp__soleon-agent-tools"]}}
    # idempotent, and existing content survives
    path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls *)", "mcp__soleon-agent-tools"], "deny": ["WebFetch"]},
                                "other": 1}))
    proc = _run("materialize", "--slug", SLUG, "--dir", str(agent_dir), "--server-url", SERVER_URL,
                "--model", "sonnet", "--plugin-root", str(PLUGIN), cwd=root)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["permissions"]["permissionRulesAdded"] == ["mcp__soleon-workspace"]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["other"] == 1 and data["permissions"]["deny"] == ["WebFetch"]
    assert data["permissions"]["allow"] == ["Bash(ls *)", "mcp__soleon-agent-tools", "mcp__soleon-workspace"]


def test_agents_dir_override(tmp_path):
    agent_dir = make_pulled_dir(tmp_path)
    custom = tmp_path / "elsewhere"
    proc = _run("materialize", "--slug", SLUG, "--dir", str(agent_dir), "--server-url", SERVER_URL,
                "--model", "sonnet", "--plugin-root", str(PLUGIN), "--agents-dir", str(custom), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (custom / f"{SLUG}.md").is_file()
