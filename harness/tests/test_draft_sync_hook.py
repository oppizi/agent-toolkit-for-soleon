"""bin/soleon_draft_sync.py — the PostToolUse save hook.

Hook JSON on stdin → the right `patch_agent_draft` changes for SOUL.md /
config.json / a skill file / an eval file; the etag round trip; conflict → exit 2
+ stderr + .pull/conflict.json; non-agent paths → exit 0 with NO network."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from _local_emulation_fixtures import BIN, DOCUMENT, PLUGIN, PULL_ASSETS, SERVER_URL, SLUG, make_pulled_dir

sys.path.insert(0, str(BIN))
import soleon_draft_sync as hook  # noqa: E402
import soleon_mcp_client as client_mod  # noqa: E402


class FakeClient:
    """Records patch/sync calls; answers with a configurable envelope."""

    instances = []

    def __init__(self, server_url):
        self.server_url = server_url
        self.calls = []
        self.patch_answer = {"status": 200, "body": {"patched": True, "changed_keys": [], "deleted_keys": [],
                                                      "previous_draft_etag": "1726560000000", "draft_etag": "1726560099999",
                                                      "next_action": "Review with get_agent_draft"}}
        FakeClient.instances.append(self)

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "patch_agent_draft":
            answer = self.patch_answer
            if isinstance(answer, Exception):
                raise answer
            return json.loads(json.dumps(answer))
        if name == "sync_draft_test_chat":
            return {"status": 200, "body": {"synced": True, "hasDraft": True}}
        raise AssertionError("unexpected tool " + name)


def _materialize(tmp_path) -> Path:
    agent_dir = make_pulled_dir(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(PULL_ASSETS / "pull_agent.py"), "materialize", "--slug", SLUG, "--dir", str(agent_dir),
         "--server-url", SERVER_URL, "--model", "sonnet", "--plugin-root", str(PLUGIN)],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return agent_dir


def _run_hook(path: Path, capsys, factory=FakeClient, tool="Write"):
    FakeClient.instances = []
    payload = {"hook_event_name": "PostToolUse", "tool_name": tool,
               "tool_input": {"file_path": str(path), "content": "x"}, "tool_response": {}}
    code = hook.main(io.StringIO(json.dumps(payload)), client_factory=factory)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def agent_dir(tmp_path):
    return _materialize(tmp_path)


def test_soul_save_patches_soul_with_the_pulled_etag(agent_dir, capsys):
    (agent_dir / "SOUL.md").write_text("# Demo\n\nBe terse.\n", encoding="utf-8")
    code, out, err = _run_hook(agent_dir / "SOUL.md", capsys)
    assert code == 0, err
    (client,) = FakeClient.instances
    assert client.server_url == SERVER_URL
    names = [c[0] for c in client.calls]
    assert names == ["patch_agent_draft", "sync_draft_test_chat"]
    _, args = client.calls[0]
    assert args == {"slug": SLUG, "app_env": "dev", "changes": {"soul": "# Demo\n\nBe terse.\n"},
                    "expected_updated_at": 1726560000000}
    assert client.calls[1][1] == {"slug": SLUG, "app_env": "dev"}
    msg = json.loads(out.strip())
    assert msg["systemMessage"].startswith("Soleon draft updated from SOUL.md (etag 1726560099999")
    pull = json.loads((agent_dir / "pull.json").read_text())
    assert pull["draftEtag"] == "1726560099999" and pull["lastSyncedFile"] == "SOUL.md"


def test_config_save_maps_nested_config_onto_the_flat_document(agent_dir, capsys):
    cfg = json.loads((agent_dir / "config.json").read_text())
    cfg["agents"]["defaults"]["model"] = "us.anthropic.claude-opus-4-6-v1"
    cfg["loop"]["effort"] = {"default": "swift", "ceiling": "balanced"}
    cfg["promptCaching"] = False
    cfg["guardrails"] = {"piiEnabled": True}
    cfg["schedules"].append({"id": "s2", "cron": "* * * * *", "prompt": "never synced"})
    (agent_dir / "config.json").write_text(json.dumps(cfg))
    code, out, err = _run_hook(agent_dir / "config.json", capsys, tool="Edit")
    assert code == 0, err
    changes = FakeClient.instances[0].calls[0][1]["changes"]
    assert changes["model"] == "us.anthropic.claude-opus-4-6-v1"
    assert changes["effort"] == {"default": "swift", "ceiling": "balanced"}
    assert changes["promptCaching"] is False
    assert changes["guardrails"] == {"piiEnabled": True}
    assert changes["tools"] == DOCUMENT["tools"]
    assert changes["loopDimensions"] == {"thinking": "auto"}
    assert changes["evals"]["standardEvals"][0]["id"] == "terse"
    assert changes["subagents"]["schemaVersion"] == 1
    assert "schedules" not in changes and "automations" not in changes  # platform-only (D13)
    assert "soul" not in changes and "skills" not in changes and "name" not in changes


def test_skill_save_rebuilds_the_whole_skills_list(agent_dir, capsys):
    skill_md = agent_dir / "skills/cite-sources/SKILL.md"
    skill_md.write_text("# Cite Sources\n\nCite every claim, with a URL.\n", encoding="utf-8")
    (agent_dir / "skills/cite-sources/refs/style.md").write_text("Chicago.\n")
    (agent_dir / "skills/new-skill").mkdir()
    (agent_dir / "skills/new-skill/SKILL.md").write_text("# New\n")
    code, out, err = _run_hook(skill_md, capsys)
    assert code == 0, err
    changes = FakeClient.instances[0].calls[0][1]["changes"]
    assert list(changes) == ["skills"]
    skills = {s["id"]: s for s in changes["skills"]}
    assert [s["id"] for s in changes["skills"]] == ["cite-sources", "off-skill", "new-skill"]  # pulled order, new last
    cite = skills["cite-sources"]
    assert cite["content"] == "# Cite Sources\n\nCite every claim, with a URL.\n"
    assert cite["name"] == "Cite Sources" and cite["description"] == "Always cite" and cite["enabled"] is True
    files = {f["path"]: f for f in cite["files"]}
    assert files["refs/style.md"] == {"path": "refs/style.md", "enabled": True, "content": "Chicago.\n"}
    assert files["assets/logo.png"] == {"path": "assets/logo.png", "enabled": True, "size": 1234, "mime": "image/png"}  # binary round-trips
    assert skills["off-skill"]["enabled"] is False
    assert skills["new-skill"] == {"id": "new-skill", "name": "new-skill", "description": "", "content": "# New\n", "enabled": True}


def test_skill_meta_edit_and_deleted_skill_dir(agent_dir, capsys):
    import shutil
    meta_path = agent_dir / "skills/cite-sources/skill.json"
    meta = json.loads(meta_path.read_text())
    meta["enabled"] = False
    meta["name"] = "Cite!"
    meta_path.write_text(json.dumps(meta))
    shutil.rmtree(agent_dir / "skills/off-skill")
    code, out, err = _run_hook(meta_path, capsys)
    assert code == 0, err
    skills = FakeClient.instances[0].calls[0][1]["changes"]["skills"]
    assert [s["id"] for s in skills] == ["cite-sources"]
    assert skills[0]["enabled"] is False and skills[0]["name"] == "Cite!"


def test_eval_save_rebuilds_the_evals_block(agent_dir, capsys):
    ev = json.loads((agent_dir / "evals/terse.json").read_text())
    ev["expectedOutput"] = "Paris, France"
    (agent_dir / "evals/terse.json").write_text(json.dumps(ev))
    (agent_dir / "evals/third.json").write_text(json.dumps({"id": "third", "name": "Third", "inputs": [], "evaluationCriteria": "x"}))
    (agent_dir / "evals/results").mkdir()
    (agent_dir / "evals/results/20260101T000000Z-terse.json").write_text(json.dumps({"evalId": "terse", "score": 80}))
    code, out, err = _run_hook(agent_dir / "evals/terse.json", capsys)
    assert code == 0, err
    changes = FakeClient.instances[0].calls[0][1]["changes"]
    assert list(changes) == ["evals"]
    evals = changes["evals"]
    assert evals["sessionCriteria"] == [] and evals["requestCriteria"] == []
    assert [e["id"] for e in evals["standardEvals"]] == ["terse", "cites", "third"]
    assert evals["standardEvals"][0]["expectedOutput"] == "Paris, France"
    assert "scoreThreshold" not in json.dumps(evals) and "scoringMode" not in json.dumps(evals)


def test_results_and_workspace_and_pull_json_are_never_synced(agent_dir, capsys):
    for rel in ("evals/results/x.json", "workspace/memory/MEMORY.md", "pull.json", "tools.json", "prompt.json",
                ".pull/draft.json", "workflows/workflow_mgr1/SKILL.md"):
        p = agent_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
        code, out, err = _run_hook(p, capsys)
        assert code == 0 and out == "" and err == "", rel
        assert FakeClient.instances == [], rel


def test_non_agent_path_exits_zero_without_network(tmp_path, capsys):
    other = tmp_path / "src" / "main.py"
    other.parent.mkdir(parents=True)
    other.write_text("print(1)")
    code, out, err = _run_hook(other, capsys)
    assert code == 0 and out == "" and err == "" and FakeClient.instances == []
    # a .soleon/agents file with NO pull.json is not a pulled agent either
    stray = tmp_path / ".soleon" / "agents" / "ghost" / "SOUL.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("x")
    code, out, err = _run_hook(stray, capsys)
    assert code == 0 and FakeClient.instances == []
    # and the real process, against an unreachable server, exits 0 immediately
    proc = subprocess.run(
        [sys.executable, str(BIN / "soleon_draft_sync.py")],
        input=json.dumps({"tool_input": {"file_path": str(other)}}), capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "SOLEON_MCP_TOKEN": "t"},
    )
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    empty = subprocess.run([sys.executable, str(BIN / "soleon_draft_sync.py")], input="", capture_output=True,
                           text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    assert empty.returncode == 0


def test_conflict_blocks_with_exit_2_and_records_the_current_draft(agent_dir, capsys):
    class ConflictClient(FakeClient):
        def __init__(self, server_url):
            super().__init__(server_url)
            self.patch_answer = {"status": 409, "conflict": True,
                                 "detail": "The draft was modified by another session since your last read.",
                                 "current_draft": {"agent": {"soul": "theirs"}, "draftEtag": "1726560777777"},
                                 "next_action": "Re-check your changes against current_draft and retry"}

    (agent_dir / "SOUL.md").write_text("mine", encoding="utf-8")
    code, out, err = _run_hook(agent_dir / "SOUL.md", capsys, factory=ConflictClient)
    assert code == 2
    assert out == ""
    assert "changed elsewhere" in err and "/pull-agent demo-agent" in err and "adopt-etag" in err
    assert "Ask the user" in err or "ask" in err.lower()
    conflict = json.loads((agent_dir / ".pull" / "conflict.json").read_text())
    assert conflict["draftEtag"] == "1726560777777" and conflict["agent"] == {"soul": "theirs"}
    assert json.loads((agent_dir / "pull.json").read_text())["draftEtag"] == "1726560000000"  # unchanged
    assert [c[0] for c in FakeClient.instances[0].calls] == ["patch_agent_draft"]  # no test-chat sync after a refusal


def test_conflict_delivered_as_an_iserror_payload_is_handled_the_same(agent_dir, capsys):
    class ConflictErrClient(FakeClient):
        def __init__(self, server_url):
            super().__init__(server_url)
            self.patch_answer = client_mod.ToolError("conflict", {
                "status": 409, "conflict": True, "current_draft": {"agent": {}, "draftEtag": "5"}})

    code, out, err = _run_hook(agent_dir / "SOUL.md", capsys, factory=ConflictErrClient)
    assert code == 2 and (agent_dir / ".pull" / "conflict.json").is_file()


def test_other_platform_errors_exit_2_with_the_error_text(agent_dir, capsys):
    class RefusingClient(FakeClient):
        def __init__(self, server_url):
            super().__init__(server_url)
            self.patch_answer = {"status": 400, "error": "validation_failed",
                                 "detail": "soul exceeds the 64 KB cap", "next_action": "shorten it"}

    code, out, err = _run_hook(agent_dir / "SOUL.md", capsys, factory=RefusingClient)
    assert code == 2 and "soul exceeds the 64 KB cap" in err and out == ""

    class MissingTokenClient(FakeClient):
        def call_tool(self, name, arguments):
            raise client_mod.MissingTokenError(client_mod.RECONNECT_INSTRUCTION)

    code, out, err = _run_hook(agent_dir / "SOUL.md", capsys, factory=MissingTokenClient)
    assert code == 2 and "/mcp" in err


def test_first_save_without_a_draft_omits_expected_updated_at(tmp_path, capsys):
    agent_dir = make_pulled_dir(tmp_path, has_draft=False, with_zip=False)
    proc = subprocess.run(
        [sys.executable, str(PULL_ASSETS / "pull_agent.py"), "materialize", "--slug", SLUG, "--dir", str(agent_dir),
         "--server-url", SERVER_URL, "--model", "sonnet", "--plugin-root", str(PLUGIN)],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr

    class CreateClient(FakeClient):
        def __init__(self, server_url):
            super().__init__(server_url)
            self.patch_answer = {"status": 201, "body": {"patched": True, "draft_created": True, "draft_etag": "42",
                                                          "changed_keys": ["soul"], "deleted_keys": []}}

    code, out, err = _run_hook(agent_dir / "SOUL.md", capsys, factory=CreateClient)
    assert code == 0, err
    args = FakeClient.instances[0].calls[0][1]
    assert "expected_updated_at" not in args
    pull = json.loads((agent_dir / "pull.json").read_text())
    assert pull["draftEtag"] == "42" and pull["source"] == "draft"
    assert "draft created" in json.loads(out)["systemMessage"]


def test_no_change_answer_is_reported_not_treated_as_failure(agent_dir, capsys):
    class NoChangeClient(FakeClient):
        def __init__(self, server_url):
            super().__init__(server_url)
            self.patch_answer = {"status": 200, "body": {"patched": False, "no_change": True, "draft_etag": "1726560000000"}}

    code, out, err = _run_hook(agent_dir / "SOUL.md", capsys, factory=NoChangeClient)
    assert code == 0 and "already matched" in json.loads(out)["systemMessage"]


def test_hooks_json_wires_the_script_on_write_edit_multiedit():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    (entry,) = hooks["hooks"]["PostToolUse"]
    assert entry["matcher"] == "Write|Edit|MultiEdit"
    (cmd,) = entry["hooks"]
    assert cmd["type"] == "command"
    assert cmd["command"] == 'python3 "${CLAUDE_PLUGIN_ROOT}/bin/soleon_draft_sync.py"'
