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


def _spec(tmp_path, **over):
    value = {"feed": str(tmp_path / "run-1.jsonl"), "files": ["image/*", "application/json"],
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
    envelope = {"state": "done", "result": {"tool": "write_file", "result": "ok",
                                            "savedFiles": [_saved("studio/t1/001.json", ctype="application/json")]}}
    server = shim.AgentToolsServer(SLUG, tools, _Client(envelope))
    out = server.call("write_file", {"path": "studio/t1/001.json", "content": "{}"})
    assert not out.get("isError")
    feed.flush()
    (line,) = [json.loads(x) for x in Path(s["feed"]).read_text(encoding="utf-8").splitlines()]
    assert line["tool"] == "write_file" and [f["path"] for f in line["files"]] == ["studio/t1/001.json"]


def test_without_a_feed_a_call_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "current", lambda: None)
    tools = [{"name": "write_file", "kind": "external", "description": "", "inputSchema": {"type": "object"}}]
    server = shim.AgentToolsServer(SLUG, tools, _Client({"state": "done", "result": {"result": "ok"}}))
    assert not server.call("write_file", {"path": "a", "content": "x"}).get("isError")
    assert not list(tmp_path.iterdir())


def test_saved_files_are_read_from_the_reply():
    assert activity.saved_files_of({"state": "done", "result": {"savedFiles": [{"path": "a"}]}}) == [{"path": "a"}]
    assert activity.saved_files_of({"state": "done", "result": {"result": 1}}) == []
    assert activity.saved_files_of("text") == []
