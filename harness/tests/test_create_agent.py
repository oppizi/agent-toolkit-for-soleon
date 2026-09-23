"""skills/create-agent/assets/create_agent.py — the deterministic half of
/create-agent: the brief the interview produced is checked against the
contract, then rendered into the exact ordered MCP calls AND the plain-English
summary the person approves, from the same brief.

The load-bearing properties:
  * approval is decided by EFFECT, per integration — every write of an
    attached integration is gated, so no sibling tool is left open;
  * turning approval off needs the person's own words, recorded;
  * a read-only integration's write refs are OFF and would come back gated;
  * nothing creates an agent before the plan is valid;
  * the default model is read off the agents the person runs, never pinned;
  * evals are scores, never verdicts.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _local_emulation_fixtures import PLUGIN

ASSETS = PLUGIN / "skills" / "create-agent" / "assets"
sys.path.insert(0, str(ASSETS))
import create_agent as ca  # noqa: E402

CONTRACT = ca.load_contract(PLUGIN)

AGENTS = {"agents": [
    {"slug": "fund-raising-agent", "model": "us.anthropic.claude-sonnet-5"},
    {"slug": "demo-agent", "model": "us.anthropic.claude-sonnet-5"},
    {"slug": "kimi-bot", "model": "moonshotai.kimi-k2.5"},
    {"slug": "cheap-1", "model": "minimax.minimax-m2.5"},
    {"slug": "cheap-2", "model": "minimax.minimax-m2.5"},
    {"slug": "cheap-3", "model": "minimax.minimax-m2.5"},
]}

BRIEF = {
    "slug": "investor-inbox",
    "displayName": "Investor Inbox",
    "description": "Flags the investor emails that need a reply.",
    "model": "us.anthropic.claude-sonnet-5",
    "soul": "## Role\n\nYou triage the person's inbox for investor emails.\n",
    "integrations": [
        {"id": "gmail", "access": "read"},
        {"id": "google-sheets", "access": "write"},
    ],
    "evals": [{
        "name": "Flags a real investor reply",
        "inputs": [{"role": "user", "content": "Anything from investors today?"}],
        "weightedCriteria": [{"text": "Names the fund", "points": 60},
                             {"text": "Says what reply is needed", "points": 40}],
    }],
}


def _brief(**over):
    b = copy.deepcopy(BRIEF)
    b.update(over)
    return b


def _errors(brief, agents=None):
    return ca.validate(brief, CONTRACT, ca.agents_of(agents) if agents else None)[0]


def _calls(brief):
    return ca.plan(brief)["calls"]


# ---------------------------------------------------------------------------
# approval: by effect, never by tool name
# ---------------------------------------------------------------------------

def test_a_write_integration_gates_every_write_by_default():
    attach = next(c for c in _calls(_brief()) if c["step"] == "integration:google-sheets")
    assert attach["tool"] == "attach_mcp_server"
    assert attach["arguments"]["write_approval"] is True
    assert attach["arguments"]["server_id"] == "google-sheets"
    # no per-tool approval call exists at all: the gate is the integration's
    # WHOLE write side, so `create_spreadsheet` cannot be left open while
    # `create_sheet` is gated
    assert not [c for c in _calls(_brief()) if c["step"].startswith("integration:google-sheets:")]


def test_a_read_only_integration_switches_its_writes_off_and_leaves_them_gated():
    calls = _calls(_brief())
    attach = next(c for c in calls if c["step"] == "integration:gmail")
    # ON, so that re-enabling the write tools in the Tools page brings them
    # back behind approval rather than wide open
    assert attach["arguments"]["write_approval"] is True
    off = next(c for c in calls if c["step"] == "integration:gmail:read-only")
    assert off["tool"] == "set_agent_tool"
    assert off["arguments"] == {"slug": "investor-inbox", "app_env": "dev",
                                "tool_id": "mcp_gmail_write", "enabled": False}
    assert calls.index(off) == calls.index(attach) + 1


def test_approval_off_needs_the_person_s_own_words():
    b = _brief(integrations=[{"id": "hubspot", "access": "write", "writeApproval": False}])
    assert any("writeApprovalReason" in e for e in _errors(b))
    b["integrations"][0]["writeApprovalReason"] = "   "
    assert any("writeApprovalReason" in e for e in _errors(b))
    b["integrations"][0]["writeApprovalReason"] = "let it update deal stages on its own"
    assert _errors(b) == []
    attach = next(c for c in _calls(b) if c["step"] == "integration:hubspot")
    assert attach["arguments"]["write_approval"] is False


def test_approval_off_on_a_read_only_integration_is_a_contradiction():
    b = _brief(integrations=[{"id": "gmail", "access": "read", "writeApproval": False,
                              "writeApprovalReason": "whatever"}])
    assert any("only applies to access 'write'" in e for e in _errors(b))


def test_a_custom_mcp_server_uses_the_custom_ref_prefix():
    b = _brief(integrations=[{"id": "echo-server", "kind": "custom", "access": "read"}])
    calls = _calls(b)
    assert next(c for c in calls if c["step"] == "integration:echo-server")["arguments"]["kind"] == "custom"
    assert next(c for c in calls if c["step"] == "integration:echo-server:read-only")["arguments"]["tool_id"] \
        == "custom_echo-server_write"


# ---------------------------------------------------------------------------
# the summary the person approves is the plan that runs
# ---------------------------------------------------------------------------

def test_summary_states_read_change_and_ask_first_per_integration():
    lines = ca.plan(_brief())["summary"]
    assert "Gmail: can read, cannot change anything" in lines
    assert "Google Sheets: can read, and can make changes — every change asks you first" in lines
    assert "Tested against: Flags a real investor reply" in lines


def test_summary_quotes_the_person_when_approval_is_off():
    b = _brief(integrations=[{"id": "hubspot", "access": "write", "writeApproval": False,
                              "writeApprovalReason": "let it update deal stages"}])
    lines = ca.plan(b)["summary"]
    assert 'HubSpot: can read and make changes WITHOUT asking you (you said: "let it update deal stages")' in lines


def test_no_integrations_never_claims_the_agent_can_only_talk():
    """Every new agent gets the template's web search / web fetch / workspace
    files, none gated (seen live on dev). An earlier summary said an agent
    with no integrations "can only talk" while it could browse the web."""
    lines = ca.plan(_brief(integrations=[]))["summary"]
    assert "Integrations: none — it cannot read or change anything in your accounts" in lines
    assert not any("only talk" in l for l in lines)


def test_web_off_is_one_call_on_the_ref_that_owns_every_web_op():
    """`enabled: false` on `sys_web_prompt` makes the runtime skip both its
    collapsed and per-op registration — web_search, web_fetch and browser_*."""
    out = ca.plan(_brief(webAccess=False))
    off = [c for c in out["calls"] if c["step"] == "web:off"]
    assert off == [{"step": "web:off", "tool": "set_agent_tool",
                    "arguments": {"slug": "investor-inbox", "app_env": "dev",
                                  "tool_id": "sys_web_prompt", "enabled": False}}]
    assert out["calls"][-1]["tool"] == "validate_agent_draft"


def test_web_stays_on_unless_the_brief_says_false():
    for brief in (_brief(), _brief(webAccess=True)):
        assert not [c for c in ca.plan(brief)["calls"] if c["step"] == "web:off"]


def test_with_web_off_the_summary_never_claims_web_access():
    lines = ca.plan(_brief(webAccess=False))["summary"]
    assert lines[-1] == ca.BASELINE_NO_WEB_LINE
    assert lines[-1].startswith("Web: switched off")
    assert not any("search, read and browse the web" in l for l in lines)
    # the document tools are NOT behind the web switch — still named
    assert "Excel and PowerPoint" in lines[-1]


def test_web_access_must_be_a_boolean():
    assert any("webAccess" in e for e in _errors(_brief(webAccess="no")))


def test_every_summary_names_the_template_baseline_last():
    """Read off `list_agent_tools` on a fresh agent: web + browser, the
    .xlsx/.pptx makers and attach_file, workspace files — all approval:false.
    The document tools are not refs, so they were missed once already."""
    for brief in (_brief(), _brief(integrations=[])):
        lines = ca.plan(brief)["summary"]
        assert lines[-1] == ca.BASELINE_LINE
        for capability in ("search, read and browse the web", "Excel and PowerPoint",
                           "its own workspace", "without asking", "None of that uses your accounts"):
            assert capability in lines[-1], capability


@pytest.mark.parametrize("integrations", [
    [{"id": "gmail", "access": "read"}],
    [{"id": "gmail", "access": "write"}],
    [{"id": "gmail", "access": "write", "writeApproval": False, "writeApprovalReason": "yes really"}],
])
def test_summary_and_calls_agree_on_approval(integrations):
    """Rendered from one brief: the summary's claim about approval is exactly
    what attach_mcp_server + set_agent_tool will apply."""
    out = ca.plan(_brief(integrations=integrations))
    line = next(l for l in out["summary"] if l.startswith("Gmail:"))
    calls = {c["step"]: c for c in out["calls"]}
    gated = calls["integration:gmail"]["arguments"]["write_approval"]
    writes_off = "integration:gmail:read-only" in calls
    if writes_off:
        assert line == "Gmail: can read, cannot change anything"
    elif gated:
        assert line.endswith("every change asks you first")
    else:
        assert "WITHOUT asking you" in line


# ---------------------------------------------------------------------------
# the plan's shape
# ---------------------------------------------------------------------------

def test_create_comes_first_and_carries_the_agent():
    first = _calls(_brief())[0]
    assert first["tool"] == "create_agent"
    assert first["arguments"] == {
        "slug": "investor-inbox", "display_name": "Investor Inbox",
        "model": "us.anthropic.claude-sonnet-5", "framework": "maverick",
        "soul": BRIEF["soul"], "app_env": "dev",
        "description": "Flags the investor emails that need a reply.",
    }


def test_validation_is_the_last_call_and_deploy_is_held_back():
    out = ca.plan(_brief())
    assert out["calls"][-1]["tool"] == "validate_agent_draft"
    # the deploy is NOT a call the skill runs through — it waits for gate 2
    assert all(c["tool"] != "deploy_agent_draft" for c in out["calls"])
    assert out["deploy"] == {"step": "deploy", "tool": "deploy_agent_draft",
                             "arguments": {"slug": "investor-inbox", "app_env": "dev"}}


def test_every_call_targets_dev():
    for c in _calls(_brief(knowledgeBases=["handbook"],
                           skills=[{"name": "Weekly Digest", "content": "Do the digest."}])):
        assert c["arguments"]["app_env"] == "dev", c


def test_a_channel_rides_on_create():
    ci = "ci_" + "a" * 24
    assert _calls(_brief(channelInstanceId=ci))[0]["arguments"]["channel_instance_id"] == ci
    assert "channel_instance_id" not in _calls(_brief())[0]["arguments"]


def test_knowledge_bases_skills_and_evals_become_their_own_calls():
    b = _brief(knowledgeBases=["handbook"],
               skills=[{"name": "Weekly Digest", "description": "d", "content": "Do the digest."}])
    steps = [c["step"] for c in _calls(b)]
    assert "knowledge-base:handbook" in steps and "skill:weekly-digest" in steps
    assert "eval:flags-a-real-investor-reply" in steps
    ev = next(c for c in _calls(b) if c["tool"] == "put_standard_eval")["arguments"]["eval"]
    assert ev["enabled"] is True and ev["weightedCriteria"][0]["points"] == 60


DAN = "pn_baedd774105f40979259957c"
SAM = "pn_1111111111111111111111ab"


def _schedule(**over):
    s = {"name": "Morning triage", "cron": "0 8 * * 1-5", "timezone": "Europe/London",
         "prompt": "Triage the inbox.",
         "recipients": [{"personId": DAN, "name": "Danny Silva"}]}
    s.update(over)
    return s


def test_a_schedule_is_created_with_the_agent_not_handed_back():
    """Recipients used to be unobtainable, so the whole automation was handed back
    and the person re-entered a name, a cron, a timezone and a prompt the interview
    had already written. resolve_people supplies the ids; the plan builds it."""
    out = ca.plan(_brief(schedules=[_schedule()]))
    call = next(c for c in out["calls"] if c["tool"] == "put_agent_automation")
    assert call["arguments"]["automation"] == {
        "name": "Morning triage", "type": "schedule", "schedule": "0 8 * * 1-5",
        "timezone": "Europe/London", "prompt": "Triage the inbox.", "recipients": [DAN]}
    assert out["handoff"]["schedules"] == []


def test_the_automation_is_created_before_the_draft_is_validated():
    steps = [c["step"] for c in _calls(_brief(schedules=[_schedule()]))]
    assert steps.index("automation:morning-triage") < steps.index("validate")


def test_a_recipient_may_be_a_bare_id_and_duplicates_collapse():
    out = ca.plan(_brief(schedules=[_schedule(recipients=[DAN, {"personId": DAN}, SAM])]))
    call = next(c for c in out["calls"] if c["tool"] == "put_agent_automation")
    assert call["arguments"]["automation"]["recipients"] == [DAN, SAM]


def test_the_timezone_defaults_rather_than_being_omitted():
    """An absent timezone would leave "6am" meaning whatever the container thinks."""
    out = ca.plan(_brief(schedules=[_schedule(timezone=None)]))
    call = next(c for c in out["calls"] if c["tool"] == "put_agent_automation")
    assert call["arguments"]["automation"]["timezone"] == ca.DEFAULT_TIMEZONE


def test_delivery_channels_are_left_absent_so_every_attached_channel_gets_it():
    """Absent means "every channel this agent is attached to" — what a person
    means by "send it to me". An empty list for an env would run it nowhere."""
    out = ca.plan(_brief(schedules=[_schedule()]))
    call = next(c for c in out["calls"] if c["tool"] == "put_agent_automation")
    assert "deliveryChannels" not in call["arguments"]["automation"]


def test_the_summary_says_when_it_runs_and_who_for():
    line = next(l for l in ca.plan(_brief(schedules=[_schedule()]))["summary"]
                if l.startswith("Runs on its own"))
    assert "every weekday at 8:00" in line
    assert "Europe/London" in line and "0 8 * * 1-5" in line
    assert "Danny Silva" in line


@pytest.mark.parametrize("cron,words", [
    ("0 6 * * *", "every day at 6:00"),
    ("30 17 * * 1-5", "every weekday at 17:30"),
    ("0 9 * * 1", "every Monday at 9:00"),
    ("*/5 * * * *", "on a schedule"),      # unrecognised — never guessed at
    ("0 6 1 * *", "on a schedule"),        # monthly — not a shape we claim to read
    ("nonsense", "on a schedule"),
])
def test_cron_is_put_in_words_only_when_it_is_certain(cron, words):
    assert ca._cron_in_words(cron) == words


@pytest.mark.parametrize("recipients,needle", [
    (None, "needs recipients"),
    ([], "needs recipients"),
    (["dan@oppizi.com"], "must be a platform person id"),
    ([{"personId": "everyone"}], "must be a platform person id"),
    ([{"name": "Danny"}], "must be a platform person id"),
])
def test_a_schedule_without_real_recipients_is_refused(recipients, needle):
    """The platform refuses it too (AUTOMATION_RECIPIENTS_REQUIRED); failing here
    keeps "who is this for?" a question the interview can still ask."""
    errors = _errors(_brief(schedules=[_schedule(recipients=recipients)]), AGENTS)
    assert any(needle in e for e in errors), errors


def test_handoff_names_the_accounts_to_connect():
    assert ca.plan(_brief())["handoff"]["connect"] == ["Gmail", "Google Sheets"]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_the_canonical_brief_is_valid():
    assert _errors(_brief(), AGENTS) == []


@pytest.mark.parametrize("field,value,needle", [
    ("slug", "Investor Inbox", "slug"),
    ("slug", "fund-raising-agent", "already taken"),
    ("displayName", "  ", "displayName"),
    ("model", "", "model is required"),
    ("soul", "", "soul is required"),
    ("channelInstanceId", "ci_nope", "channelInstanceId"),
])
def test_bad_top_level_fields(field, value, needle):
    assert any(needle in e for e in _errors(_brief(**{field: value}), AGENTS))


def test_an_oversized_soul_is_refused():
    big = "x" * (CONTRACT["max_soul_bytes"] + 1)
    assert any("cap is" in e for e in _errors(_brief(soul=big)))


def test_integration_mistakes():
    assert any("access" in e for e in _errors(_brief(integrations=[{"id": "gmail"}])))
    assert any("listed twice" in e for e in _errors(_brief(integrations=[
        {"id": "gmail", "access": "read"}, {"id": "gmail", "access": "write"}])))
    assert any("not a valid integration id" in e for e in _errors(_brief(integrations=[
        {"id": "Gmail!", "access": "read"}])))


def test_an_unknown_integration_is_a_warning_not_an_error():
    """The catalogue is a hint — admins add integrations — so an unknown id is
    let through for attach_mcp_server to settle, loudly."""
    errors, warnings = ca.validate(_brief(integrations=[{"id": "notion", "access": "read"}]), CONTRACT)
    assert errors == []
    assert any("notion" in w for w in warnings)
    errors, warnings = ca.validate(_brief(integrations=[{"id": "am_877d4d0c32fc4720", "access": "read"}]), CONTRACT)
    assert errors == [] and warnings == []


def test_a_model_no_agent_runs_is_a_warning():
    errors, warnings = ca.validate(_brief(model="us.anthropic.claude-opus-5"), CONTRACT, ca.agents_of(AGENTS))
    assert errors == [] and any("not used by any agent" in w for w in warnings)


@pytest.mark.parametrize("key", ["scoringMode", "scoreThreshold"])
def test_an_eval_can_never_carry_a_verdict(key):
    ev = dict(BRIEF["evals"][0], **{key: "pass_fail" if key == "scoringMode" else 70})
    assert any("no pass/fail" in e for e in _errors(_brief(evals=[ev])))


def test_weighted_criteria_must_total_exactly_100():
    ev = copy.deepcopy(BRIEF["evals"][0])
    ev["weightedCriteria"][1]["points"] = 50
    assert any("total 110" in e for e in _errors(_brief(evals=[ev])))
    ev["weightedCriteria"][1]["points"] = True  # a bool is not points
    assert any("whole points" in e for e in _errors(_brief(evals=[ev])))


def test_an_eval_must_end_on_the_user_and_have_something_to_grade():
    ev = copy.deepcopy(BRIEF["evals"][0])
    ev["inputs"].append({"role": "agent", "content": "ok"})
    assert any("last input turn" in e for e in _errors(_brief(evals=[ev])))
    bare = {"name": "x", "inputs": [{"role": "user", "content": "hi"}]}
    assert any("to grade against" in e for e in _errors(_brief(evals=[bare])))


def test_schedule_mistakes():
    b = _brief(schedules=[{"name": "s", "cron": "0 8 * *", "prompt": ""}])
    errs = _errors(b)
    assert any("5 fields" in e for e in errs) and any("prompt" in e for e in errs)


# ---------------------------------------------------------------------------
# defaults + slugs
# ---------------------------------------------------------------------------

def test_default_model_is_the_claude_model_most_agents_run_on():
    """Minimax is used by MORE agents here, but /pull-agent — offered at the
    end — can only emulate Claude, so the most-used Claude model wins."""
    model, reason, in_use = ca.default_model(ca.agents_of(AGENTS))
    assert model == "us.anthropic.claude-sonnet-5"
    assert "2 of 6" in reason
    assert in_use[0] == {"model": "minimax.minimax-m2.5", "agents": 3}


def test_default_model_falls_back_then_admits_it_has_none():
    assert ca.default_model([{"model": "zai.glm-5"}])[0] == "zai.glm-5"
    model, reason, _ = ca.default_model([])
    assert model is None and "ask" in reason


def test_agents_of_accepts_every_envelope():
    rows = AGENTS["agents"]
    assert ca.agents_of(AGENTS) == rows
    assert ca.agents_of({"status": 200, "body": AGENTS}) == rows
    assert ca.agents_of(rows) == rows


def test_suggest_slug_avoids_taken_and_invalid():
    pat = re.compile(CONTRACT["slug_pattern"])
    assert ca.suggest_slug("Investor Inbox!", set(), pat) == "investor-inbox"
    assert ca.suggest_slug("Fund Raising Agent", {"fund-raising-agent"}, pat) == "fund-raising-agent-2"
    assert ca.suggest_slug("Fund Raising Agent", {"fund-raising-agent", "fund-raising-agent-2"}, pat) \
        == "fund-raising-agent-3"
    assert pat.match(ca.suggest_slug("x", set(), pat))
    assert pat.match(ca.suggest_slug("é" * 10, set(), pat))


def test_known_integrations_carry_no_test_fixture():
    assert "file-cred-test" not in ca.KNOWN_INTEGRATIONS


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _run(*args, cwd):
    return subprocess.run([sys.executable, str(ASSETS / "create_agent.py"), "--plugin-root", str(PLUGIN), *args],
                          capture_output=True, text=True, timeout=60, cwd=str(cwd))


def test_cli_plan_and_validate(tmp_path):
    (tmp_path / "brief.json").write_text(json.dumps(BRIEF))
    (tmp_path / "agents.json").write_text(json.dumps(AGENTS))
    proc = _run("plan", "--brief", "brief.json", "--agents", "agents.json", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["calls"][0]["tool"] == "create_agent" and out["summary"] and out["warnings"] == []


def test_cli_refuses_to_plan_an_invalid_brief(tmp_path):
    (tmp_path / "brief.json").write_text(json.dumps(dict(BRIEF, slug="fund-raising-agent")))
    (tmp_path / "agents.json").write_text(json.dumps(AGENTS))
    proc = _run("plan", "--brief", "brief.json", "--agents", "agents.json", cwd=tmp_path)
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["valid"] is False and "calls" not in out


def test_cli_defaults_and_suggest_slug(tmp_path):
    (tmp_path / "agents.json").write_text(json.dumps(AGENTS))
    d = json.loads(_run("defaults", "--agents", "agents.json", cwd=tmp_path).stdout)
    assert d["defaultModel"] == "us.anthropic.claude-sonnet-5"
    assert "fund-raising-agent" in d["takenSlugs"] and d["knownIntegrations"]["gmail"] == "Gmail"
    s = json.loads(_run("suggest-slug", "--name", "Demo Agent", "--agents", "agents.json", cwd=tmp_path).stdout)
    assert s == {"slug": "demo-agent-2"}


# ---------------------------------------------------------------------------
# the skill file
# ---------------------------------------------------------------------------

SKILL = (PLUGIN / "skills" / "create-agent" / "SKILL.md").read_text(encoding="utf-8")


def test_skill_frontmatter_names_it_and_says_when_to_use_it():
    m = re.match(r"^---\nname: create-agent\ndescription: (.+?)\n---\n", SKILL, re.S)
    assert m, SKILL[:300]
    assert "interview" in m.group(1).lower() and "deploy-agent" in m.group(1)


def test_skill_has_both_gates_and_never_deploys_on_its_own():
    assert "gate 1" in SKILL and "gate 2" in SKILL
    assert "Shall I create it?" in SKILL and "Deploy it to dev?" in SKILL
    assert "nothing in Step 6 runs before" in SKILL


def test_skill_looks_platform_facts_up_instead_of_guessing():
    """The interview's weak spot was "how does Soleon work" facts — there was no
    lookup, so they were guessed or turned into questions. Both reference tools
    are named, with the rule that a live observation beats a reference topic."""
    flat = " ".join(SKILL.split())  # the skill is hard-wrapped; assert on the prose
    for tool in ("search_system_reference", "get_system_reference_topic"):
        assert tool in SKILL, tool
    assert "the live observation wins" in flat
    assert "it can lag recent changes" in flat
    # and it cites what it repeats
    assert "System Reference, *Loop → Effort*" in flat


def test_skill_builds_the_schedule_instead_of_handing_it_back():
    """The whole automation used to be handed back because no tool could produce a
    recipient id. The skill must now name the tool that can, default the recipient
    to the person being interviewed, and never ask for an id."""
    flat = " ".join(SKILL.split())
    assert "resolve_people" in SKILL
    assert "A schedule runs for the person you are talking to." in flat
    assert '"What is your person id?"' in flat
    # the old instruction must be gone from every surface
    assert "you add this in Soleon" not in flat
    assert "which no tool can look up" not in flat


def test_skill_forbids_the_questionnaire():
    """The interview adapts to the description: there is no fixed question
    list, the aspects are NOT questions, and plumbing is never asked."""
    assert "are **not** questions" in SKILL
    assert "Stop as soon as nothing Critical is Missing" in SKILL
    assert "never a\nquestionnaire" in SKILL or "never a questionnaire" in SKILL
    for plumbing in ("model id", "slug", "tool id", "cron syntax"):
        assert plumbing in SKILL


# ---------------------------------------------------------------------------
# the link — where the person goes to see what was just made
# ---------------------------------------------------------------------------

def test_the_plan_carries_a_link_to_the_new_agent_on_the_agents_page():
    """Everything the person still has to do — connect an account, watch a run,
    change anything — happens on the Agents page, so finishing without the URL
    made them go and find it."""
    links = ca.plan(_brief(), "https://mcp-dev.oppizi.com/mcp")["links"]
    assert links["agent"] == "https://soleon-dev.oppizi.com/agents/investor-inbox/edit?env=dev"
    assert links["agents"] == "https://soleon-dev.oppizi.com/agents?env=dev"


def test_the_link_follows_the_server_the_toolkit_is_talking_to():
    """The SPA host and the MCP host are the same env under two names. A toolkit
    pointed at prod handing out a dev link would send the person to another
    deployment entirely."""
    prod = ca.soleon_links("https://mcp.oppizi.com/mcp", "investor-inbox")
    assert prod["agent"].startswith("https://soleon.oppizi.com/agents/investor-inbox/edit")
    branch = ca.soleon_links("https://mcp-ahp-889.oppizi.com/mcp", "x")
    assert branch["agents"].startswith("https://soleon-ahp-889.oppizi.com/agents")
    # The base domain is a parameter of the platform's own rule (client installs
    # carry their own), so it is derived too rather than pinned to oppizi.com.
    other = ca.soleon_links("https://mcp-dev.acme.example/mcp", "x")
    assert other["agent"].startswith("https://soleon-dev.acme.example/agents/x/edit")


@pytest.mark.parametrize("url", [
    None, "", "not a url",
    "https://abc123.execute-api.us-east-1.amazonaws.com/prod/mcp",  # the raw API-GW URL
    "http://localhost:8080/mcp",
])
def test_a_host_outside_the_rule_gets_no_link_rather_than_a_guess(url):
    """A wrong link reads as authoritative: it 404s, or it opens someone else's
    env. Saying "open it from your Agents page" is the honest answer."""
    assert ca.soleon_links(url, "investor-inbox") == {}


def test_the_plan_without_a_server_url_simply_has_no_links():
    assert ca.plan(_brief())["links"] == {}


def test_the_skill_gives_the_link_and_never_invents_one():
    flat = " ".join(SKILL.split())
    assert "--server-url \"$SOLEON_MCP_URL\"" in flat
    assert "links.agent" in flat
    assert "give NO URL rather than a guessed one" in flat
