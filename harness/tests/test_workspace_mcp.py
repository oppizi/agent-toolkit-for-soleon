"""bin/soleon_workspace_mcp.py — the stdio server is driven as a real subprocess
over newline-delimited JSON-RPC: initialize, tools/list (the platform's four
filesystem tools, byte-for-byte names + schemas), tools/call round trips, path
escapes rejected as error results, read-only extra roots."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from _local_emulation_fixtures import BIN

sys.path.insert(0, str(BIN))
import soleon_workspace_mcp as ws_mod  # noqa: E402

SERVER = BIN / "soleon_workspace_mcp.py"


class Stdio:
    def __init__(self, *args: str):
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin"},
        )
        self._id = 0

    def request(self, method: str, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, self.proc.stderr.read().decode()
        resp = json.loads(line)
        assert resp["id"] == self._id
        return resp

    def notify(self, method: str):
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())
        self.proc.stdin.flush()

    def call(self, name: str, **arguments):
        resp = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "result" in resp, resp
        return resp["result"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture()
def server(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "memory").mkdir()
    (root / "memory" / "MEMORY.md").write_text("# Memory\n- likes tea\n", encoding="utf-8")
    skills = tmp_path / "skills"
    (skills / "cite").mkdir(parents=True)
    (skills / "cite" / "SKILL.md").write_text("# Cite\n", encoding="utf-8")
    s = Stdio(str(root), "--readable", str(skills))
    yield s, root, skills
    s.close()


def text_of(result) -> str:
    return result["content"][0]["text"]


def test_initialize_and_tools_list_match_the_platform_surface(server):
    s, root, _ = server
    init = s.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {}}
    assert init["result"]["serverInfo"]["name"] == "soleon-workspace"
    s.notify("notifications/initialized")
    assert s.request("ping")["result"] == {}
    tools = s.request("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["read_file", "write_file", "edit_file", "list_dir"]
    by = {t["name"]: t for t in tools}
    assert by["read_file"]["inputSchema"]["required"] == ["path"]
    assert set(by["read_file"]["inputSchema"]["properties"]) == {"path", "offset", "limit"}
    assert by["write_file"]["inputSchema"]["required"] == ["path", "content"]
    assert by["edit_file"]["inputSchema"]["required"] == ["path", "old_text", "new_text"]
    assert set(by["edit_file"]["inputSchema"]["properties"]) == {"path", "old_text", "new_text", "replace_all"}
    assert set(by["list_dir"]["inputSchema"]["properties"]) == {"path", "recursive", "max_entries"}
    assert by["list_dir"]["description"].startswith("List the contents of a directory.")
    assert "Returns numbered lines" in by["read_file"]["description"]


def test_write_read_edit_list_round_trip(server):
    s, root, _ = server
    w = s.call("write_file", path="notes/todo.md", content="- milk\n- eggs\n")
    assert "isError" not in w and "Successfully wrote 14 bytes" in text_of(w)
    assert (root / "notes" / "todo.md").read_text() == "- milk\n- eggs\n"
    r = s.call("read_file", path="notes/todo.md")
    assert text_of(r).startswith("1| - milk\n2| - eggs\n")
    assert "(End of file — 2 lines total)" in text_of(r)
    r2 = s.call("read_file", path="notes/todo.md", offset=2, limit=1)
    assert text_of(r2).startswith("2| - eggs")
    e = s.call("edit_file", path="notes/todo.md", old_text="- eggs", new_text="- bread")
    assert "Successfully edited" in text_of(e)
    assert (root / "notes" / "todo.md").read_text() == "- milk\n- bread\n"
    miss = s.call("edit_file", path="notes/todo.md", old_text="- cheese", new_text="x")
    assert miss.get("isError") is True and "old_text not found" in text_of(miss)
    l = s.call("list_dir", path=".")
    assert "📁 memory" in text_of(l) and "📁 notes" in text_of(l)
    lr = s.call("list_dir", path=".", recursive=True)
    assert "memory/MEMORY.md" in text_of(lr) and "notes/todo.md" in text_of(lr)
    # absolute paths under the root, and the container aliases, resolve too
    assert "likes tea" in text_of(s.call("read_file", path=str(root / "memory" / "MEMORY.md")))
    assert "likes tea" in text_of(s.call("read_file", path="/mnt/workspace/memory/MEMORY.md"))
    assert "likes tea" in text_of(s.call("read_file", path="/app/workspace/memory/MEMORY.md"))


def test_path_escape_is_rejected_as_an_error_result(server, tmp_path):
    s, root, _ = server
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    for path in ("../secret.txt", str(outside), "/etc/passwd", "memory/../../secret.txt"):
        r = s.call("read_file", path=path)
        assert r.get("isError") is True, path
        assert "outside allowed directory" in text_of(r), path
    w = s.call("write_file", path="../evil.txt", content="x")
    assert w.get("isError") is True and not (tmp_path / "evil.txt").exists()
    # the server is still alive after every rejection
    assert s.request("ping")["result"] == {}


def test_readable_extra_root_is_read_only(server):
    s, root, skills = server
    r = s.call("read_file", path=str(skills / "cite" / "SKILL.md"))
    assert "isError" not in r and "1| # Cite" in text_of(r)
    w = s.call("write_file", path=str(skills / "cite" / "SKILL.md"), content="hacked")
    assert w.get("isError") is True and "read-only" in text_of(w)
    assert (skills / "cite" / "SKILL.md").read_text() == "# Cite\n"
    e = s.call("edit_file", path=str(skills / "cite" / "SKILL.md"), old_text="Cite", new_text="X")
    assert e.get("isError") is True


def test_unknown_tool_and_method(server):
    s, _, _ = server
    r = s.call("delete_everything")
    assert r.get("isError") is True and "Unknown tool" in text_of(r)
    resp = s.request("resources/list")
    assert resp["error"]["code"] == -32601


def test_whole_file_read_of_a_big_file_pages_under_claude_codes_output_cap(tmp_path):
    """A 58 KB HTML newsletter read with no offset/limit used to come back whole,
    trip Claude Code's 25k-token MCP output cap, and reach the agent as an
    "exceeds maximum allowed tokens" error (a failed step on the trace). The
    page must stay under the cap even for dense markup and end in the
    platform's own continuation tail, so the agent pages instead of failing."""
    ws = ws_mod.Workspace(str(tmp_path))
    # 141 lines of ~420 dense chars, like a minified HTML email body.
    line = ("<td style=\"padding:0;margin:0\"><a href=\"https://x.y/z\">" * 8)[:420]
    (tmp_path / "mail.txt").write_text("\n".join(line for _ in range(141)) + "\n", encoding="utf-8")
    out = ws_mod._read_file(ws, path="mail.txt")
    assert not out.startswith("Error")
    # ~1.5 chars/token for markup this dense → 30k chars ≈ 20k tokens < 25k.
    assert len(out) <= ws_mod._READ_MAX_CHARS + 200
    assert "Use offset=" in out and "of 141" in out
    # Following the tail page by page reaches the end with every line seen
    # exactly once, and each page stays under the cap.
    seen, page, pages = 0, out, 0
    while True:
        pages += 1
        assert len(page) <= ws_mod._READ_MAX_CHARS + 200
        body = page.split("\n\n(", 1)[0]
        seen += body.count("\n") + 1
        if "(End of file" in page:
            break
        nxt = int(page.rsplit("offset=", 1)[1].split(" ", 1)[0])
        assert nxt == seen + 1
        page = ws_mod._read_file(ws, path="mail.txt", offset=nxt)
        assert page.startswith("{}| ".format(nxt))
    assert seen == 141 and pages > 1 and page.rstrip().endswith("(End of file — 141 lines total)")


def test_edit_file_tolerates_whitespace_and_crlf(tmp_path):
    ws = ws_mod.Workspace(str(tmp_path))
    (tmp_path / "a.txt").write_bytes(b"line one\r\n  line two\r\nline three\r\n")
    # whitespace-trimmed window match (no exact substring): the platform's fallback
    out = ws_mod._edit_file(ws, path="a.txt", old_text="line one\nline two", new_text="LINE 1\nLINE 2")
    assert out.startswith("Successfully edited")
    assert (tmp_path / "a.txt").read_bytes() == b"LINE 1\r\nLINE 2\r\nline three\r\n"  # CRLF preserved
    (tmp_path / "b.txt").write_text("x\nx\n")
    warn = ws_mod._edit_file(ws, path="b.txt", old_text="x", new_text="y")
    assert warn.startswith("Warning: old_text appears 2 times")
    assert ws_mod._edit_file(ws, path="b.txt", old_text="x", new_text="y", replace_all=True).startswith("Successfully")
    assert (tmp_path / "b.txt").read_text() == "y\ny\n"
