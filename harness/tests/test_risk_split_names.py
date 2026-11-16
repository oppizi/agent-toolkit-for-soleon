"""AHP-1477 WP-H T2 — the plugin's docs name the risk-split tools, and the
release note carries the old-rule → new-rule table and paste-ready rules.

The platform (agent-infra, AHP-1477 spec §4) retires 15 combined tools and
replaces each with tools of one risk level each: development-tier names end in
`_development` / `_to_staging`, production-tier names in `_production` /
`_everywhere`. A builder's Claude Code rules match tool NAMES only, so:

1. nothing this repo ships or publishes (skills, plugin README, TOOLS.md, the
   site) may still tell a model or a person to call a retired name — the call
   would be refused, and a rule written from it matches nothing;
2. the release note maps every retired name, in the plugin-scoped form Claude
   Code rules use (`mcp__plugin_soleon_soleon__<tool>`), to every tool that
   replaces it;
3. its two paste-ready snippets (headless `claude -p`, and the desk) put every
   production-tier tool behind `deny` / `ask` and allow the development tier.
   The headless `deny` lists the production tier by EXACT name (set equality
   below), so a job's hard refusal never depends on a glob; the desk snippet's
   globs sit in the tool position after the literal server prefix, which
   Claude Code 2.1.325's rule validator accepts in allow, ask and deny rules
   (it gives `mcp__github__get_*` as its example). `fnmatch` stands in for
   Claude Code's matcher only for those globs.

The §4 closed set is copied here on purpose: the server repo is not vendored,
and a drift between this map and the server's is exactly what the release
note must not carry.
"""
import fnmatch
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NOTE = ROOT / "docs" / "release-notes" / "AHP-1477.md"
PREFIX = "mcp__plugin_soleon_soleon__"

# Spec §4: retired name -> the tools that replace it (development tier first).
RETIRED = {
    "promote_agent": ["promote_agent_to_staging", "promote_agent_to_production"],
    "set_agent_active": ["set_agent_active_development", "set_agent_active_production"],
    "create_user_automation": ["create_user_automation_development", "create_user_automation_production"],
    "update_user_automation": ["update_user_automation_development", "update_user_automation_production"],
    "delete_user_automation": ["delete_user_automation_development", "delete_user_automation_production"],
    "bind_agent_channel": ["bind_agent_channel_development", "bind_agent_channel_production"],
    "unbind_agent_channel": ["unbind_agent_channel_development", "unbind_agent_channel_production"],
    "set_agent_channel_matchers": ["set_agent_channel_matchers_development",
                                   "set_agent_channel_matchers_production"],
    "publish_custom_mcp": ["publish_custom_mcp_development", "publish_custom_mcp_production"],
    "set_custom_mcp_lifecycle": ["set_custom_mcp_lifecycle_development", "set_custom_mcp_lifecycle_production"],
    "promote_custom_mcp": ["promote_custom_mcp_to_staging", "promote_custom_mcp_to_production"],
    "update_custom_mcp_connection": ["update_custom_mcp_connection_everywhere"],
    "patch_tool_policy": ["patch_tool_policy_development", "patch_tool_policy_everywhere"],
    "set_tool_argument_binding": ["set_tool_argument_binding_development", "set_tool_argument_binding_everywhere"],
    "run_eval": ["run_eval_checks", "run_eval_suite"],
}
# Spec §4: new names that retire nothing (the kept tool changes behaviour).
ADDED = {"set_ticket_stage": ["launch_ticket_production"],
         "call_agent_tool": ["call_approved_agent_tool"]}

NEW = sorted({n for names in list(RETIRED.values()) + list(ADDED.values()) for n in names})
PRODUCTION = sorted(n for n in NEW if n.endswith(("_production", "_everywhere"))) + ["call_approved_agent_tool"]
DEVELOPMENT = sorted(n for n in NEW if n.endswith(("_development", "_to_staging")))
# A cost split, not a risk tier (spec G-Q2): in neither glob family.
NEITHER = ["run_eval_checks", "run_eval_suite"]

RETIRED_RE = re.compile(r"\b(" + "|".join(sorted(RETIRED, key=len, reverse=True)) + r")\b")


def test_closed_set_counts():
    """The spec's own arithmetic: 31 new names, 15 retired."""
    assert len(RETIRED) == 15
    assert len(NEW) == 31
    assert set(PRODUCTION) | set(DEVELOPMENT) | set(NEITHER) == set(NEW)
    assert not (set(PRODUCTION) & set(DEVELOPMENT))


def _shipped_docs():
    files = [ROOT / "TOOLS.md", ROOT / "README.md", ROOT / "CLAUDE.md", ROOT / "docs" / "index.html"]
    plugin = ROOT / "plugins" / "soleon"
    files += sorted(p for p in plugin.rglob("*") if p.is_file() and p.suffix in {".md", ".py", ".json", ".html"})
    return files


