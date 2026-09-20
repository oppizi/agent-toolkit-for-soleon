"""bin/soleon_pull_refresh.py — the UserPromptSubmit hook that keeps a pulled
agent in step with the platform.

Before each prompt: one `get_agent_draft` read per pulled agent; same version
→ silent, no writes. A different version → the pull's reads are repeated
(config, skills, sandbox sync, tools, system prompt), the files are rewritten
and `materialize` re-runs with `--keep-workspace`; a pending save conflict is
never overwritten; a failed read leaves the local copy untouched. The hook
never blocks the prompt."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from _local_emulation_fixtures import (
    BIN, DOCUMENT, PLUGIN, PULL_ASSETS, SERVER_URL, SLUG, SOUL, config_envelope, draft_envelope,
    make_pulled_dir, prompt_envelope, skills_envelope, tools_envelope,
)

sys.path.insert(0, str(BIN))
import soleon_agent_document as doc  # noqa: E402
import soleon_pull_refresh as refresh  # noqa: E402
from soleon_mcp_client import ToolError  # noqa: E402

OLD_ETAG = "1726560000000"
NEW_ETAG = "1726560999000"
NEW_SOUL = "# Demo v2\n\nYou are Demo, now edited in the Soleon editor.\n"


class FakeClient:
    """Answers the pull's reads; `draft` is what get_agent_draft returns."""

    instances: list = []

    def __init__(self, server_url, *, draft=None, fail=None):
        self.server_url = server_url
        self.calls = []
        self._sleep = lambda seconds: None
        self.draft = draft if draft is not None else draft_envelope(True, OLD_ETAG)
        self.fail = fail or {}
        FakeClient.instances.append(self)

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.fail:
            err = self.fail[name]
            if isinstance(err, Exception):
                raise err
            return err
        answers = {
            "get_agent_draft": self.draft,
            "get_agent_config": config_envelope(),
            "get_agent_skills": skills_envelope(),
            "sync_draft_test_chat": {"status": 200, "body": {"synced": True, "hasDraft": True}},
            "list_agent_tools": tools_envelope(),
            "get_agent_system_prompt": prompt_envelope(),
        }
        return json.loads(json.dumps(answers[name]))


def _materialize(root: Path, agent_dir: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(PULL_ASSETS / "pull_agent.py"), "materialize", "--slug", SLUG, "--dir", str(agent_dir),
         "--server-url", SERVER_URL, "--model", "sonnet", "--plugin-root", str(PLUGIN),
         "--agents-dir", str(root / ".claude" / "agents")],
        capture_output=True, text=True, timeout=60, cwd=str(root), env={"PATH": "/usr/bin:/bin", "HOME": str(root)},
    )
    assert proc.returncode == 0, proc.stderr


def _pulled(tmp_path: Path) -> Path:
    agent_dir = make_pulled_dir(tmp_path)
    _materialize(tmp_path, agent_dir)
    assert json.loads((agent_dir / "pull.json").read_text())["draftEtag"] == OLD_ETAG
    return agent_dir


def _run(tmp_path: Path, factory, runner=subprocess.run):
    out, err = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = out, err
    try:
        rc = refresh.main(stream=io.StringIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "UserPromptSubmit",
                                                          "user_prompt": "talk to demo-agent"})),
                          client_factory=factory, runner=runner)
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    return rc, out.getvalue(), err.getvalue()


def _real_runner_with_home(home: Path):
    def run(cmd, **kw):
        kw.setdefault("env", {"PATH": "/usr/bin:/bin", "HOME": str(home)})
        return subprocess.run(cmd, **kw)
    return run


def test_no_pulled_agent_means_no_network_and_no_output(tmp_path):
    FakeClient.instances.clear()
    rc, out, _ = _run(tmp_path, FakeClient)
    assert rc == 0 and out == "" and FakeClient.instances == []


def test_same_version_is_one_read_and_silent(tmp_path):
    agent_dir = _pulled(tmp_path)
    before = (agent_dir / "SOUL.md").read_text(encoding="utf-8")
    FakeClient.instances.clear()
    rc, out, _ = _run(tmp_path, FakeClient)
    assert rc == 0 and out == ""
    assert [c[0] for c in FakeClient.instances[0].calls] == ["get_agent_draft"]
    assert (agent_dir / "SOUL.md").read_text(encoding="utf-8") == before


