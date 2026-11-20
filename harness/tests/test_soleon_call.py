"""bin/soleon_call.py — call one Soleon MCP tool from a terminal (2026-11-20).

The fake client stands in at the client boundary (`list_tools` reads
`_resolve_token` + `_post`, a call reads `call_tool`), so no test reaches the
network or reads a real sign-in."""
from __future__ import annotations

import json
import sys

import pytest

from _local_emulation_fixtures import BIN

sys.path.insert(0, str(BIN))
import soleon_call as sc  # noqa: E402
from soleon_mcp_client import AuthError, MissingTokenError, ToolError, TransportError  # noqa: E402


class FakeClient:
    def __init__(self, server_url, credentials_path=None, *, tools=None, answers=None, raises=None):
        self.server_url = server_url
        self.credentials_path = credentials_path
        self.tools = tools if tools is not None else [{"name": "list_agents"}, {"name": "search_knowledge_base"}]
        self.answers = list(answers or [])
        self.raises = raises
        self.calls = []
        self._sleep = lambda _s: None

    def _resolve_token(self):
        if self.raises:
            raise self.raises
        return "tok"

    def _post(self, payload, token):
        assert payload["method"] == "tools/list" and token == "tok"
        return {"jsonrpc": "2.0", "id": 1, "result": {"tools": self.tools}}

    def call_tool(self, name, arguments):
        if self.raises:
            raise self.raises
        self.calls.append((name, dict(arguments)))
        return self.answers.pop(0)


def _run(argv, capsys, **fake):
    made = {}

    def factory(url, credentials_path=None):
        made["client"] = FakeClient(url, credentials_path, **fake)
        return made["client"]

    code = sc.main(argv, client_factory=factory)
    out, err = capsys.readouterr()
    return code, out, err, made.get("client")


def test_list_prints_the_names_the_server_offers(capsys):
    code, out, err, _ = _run(["--list"], capsys)
    assert code == 0
    assert out.split() == ["list_agents", "search_knowledge_base"]
    assert "2 tool(s) from https://mcp-dev.oppizi.com/mcp" in err


def test_list_grep_narrows_the_names(capsys):
    code, out, _, _ = _run(["--list", "--grep", "knowledge"], capsys)
    assert code == 0 and out.split() == ["search_knowledge_base"]


def test_a_call_prints_the_envelope_and_sends_the_arguments(capsys):
    envelope = {"status": 200, "body": {"hits": []}}
    code, out, _, client = _run(["search_knowledge_base", '{"kb_id": "2ebb3c747a88", "query": "x"}'],
                                capsys, answers=[envelope])
    assert code == 0 and json.loads(out) == envelope
    assert client.calls == [("search_knowledge_base", {"kb_id": "2ebb3c747a88", "query": "x"})]


def test_arguments_can_come_from_a_file(capsys, tmp_path):
    path = tmp_path / "args.json"
    path.write_text('{"slug": "a"}', encoding="utf-8")
    code, _, _, client = _run(["get_agent", "@" + str(path)], capsys, answers=[{"status": 200}])
    assert code == 0 and client.calls == [("get_agent", {"slug": "a"})]


def test_a_pending_agent_call_is_followed_to_its_end(capsys):
    pending = {"state": "pending", "call_id": "lc_1", "op": "tool_call"}
    done = {"state": "done", "call_id": "lc_1", "result": {"ok": True}}
    code, out, err, client = _run(["call_agent_tool", '{"slug": "kb-extractor", "name": "t"}'],
                                  capsys, answers=[pending, done])
    assert code == 0 and json.loads(out)["result"] == {"ok": True}
    assert client.calls[1] == ("get_agent_tool_result",
                               {"slug": "kb-extractor", "app_env": "dev", "call_id": "lc_1"})
    assert "pending: tool_call (call lc_1)" in err


@pytest.mark.parametrize("envelope", [
    {"error": "not_found", "detail": "no such agent"},
    {"state": "error", "error": "unknown_tool"},
    {"status": 404, "body": {"error": "knowledge base not found"}},
])
def test_an_error_answer_exits_1(capsys, envelope):
    code, out, _, _ = _run(["get_agent", '{"slug": "a"}'], capsys, answers=[envelope])
    assert code == 1 and json.loads(out) == envelope


@pytest.mark.parametrize("exc, code", [
    (MissingTokenError("sign in"), 3), (AuthError("rejected"), 3),
    (ToolError("boom"), 1), (TransportError("down"), 4),
])
def test_failures_map_to_exit_codes(capsys, exc, code):
    got, _, err, _ = _run(["get_agent", '{"slug": "a"}'], capsys, raises=exc)
    assert got == code and str(exc) in err


@pytest.mark.parametrize("argv", [[], ["get_agent", "[1, 2]"], ["get_agent", "{not json"]])
def test_bad_usage_exits_2(capsys, argv):
    code, _, _, client = _run(argv, capsys)
    assert code == 2 and client is None


def test_server_url_comes_from_the_flag_then_the_environment(capsys, monkeypatch):
    monkeypatch.setenv("SOLEON_MCP_URL", "https://mcp.example/mcp")
    _, _, err, _ = _run(["--list"], capsys)
    assert "from https://mcp.example/mcp" in err
    _, _, err, _ = _run(["--list", "--server-url", "https://other/mcp"], capsys)
    assert "from https://other/mcp" in err