def test_no_shipped_doc_names_a_retired_tool():
    hits = []
    for path in _shipped_docs():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = RETIRED_RE.search(line)
            if m:
                hits.append("{}:{}: {}".format(path.relative_to(ROOT), i, m.group(1)))
    assert not hits, "retired tool names still shipped:\n" + "\n".join(hits)


def test_sweep_negative_control():
    """The sweep sees a retired name, and not a new name that extends it."""
    assert RETIRED_RE.search("then call `promote_agent(slug)`")
    assert RETIRED_RE.search("the platform's run_eval starts")
    assert not RETIRED_RE.search("promote_agent_to_staging and run_eval_checks")
    assert not RETIRED_RE.search("update_custom_mcp_connection_everywhere")


def _note():
    assert NOTE.is_file(), "release note missing: {}".format(NOTE)
    return NOTE.read_text(encoding="utf-8")


@pytest.mark.parametrize("old", sorted(RETIRED))
def test_release_note_maps_every_retired_name(old):
    rows = [l for l in _note().splitlines()
            if l.startswith("|") and "`{}{}`".format(PREFIX, old) in l]
    assert rows, "no old-rule → new-rule row for {}".format(old)
    for new in RETIRED[old]:
        assert any("`{}{}`".format(PREFIX, new) in r for r in rows), \
            "{}'s row does not name {}{}".format(old, PREFIX, new)


@pytest.mark.parametrize("kept", sorted(ADDED))
def test_release_note_names_added_tools(kept):
    note = _note()
    for new in ADDED[kept]:
        assert "`{}{}`".format(PREFIX, new) in note


def _snippets():
    """The note's ```json blocks, keyed by the `###` heading above each."""
    out, heading = {}, None
    for block in re.finditer(r"^### ([^\n]+)$|^```json\n(.*?)^```", _note(), re.M | re.S):
        if block.group(1):
            heading = block.group(1).strip().lower()
        elif heading is not None:
            out[heading] = json.loads(block.group(2))
    return out


def _pick(word):
    found = [v for k, v in _snippets().items() if word in k]
    assert len(found) == 1, "expected one ```json snippet under a '### …{}…' heading".format(word)
    perms = found[0]["permissions"]
    return {k: perms.get(k, []) for k in ("allow", "ask", "deny")}


def _matches(rules, tool):
    return any(fnmatch.fnmatchcase(PREFIX + tool, r) for r in rules)


@pytest.mark.parametrize("which,gate", [("headless", "deny"), ("desk", "ask")])
def test_snippet_gates_every_production_tool(which, gate):
    rules = _pick(which)
    for r in rules["allow"] + rules["ask"] + rules["deny"]:
        assert r.startswith(PREFIX), "{} is not plugin-scoped".format(r)
    for tool in PRODUCTION:
        assert _matches(rules[gate], tool), "{}: {} not under {}".format(which, tool, gate)
        assert not _matches(rules["allow"], tool), "{}: allow matches {}".format(which, tool)
    for tool in DEVELOPMENT:
        assert _matches(rules["allow"], tool), "{}: {} not allowed".format(which, tool)
        assert not _matches(rules[gate], tool), "{}: {} gates development {}".format(which, gate, tool)


def test_snippet_glob_negative_control():
    """The matcher is not vacuous: the production glob misses a development
    tool, and a missing glob is caught."""
    assert _matches([PREFIX + "*_production"], "promote_agent_to_production")
    assert not _matches([PREFIX + "*_production"], "promote_agent_to_staging")
    assert not _matches([PREFIX + "*_production"], "patch_tool_policy_everywhere")
    assert not _matches(["mcp__plugin_soleon_other__*"], "run_eval_checks")


def _decide(rules, tool):
    """Claude Code's precedence: deny, then ask, then allow; else not allowed."""
    for verdict in ("deny", "ask", "allow"):
        if _matches(rules[verdict], tool):
            return verdict
    return None


def test_headless_deny_is_the_production_tier_by_exact_name():
    rules = _pick("headless")
    assert set(rules["deny"]) == {PREFIX + t for t in PRODUCTION}
    assert not any("*" in r for r in rules["deny"])
    assert set(DEVELOPMENT) <= {r[len(PREFIX):] for r in rules["allow"]}


@pytest.mark.parametrize("which", ["headless", "desk"])
def test_snippets_leave_the_cost_split_and_the_unapproved_call_alone(which):
    rules = _pick(which)
    for tool in NEITHER + ["call_agent_tool"]:
        assert not _matches(rules["deny"] + rules["ask"], tool), "{}: {} gated".format(which, tool)
    if which == "headless":
        assert _decide(rules, "run_eval_suite") is None, "the uncapped suite is left out on purpose"
        assert _decide(rules, "run_eval_checks") == "allow"

