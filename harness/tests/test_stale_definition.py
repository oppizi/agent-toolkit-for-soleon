"""A tool server started from a subagent definition that a newer pull replaced refuses every call.

Claude Code keeps running the OLD definition of ~/.claude/agents/<slug>.md for a few minutes after a pull
rewrites it, and that definition starts the old pull's servers on the old files (2026-11-17)."""
from __future__ import annotations

import json
import sys

from _local_emulation_fixtures import BIN

sys.path.insert(0, str(BIN))
import soleon_agent_tools_mcp as shim  # noqa: E402


def _agent_file(agents_dir, slug, tools_path):
    """The frontmatter shape materialize writes: the args list is JSON, so Windows paths are escaped."""
    args = json.dumps(["/plugin/bin/soleon_agent_tools_mcp.py", "--slug", slug, "--tools", str(tools_path),
                       "--server-url", "https://mcp.example/mcp"])
    (agents_dir / (slug + ".md")).write_text(
        "---\nname: {}\nmcpServers:\n  soleon-agent-tools:\n    type: stdio\n    args: {}\n---\n# Agent\n".format(
            slug, args), encoding="utf-8")


def test_same_tools_path_is_current(tmp_path):
    tools = tmp_path / "proj" / ".soleon" / "agents" / "fb" / "tools.json"
    _agent_file(tmp_path, "fb", tools)
    assert shim.replaced_definition("fb", str(tools), agents_dir=str(tmp_path)) is None


def test_windows_path_with_backslashes_matches(tmp_path):
    tools = r"C:\Soleon\agent-infra\.soleon\agents\fb\tools.json"
    _agent_file(tmp_path, "fb", tools)
    assert shim.replaced_definition("fb", tools, agents_dir=str(tmp_path)) is None


def test_a_newer_pull_elsewhere_is_reported(tmp_path):
    old = tmp_path / "old-project" / ".soleon" / "agents" / "fb" / "tools.json"
    new = tmp_path / "new-project" / ".soleon" / "agents" / "fb" / "tools.json"
    _agent_file(tmp_path, "fb", new)
    current = shim.replaced_definition("fb", str(old), agents_dir=str(tmp_path))
    assert current == str(new)


def test_no_file_or_unreadable_file_never_blocks(tmp_path):
    assert shim.replaced_definition("fb", "/x/tools.json", agents_dir=str(tmp_path)) is None
    (tmp_path / "fb.md").write_text("---\nname: fb\n---\nno servers here\n", encoding="utf-8")
    assert shim.replaced_definition("fb", "/x/tools.json", agents_dir=str(tmp_path)) is None


def test_a_stale_server_refuses_every_call_and_runs_nothing():
    class NoClient:
        def call_tool(self, *a, **k):
            raise AssertionError("a stale server must not reach the platform")

    tools = [{"name": "web_search", "description": "d", "inputSchema": {"type": "object", "properties": {}}}]
    note = shim.stale_definition_note("fb", "/new/agents/fb/tools.json")
    server = shim.AgentToolsServer("fb", tools, NoClient(), stale_note=note)
    out = server.call("web_search", {"query": "x"})
    assert out["isError"] is True
    text = out["content"][0]["text"]
    assert "Nothing was run" in text and "start the agent again" in text and "/new/agents/fb" in text
    assert server.call("read_result_page", {"id": "r1", "offset": 0})["isError"] is True
