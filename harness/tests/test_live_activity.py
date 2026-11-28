"""bin/soleon_activity.py — the live activity feed of a local run.

An app calling an agent through Soleon's API can ask for its live activity
(`soleon_activity`): each finished step with the status line's safe label and
the files the step saved. A local run started with SOLEON_ACTIVITY gives the
app the same entries, written to a feed file, each file saved beside it."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from _local_emulation_fixtures import BIN, SLUG

sys.path.insert(0, str(BIN))
import soleon_activity as activity  # noqa: E402
import soleon_agent_tools_mcp as shim  # noqa: E402


def test_an_svg_is_matched_only_when_named(tmp_path):
    assert not activity.type_matches("image/svg+xml", ["image/*"])
    assert not activity.type_matches("text/html", ["*/*"])
    assert activity.type_matches("image/svg+xml", ["image/svg+xml"])
    assert activity.type_matches("image/png", ["image/*"])


def _spec(tmp_path, **over):
    value = {"feed": str(tmp_path / "run-1.jsonl"), "files": ["image/*", "image/svg+xml", "application/json"],
             "max_file_bytes": 1000, **over}
    return activity.spec({activity.ENV: json.dumps(value)})


def _opener(body=b"<svg/>"):
    seen = []

    def opener(url):
        seen.append(url)
        return io.BytesIO(body)
    opener.seen = seen
    return opener


def _saved(path, ctype="image/svg+xml", size=6, url="https://s3.example/x?sig=1"):
    return {"path": path, "contentType": ctype, "size": size, "url": url}


def test_no_variable_means_no_feed():
    assert activity.spec({}) is None


@pytest.mark.parametrize("raw", ["not json", "[]", '{"files": []}', '{"feed": "x", "files": "image/*"}'])
def test_an_unusable_request_writes_nothing(raw):
    assert activity.spec({activity.ENV: raw}) is None


def test_an_entry_names_the_files_asked_for_and_saves_them_beside_the_feed(tmp_path):
    s = _spec(tmp_path)
    opener = _opener()
    entry = activity.build_entry(s, seq=1, platform_name="write_file", entry={}, turn="t1", at=1795000000.0,
                                 saved=[_saved("builds/b1/option-a-front.svg"),
                                        _saved("notes.txt", ctype="text/plain")], opener=opener)
    assert entry["label"] == "Working…" and entry["tool"] == "write_file"
    (f,) = entry["files"]
    assert f["path"] == "builds/b1/option-a-front.svg" and f["filename"] == "option-a-front.svg"
    assert Path(f["local_path"]).read_bytes() == b"<svg/>"
    assert Path(f["local_path"]).parent == tmp_path / "run-1-files" / "0001"


def test_a_file_over_the_limit_is_named_without_being_fetched(tmp_path):
    s = _spec(tmp_path, max_file_bytes=5)
    opener = _opener()
    entry = activity.build_entry(s, seq=1, platform_name="run_code_file", entry={}, turn="t1", at=0,
                                 saved=[_saved("big.png", ctype="image/png", size=6)], opener=opener)
    (f,) = entry["files"]
    assert "local_path" not in f and opener.seen == []


def test_only_https_links_are_fetched(tmp_path):
    s = _spec(tmp_path)
    entry = activity.build_entry(s, seq=1, platform_name="write_file", entry={}, turn="t1", at=0,
                                 saved=[_saved("a.svg", url="file:///etc/passwd")], opener=_opener())
    assert "local_path" not in entry["files"][0]


@pytest.mark.parametrize("name,entry,label", [
    ("read_file", {}, "Reading files…"),
    ("web_prompt", {}, "Searching the web…"),
    ("kb_handbook_read", {}, "Searching the knowledge base…"),
    ("browser_navigate", {}, "Browsing…"),
    ("mcp_am_eb204f248a304985_read", {"serverName": "Google Drive"}, "Using Google Drive…"),
    ("github_read", {"serverName": "GitHub"}, "Using GitHub…"),
    ("web_fetch", {}, "Working…"),
    ("write_and_run_code", {}, "Running Python…"),
    ("run_code_file", {}, "Running Python…"),
])
def test_labels_follow_the_status_lines_rules(name, entry, label):
    assert activity.label_for(name, entry) == label


class _Client:
    def __init__(self, envelope):
        self.envelope = envelope

    @staticmethod
    def _sleep(_seconds):  # await_control's poll wait; a done envelope never waits
        raise AssertionError("a terminal envelope must not be polled")

    def call_tool(self, name, args):
        return self.envelope


def test_a_platform_call_adds_its_entry_without_waiting(tmp_path, monkeypatch):
    s = _spec(tmp_path)
    feed = activity.Feed(s, opener=_opener())
    monkeypatch.setattr(activity, "current", lambda: feed)
    tools = [{"name": "write_file", "kind": "external", "description": "", "inputSchema": {"type": "object"}}]
    envelope = {"state": "done", "result": {"tool": "write_file", "result": "ok", "liveActivity": "steps_and_files",
                                            "savedFiles": [_saved("studio/t1/001.json", ctype="application/json")]}}
    server = shim.AgentToolsServer(SLUG, tools, _Client(envelope))
    out = server.call("write_file", {"path": "studio/t1/001.json", "content": "{}"})
    assert not out.get("isError")
    feed.flush()
    (line,) = [json.loads(x) for x in Path(s["feed"]).read_text(encoding="utf-8").splitlines()]
    assert line["tool"] == "write_file" and [f["path"] for f in line["files"]] == ["studio/t1/001.json"]


def test_with_the_agents_live_activity_off_the_feed_gets_nothing(tmp_path, monkeypatch):
    """The setting is the agent's: Soleon names it on the reply, and a reply
    that says Off writes no line, whatever the app asked for."""
    s = _spec(tmp_path)
    feed = activity.Feed(s, opener=_opener())
    monkeypatch.setattr(activity, "current", lambda: feed)
    tools = [{"name": "write_file", "kind": "external", "description": "", "inputSchema": {"type": "object"}}]
    envelope = {"state": "done", "result": {"tool": "write_file", "result": "ok", "liveActivity": "off",
                                            "savedFiles": [_saved("a.json", ctype="application/json")]}}
    shim.AgentToolsServer(SLUG, tools, _Client(envelope)).call("write_file", {"path": "a", "content": "x"})
    feed.flush()
    assert not Path(s["feed"]).exists()


def test_a_platform_that_predates_the_setting_still_gets_its_line(tmp_path, monkeypatch):
    """A reply that does not name the setting comes from a platform without it
    (every call got a line there): unchanged until that platform takes it."""
    s = _spec(tmp_path)
    feed = activity.Feed(s, opener=_opener())
    monkeypatch.setattr(activity, "current", lambda: feed)
    tools = [{"name": "write_file", "kind": "external", "description": "", "inputSchema": {"type": "object"}}]
    envelope = {"state": "done", "result": {"tool": "write_file", "result": "ok"}}
    shim.AgentToolsServer(SLUG, tools, _Client(envelope)).call("write_file", {"path": "a", "content": "x"})
    feed.flush()
    assert len(Path(s["feed"]).read_text(encoding="utf-8").splitlines()) == 1


def test_steps_writes_the_line_without_files(tmp_path, monkeypatch):
    s = _spec(tmp_path)
    feed = activity.Feed(s, opener=_opener())
    monkeypatch.setattr(activity, "current", lambda: feed)
    tools = [{"name": "web_search", "kind": "external", "description": "", "inputSchema": {"type": "object"}}]
    envelope = {"state": "done", "result": {"tool": "web_search", "result": "ok", "liveActivity": "steps"}}
    shim.AgentToolsServer(SLUG, tools, _Client(envelope)).call("web_search", {"query": "car wash"})
    feed.flush()
    (line,) = [json.loads(x) for x in Path(s["feed"]).read_text(encoding="utf-8").splitlines()]
    assert line["files"] == [] and line["detail"] == "Searching “car wash”"


@pytest.mark.parametrize("envelope,expected", [
    ({"state": "done", "result": {"liveActivity": "steps"}}, "steps"),
    ({"state": "done", "result": {"liveActivity": "steps_and_files"}}, "steps_and_files"),
    ({"state": "done", "liveActivity": "steps"}, "steps"),
    ({"state": "done", "result": {"liveActivity": "off"}}, "off"),
    ({"state": "done", "result": {"liveActivity": "loud"}}, "off"),
    ({"state": "done", "result": {"result": 1}}, None),
    ("text", None),
])
def test_the_setting_is_read_from_the_reply(envelope, expected):
    assert activity.live_activity_of(envelope) == expected


def test_without_a_feed_a_call_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "current", lambda: None)
    tools = [{"name": "write_file", "kind": "external", "description": "", "inputSchema": {"type": "object"}}]
    server = shim.AgentToolsServer(SLUG, tools, _Client({"state": "done", "result": {"result": "ok"}}))
    assert not server.call("write_file", {"path": "a", "content": "x"}).get("isError")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("tool,args,expected", [
    # The SAME table as agent-infra tests/test_trace_stream_router_live_activity.py: one rule set, two copies.
    ("web_search", {"query": "speeders car wash photos"}, "Searching “speeders car wash photos”"),
    ("web_fetch", {"url": "https://www.speeders.com/locations?token=SECRET#x"}, "Reading speeders.com/locations"),
    ("web_fetch", {"url": "https://cdn.example.com/img/hero.JPG?w=2000"}, "Downloading a picture from cdn.example.com"),
    ("web_fetch", {"url": "https://fonts.example.com/Inter-Bold.woff2"}, "Downloading the Inter font"),
    ("web_fetch", {"url": "https://fonts.example.com/a1b2c3d4e5f6.woff2"}, "Downloading a font from fonts.example.com"),
    ("web_fetch", {"url": "https://www.speeders.com/assets/site.css"}, "Reading a stylesheet from speeders.com"),
    ("web_fetch", {"url": "https://user:pass@example.com/"}, "Reading a site"),
    ("browser_navigate", {"url": "https://speeders.com/?q=1"}, "Opening speeders.com"),
    ("browser_screenshot", {"full_page": True}, "Taking a screenshot of the page"),
    ("read_file", {"path": "skills/company-flyer-builder/references/5-design.md"}, "Reading 5-design.md"),
    ("write_file", {"path": "studio/notes/001.json", "content": "{\"kind\": \"name\"}"}, "Saving 001.json"),
    ("write_and_run_code", {"request_id": "flyer/build-two-options", "code": "print(1)"},
     "Running code: build two options"),
    ("run_code_file", {"path": "skills/x/engine/flyer_builder.py"}, "Running flyer_builder.py"),
    # A saved script runs by its id, which nobody may see (the platform's rule, 2026-11-28).
    ("run_code_file", {"file": "scr_f996423e0d74fb90094ee04e"}, "Running a saved script"),
    ("run_code_file", {"file": "scr_f996"}, "Running scr_f996"),
    ("attach_file", {"path": "out/a-front.svg", "filename": "a-front.svg"}, "Handing over a-front.svg"),
    ("spawn_worker", {"task": "secret plan"}, None),
    ("web_search", {}, None),
])
def test_a_calls_detail_follows_the_platforms_rules(tool, args, expected):
    assert activity.detail_for(tool, args) == expected


@pytest.mark.parametrize("tool,url,expected", [
    # The platform's security cases (agent-infra #3832): no path secret, no login.
    ("web_fetch", "https://hooks.slack.com/" + "/".join(["services", "T0FAKETEAM", "B0FAKEHOOK", "x" * 24]),
     "Reading hooks.slack.com/services"),
    ("browser_navigate", "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getMe",
     "Opening api.telegram.org"),
    ("web_fetch", "https://share.example.com/s/9f8e7d6c5b4a39281706f5e4d3c2b1a0", "Reading share.example.com/s"),
    ("web_fetch", "https://user:p?ss@host.com/x", "Reading a site"),
    ("web_fetch", "https://user:pa#ss@host.com/x", "Reading a site"),
    ("web_fetch", "https://user:pa/ss@host.com/x", "Reading a site"),
])
def test_a_url_shows_only_its_site_and_plain_path_parts(tool, url, expected):
    assert activity.detail_for(tool, {"url": url}) == expected


def test_an_integrations_tool_and_hidden_characters_get_nothing():
    assert activity.detail_for("mcp_am_eb204f248a304985_web_search", {"query": "SELECT 1"}) is None
    d = activity.detail_for("web_search", {"query": "a‮gnp.exe​ b\x85c"})
    assert all(ch not in d for ch in "‮​ \x85")


def test_a_feed_line_carries_the_calls_detail(tmp_path):
    s = _spec(tmp_path)
    line = activity.build_entry(s, seq=1, platform_name="web_search", entry={}, saved=[], turn="t", at=0,
                                args={"query": "car wash photos"})
    assert line["detail"] == "Searching “car wash photos”"
    assert "detail" not in activity.build_entry(s, seq=2, platform_name="spawn_worker", entry={}, saved=[],
                                                turn="t", at=0, args={"task": "x"})


def test_saved_files_are_read_from_the_reply():
    assert activity.saved_files_of({"state": "done", "result": {"savedFiles": [{"path": "a"}]}}) == [{"path": "a"}]
    assert activity.saved_files_of({"state": "done", "result": {"result": 1}}) == []
    assert activity.saved_files_of("text") == []