def test_changed_draft_is_re_pulled_and_re_materialized(tmp_path):
    agent_dir = _pulled(tmp_path)
    # the person edited the workspace locally — a refresh must not touch it
    (agent_dir / "workspace" / "memory" / "MEMORY.md").write_text("# Memory\n- local edit\n", encoding="utf-8")
    subagent = tmp_path / ".claude" / "agents" / "{}.md".format(SLUG)
    assert subagent.is_file()

    new_doc = json.loads(json.dumps(DOCUMENT))
    new_doc["soul"] = NEW_SOUL
    new_draft = draft_envelope(True, NEW_ETAG)
    new_draft["body"]["agent"] = new_doc

    def factory(url):
        return FakeClient(url, draft=new_draft)

    FakeClient.instances.clear()
    rc, out, _ = _run(tmp_path, factory, runner=_real_runner_with_home(tmp_path))
    assert rc == 0
    calls = [c[0] for c in FakeClient.instances[0].calls]
    assert calls == ["get_agent_draft", "get_agent_config", "get_agent_skills", "sync_draft_test_chat",
                     "list_agent_tools", "get_agent_system_prompt"]
    # the files follow the platform
    assert (agent_dir / "SOUL.md").read_text(encoding="utf-8") == NEW_SOUL
    pull = json.loads((agent_dir / "pull.json").read_text())
    assert pull["draftEtag"] == NEW_ETAG and pull["refreshedFrom"] == "draft:" + NEW_ETAG and pull["refreshedAt"]
    assert doc.unwrap(json.loads((agent_dir / ".pull" / "draft.json").read_text()))["draftEtag"] == NEW_ETAG
    assert subagent.is_file() and "pulled" in subagent.read_text(encoding="utf-8")
    # …except the workspace, which keeps the local edit
    assert (agent_dir / "workspace" / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "# Memory\n- local edit\n"
    # the person and Claude both hear about it
    payload = json.loads(out)
    assert "refreshed" in payload["systemMessage"] and "Demo Agent" in payload["systemMessage"]
    assert payload["hookSpecificOutput"] == {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": payload["systemMessage"]}


def test_pending_conflict_is_never_overwritten(tmp_path):
    agent_dir = _pulled(tmp_path)
    (agent_dir / ".pull" / "conflict.json").write_text("{}", encoding="utf-8")
    before = (agent_dir / "SOUL.md").read_text(encoding="utf-8")

    def factory(url):
        return FakeClient(url, draft=draft_envelope(True, NEW_ETAG))

    FakeClient.instances.clear()
    rc, out, _ = _run(tmp_path, factory)
    assert rc == 0
    assert [c[0] for c in FakeClient.instances[0].calls] == ["get_agent_draft"]
    assert (agent_dir / "SOUL.md").read_text(encoding="utf-8") == before
    assert json.loads((agent_dir / "pull.json").read_text())["draftEtag"] == OLD_ETAG
    msg = json.loads(out)["systemMessage"]
    assert "NOT refreshed" in msg and "adopt-etag" in msg


def test_failed_read_leaves_the_local_copy_untouched(tmp_path):
    agent_dir = _pulled(tmp_path)
    before = {p.name: p.read_text(encoding="utf-8") for p in (agent_dir / "SOUL.md", agent_dir / "pull.json", agent_dir / "tools.json")}

    def factory(url):
        return FakeClient(url, draft=draft_envelope(True, NEW_ETAG),
                          fail={"list_agent_tools": {"status": 200, "state": "error", "call_id": "lc_x", "op": "tool_list",
                                                     "error": "connections_required", "detail": "connect Gmail",
                                                     "next_action": "connect"}})

    FakeClient.instances.clear()
    rc, out, _ = _run(tmp_path, factory)
    assert rc == 0
    for p in (agent_dir / "SOUL.md", agent_dir / "pull.json", agent_dir / "tools.json"):
        assert p.read_text(encoding="utf-8") == before[p.name]
    assert doc.unwrap(json.loads((agent_dir / ".pull" / "draft.json").read_text()))["draftEtag"] == OLD_ETAG
    msg = json.loads(out)["systemMessage"]
    assert "refresh failed" in msg and "unchanged" in msg


def test_platform_unreachable_never_blocks_the_prompt(tmp_path):
    _pulled(tmp_path)

    def factory(url):
        return FakeClient(url, fail={"get_agent_draft": ToolError("HTTP 503", {"error": "unavailable"})})

    rc, out, _ = _run(tmp_path, factory)
    assert rc == 0
    assert "using the local copy" in json.loads(out)["systemMessage"]


def test_deployed_only_agent_follows_the_baseline(tmp_path):
    agent_dir = make_pulled_dir(tmp_path, has_draft=False, with_zip=False)
    _materialize(tmp_path, agent_dir)
    assert refresh.local_version_of(json.loads((agent_dir / "pull.json").read_text())) == "deployed:7"
    # same baseline → silent
    FakeClient.instances.clear()
    rc, out, _ = _run(tmp_path, lambda url: FakeClient(url, draft=draft_envelope(False)))
    assert rc == 0 and out == ""
    # a draft appears (someone opened the editor and saved) → refreshed
    rc, out, _ = _run(tmp_path, lambda url: FakeClient(url, draft=draft_envelope(True, NEW_ETAG)),
                      runner=_real_runner_with_home(tmp_path))
    assert rc == 0 and "refreshed" in json.loads(out)["systemMessage"]
    assert json.loads((agent_dir / "pull.json").read_text())["source"] == "draft"


def test_version_strings():
    assert refresh.version_of({"hasDraft": True, "draftEtag": "5"}) == "draft:5"
    assert refresh.version_of({"hasDraft": False, "baselineEtag": "7"}) == "deployed:7"
    assert refresh.version_of({"hasDraft": True}) == "deployed:"
    assert refresh.local_version_of({"draftEtag": None, "baselineEtag": "7"}) == "deployed:7"
