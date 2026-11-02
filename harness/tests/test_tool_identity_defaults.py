"""A defaulted account parameter is the CONNECTION's account, not the local login.

Receipt (fund-raising-agent, 2026-09-21): asked for a new Google Sheet, the
local agent called ``mcp_google-sheets_create_spreadsheet`` with
``user_google_email`` set to the address Claude Code announces to every
session ("the user's email address is …") instead of leaving the parameter's
own default — ``dan@oppizi.com``, the account the platform connection is
authorized for. The upstream looked that address up in its credential store,
found nothing, and returned an OAuth consent link, so a healthy connection was
reported to the person as needing re-authorization.

Three guards, each sufficient on its own:
  1. the published schema SAYS the default is the connected account;
  2. a failed call that overrode it gets the cause named;
  3. the failure signal is the PLATFORM's own ``(MCP tool error: …)`` prefix,
     because an upstream ``isError`` reaches us as a successful result.
"""
from __future__ import annotations

import sys

from _local_emulation_fixtures import BIN

sys.path.insert(0, str(BIN))
import soleon_agent_tools_mcp as shim  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "user_google_email": {"type": "string", "description": "The user's Google email address. Required.",
                              "default": "dan@oppizi.com"},
        "title": {"type": "string", "description": "The title of the new spreadsheet."},
    },
    "required": ["title"],
}
TOOL = {"name": "mcp_google-sheets_create_spreadsheet", "kind": "external",
        "description": "Creates a new spreadsheet.", "inputSchema": SCHEMA}
LOCAL_LOGIN = "accounts@promofy.io"


def _published(tool=TOOL):
    return shim.published_tools([dict(tool)])[0]


def test_the_published_schema_says_the_default_is_the_connected_account():
    prop = _published()["inputSchema"]["properties"]["user_google_email"]
    assert prop["default"] == "dan@oppizi.com"
    assert shim.IDENTITY_DEFAULT_NOTE in prop["description"]
    # the parameter's own words are kept, the note is added to them
    assert prop["description"].startswith("The user's Google email address.")
    # a parameter with no email default is untouched
    assert _published()["inputSchema"]["properties"]["title"]["description"] == \
        "The title of the new spreadsheet."


def test_annotating_does_not_mutate_the_caller_s_tool_list():
    tools = [dict(TOOL)]
    shim.published_tools(tools)
    assert "description" not in SCHEMA["properties"]["user_google_email"] or \
        shim.IDENTITY_DEFAULT_NOTE not in SCHEMA["properties"]["user_google_email"]["description"]


def test_hint_names_both_addresses_when_the_default_was_overridden():
    hint = shim.identity_mismatch_hint(SCHEMA, {"user_google_email": LOCAL_LOGIN, "title": "Personal Notes"})
    assert hint is not None
    assert LOCAL_LOGIN in hint and "dan@oppizi.com" in hint
    assert "user_google_email" in hint


def test_no_hint_when_the_call_agrees_with_the_connection():
    for supplied in ("dan@oppizi.com", "DAN@Oppizi.com ", None):
        args = {"title": "x"} if supplied is None else {"user_google_email": supplied, "title": "x"}
        assert shim.identity_mismatch_hint(SCHEMA, args) is None, supplied


def test_no_hint_when_the_parameter_has_no_account_default():
    schema = {"properties": {"user_google_email": {"type": "string"}}}
    assert shim.identity_mismatch_hint(schema, {"user_google_email": LOCAL_LOGIN}) is None


class _Server(shim.AgentToolsServer):
    """Only ``_schema_for`` and the hint wrapper are under test here."""

    def __init__(self):
        self.published = shim.published_tools([dict(TOOL)])


def _result(text, is_error=False):
    out = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


def test_an_upstream_failure_arrives_as_a_SUCCESSFUL_result_and_still_gets_the_hint():
    # mcp_proxy renders an upstream isError as "(MCP tool error: …)" and hands
    # it back as state=done — the shape that actually reached the agent.
    server = _Server()
    out = server._with_identity_hint(
        TOOL["name"], {"user_google_email": LOCAL_LOGIN, "title": "Personal Notes"},
        _result("(MCP tool error: **ACTION REQUIRED: Google Authentication Needed** https://accounts.google.com/o/oauth2/auth?...)"),
    )
    assert len(out["content"]) == 2
    assert LOCAL_LOGIN in out["content"][1]["text"]
    assert "dan@oppizi.com" in out["content"][1]["text"]


def test_an_explicit_error_result_also_gets_the_hint():
    server = _Server()
    out = server._with_identity_hint(
        TOOL["name"], {"user_google_email": LOCAL_LOGIN}, _result("Soleon refused the call", True))
    assert len(out["content"]) == 2


def test_a_call_that_worked_is_returned_untouched():
    server = _Server()
    ok = _result("Created spreadsheet 1abc")
    out = server._with_identity_hint(TOOL["name"], {"user_google_email": LOCAL_LOGIN}, ok)
    assert out["content"] == [{"type": "text", "text": "Created spreadsheet 1abc"}]


def test_a_failure_with_no_identity_mismatch_is_returned_untouched():
    server = _Server()
    failed = _result("(MCP tool error: quota exceeded)")
    out = server._with_identity_hint(TOOL["name"], {"title": "x"}, failed)
    assert len(out["content"]) == 1
