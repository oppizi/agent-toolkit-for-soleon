#!/usr/bin/env python3
"""Deterministic core of `/create-agent` — everything about CREATING a Soleon
agent that must not depend on a model getting a payload shape right.

The interview is the skill's job: an LLM talking to a person. This script owns
what comes after it — checking the brief the interview produced against the
platform contract, then turning it into the exact, ordered MCP calls. The plan
that runs and the summary the person approved are both rendered from the SAME
brief, so they cannot disagree about what the agent may read, change, or has
to ask about first.

    defaults     --agents list_agents.json
                 → {takenSlugs, defaultModel, defaultModelReason, modelsInUse}
    suggest-slug --name "Display Name" [--agents list_agents.json]
                 → {slug}
    validate     --brief brief.json [--agents list_agents.json]
                 → {valid, errors, warnings}; exit 1 when invalid
    plan         --brief brief.json [--agents list_agents.json]
                 [--server-url https://mcp-dev.oppizi.com/mcp]
                 → {calls, deploy, handoff, summary, links}; exit 1 when invalid

`list_agents.json` is the raw answer of the MCP tool `list_agents(app_env="dev")`.

Stdlib only; Python 3.9+. Never touches the network — the skill makes every call.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

APP_ENV = "dev"
FRAMEWORK = "maverick"
AUTOMATION_PROMPT_MAX = 4096
#: A schedule runs once for each NAMED person — there is no "everyone" and no
#: group (the platform's automation_contract.selected_recipients). Ids come from
#: the MCP tool resolve_people, which is the only way to obtain one.
PERSON_ID_RE = re.compile(r"^pn_[0-9a-f]{24}$")
DEFAULT_TIMEZONE = "America/New_York"
CHANNEL_INSTANCE_RE = re.compile(r"^ci_[0-9a-f]{24}$")
# Tool Library ids are kebab-case (`google-sheets`); admin-authored instances
# are `am_<hex>`; custom MCP servers are slugs. One pattern admits all three.
SERVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
ACCESS = ("read", "write")
KINDS = ("mcp", "custom")

#: The standard Tool Library, as seeded on the platform (snapshot 2026-09-22,
#: `scripts/seed_standard_integrations.py`, cross-checked against the
#: integrations actually attached on dev). There is NO MCP tool that lists the
#: catalogue, so this is how the interview maps "read my email" to `gmail`.
#: It is a HINT, never a gate: an id outside it is only a warning, because
#: admins add integrations (`am_…`) and `attach_mcp_server` refuses an id that
#: does not exist — a stale entry fails loud, never silently.
KNOWN_INTEGRATIONS = {
    "airops": "AirOps",
    "clay": "Clay",
    "confluence": "Confluence",
    "gmail": "Gmail",
    "google-analytics": "Google Analytics",
    "google-calendar": "Google Calendar",
    "google-contacts": "Google Contacts",
    "google-docs": "Google Docs",
    "google-drive": "Google Drive",
    "google-forms": "Google Forms",
    "google-maps": "Google Maps",
    "google-search-console": "Google Search Console",
    "google-sheets": "Google Sheets",
    "google-slides": "Google Slides",
    "google-tasks": "Google Tasks",
    "hubspot": "HubSpot",
    "lemlist": "lemlist",
    "rb2b": "RB2B",
    "slack": "Slack",
    "strapi": "Strapi",
}

#: Evals yield a 0-100 score and nothing else — there is no pass/fail mode and
#: no threshold on the platform, and a stored threshold once got a graded
#: rubric reported to a person as an outright "fail". Refused by name so no
#: brief can bring them back.
FORBIDDEN_EVAL_KEYS = ("scoringMode", "scoreThreshold")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _plugin_root(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[3]


def load_contract(plugin_root: Path) -> Dict[str, Any]:
    with open(str(plugin_root / "contract.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def agents_of(raw: Any) -> List[Dict[str, Any]]:
    """The agent rows out of a raw `list_agents` answer, whichever envelope."""
    body = raw
    if isinstance(body, dict) and "body" in body and "status" in body:
        body = body["body"]
    if isinstance(body, dict):
        body = body.get("agents", body.get("items", []))
    if not isinstance(body, list):
        raise SystemExit("list_agents answer carries no agent list")
    return [a for a in body if isinstance(a, dict)]


# ---------------------------------------------------------------------------
# defaults / suggest-slug
# ---------------------------------------------------------------------------

def default_model(agents: List[Dict[str, Any]]) -> Tuple[Optional[str], str, List[Dict[str, Any]]]:
    """The model to propose when the person names none.

    Read off the agents the person can already see, never hard-coded: a pinned
    id goes stale (the toolkit's own `model_alias_map` still says Sonnet 4.6),
    while a model the platform is running right now is both current and known
    to deploy. Anthropic first, because `/pull-agent` — offered at the end —
    can only emulate Claude models locally.
    """
    counts: Dict[str, int] = {}
    for a in agents:
        m = a.get("model")
        if isinstance(m, str) and m.strip():
            counts[m] = counts.get(m, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    in_use = [{"model": m, "agents": n} for m, n in ranked]
    anthropic = [(m, n) for m, n in ranked if "anthropic" in m]
    if anthropic:
        m, n = anthropic[0]
        return m, "the Claude model most of your agents run on ({} of {})".format(n, len(agents)), in_use
    if ranked:
        m, n = ranked[0]
        return m, "the model most of your agents run on ({} of {})".format(n, len(agents)), in_use
    return None, "no agent you can see declares a model — ask which to use", in_use


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)[:64].strip("-")


def suggest_slug(name: str, taken: set, pattern: re.Pattern) -> str:
    base = slugify(name) or "agent"
    if len(base) < 2:
        base = (base + "-agent").strip("-")
    candidate, n = base, 2
    while candidate in taken or not pattern.match(candidate):
        suffix = "-{}".format(n)
        candidate = base[: 64 - len(suffix)].rstrip("-") + suffix
        n += 1
    return candidate


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def _nonempty(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _integrations(brief: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [i for i in brief.get("integrations") or [] if isinstance(i, dict)]


def validate(brief: Any, contract: Dict[str, Any],
             agents: Optional[List[Dict[str, Any]]] = None) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(brief, dict):
        return ["the brief must be a JSON object"], warnings

    slug_re = re.compile(contract["slug_pattern"])
    slug = brief.get("slug")
    if not isinstance(slug, str) or not slug_re.match(slug):
        errors.append("slug {!r} must match {}".format(slug, contract["slug_pattern"]))
    elif agents is not None and slug in {a.get("slug") for a in agents}:
        errors.append("slug {!r} is already taken — pick another (suggest-slug finds a free one)".format(slug))

    name = brief.get("displayName")
    max_name = contract["display_name"]["max_len"]
    if not _nonempty(name):
        errors.append("displayName is required")
    elif len(name) > max_name:
        errors.append("displayName is longer than {} characters".format(max_name))

    if not _nonempty(brief.get("model")):
        errors.append("model is required (run `defaults` for the one your agents use)")
    elif agents is not None and brief["model"] not in {a.get("model") for a in agents}:
        warnings.append("model {!r} is not used by any agent you can see; create_agent refuses an id "
                        "the catalogue does not have, and its refusal lists the valid ones".format(brief["model"]))

    soul = brief.get("soul")
    if not _nonempty(soul):
        errors.append("soul is required — the agent's instructions")
    elif len(soul.encode("utf-8")) > contract["max_soul_bytes"]:
        errors.append("soul is {} bytes; the cap is {}".format(len(soul.encode("utf-8")), contract["max_soul_bytes"]))

    if brief.get("description") is not None and not isinstance(brief.get("description"), str):
        errors.append("description must be a string")

    if "webAccess" in brief and not isinstance(brief["webAccess"], bool):
        errors.append("webAccess must be true or false")

    ci = brief.get("channelInstanceId")
    if ci is not None and not (isinstance(ci, str) and CHANNEL_INSTANCE_RE.match(ci)):
        errors.append("channelInstanceId {!r} must look like ci_ followed by 24 hex characters".format(ci))
    elif ci and not str(brief.get("channelName") or "").strip():
        # A warning, not an error: the bind works either way. But the person
        # approves a summary, and `ci_9f3…` tells them nothing about which
        # workspace they just wired their agent into.
        warnings.append("channelInstanceId is set with no channelName — the summary can only show the id")

    _validate_integrations(brief, errors, warnings)
    _validate_knowledge_bases(brief, errors)
    _validate_skills(brief, contract, errors)
    _validate_evals(brief, contract, errors)
    _validate_schedules(brief, errors)
    return errors, warnings


def _validate_integrations(brief: Dict[str, Any], errors: List[str], warnings: List[str]) -> None:
    raw = brief.get("integrations")
    if raw is None:
        return
    if not isinstance(raw, list):
        errors.append("integrations must be a list")
        return
    seen = set()
    for i, entry in enumerate(raw):
        where = "integrations[{}]".format(i)
        if not isinstance(entry, dict):
            errors.append("{} must be an object".format(where))
            continue
        sid = entry.get("id")
        if not (isinstance(sid, str) and SERVER_ID_RE.match(sid)):
            errors.append("{}.id {!r} is not a valid integration id".format(where, sid))
            continue
        if sid in seen:
            errors.append("integration {!r} is listed twice".format(sid))
        seen.add(sid)
        kind = entry.get("kind", "mcp")
        if kind not in KINDS:
            errors.append("{}.kind must be one of {}".format(where, "/".join(KINDS)))
        access = entry.get("access")
        if access not in ACCESS:
            errors.append("{} ({}): access must be 'read' or 'write'".format(where, sid))
        approval = entry.get("writeApproval", True)
        if not isinstance(approval, bool):
            errors.append("{} ({}): writeApproval must be true or false".format(where, sid))
        elif approval is False:
            # Approval is the ONLY control on this path that cannot be talked
            # past — a SOUL sentence is not. Turning it off must be the
            # person's explicit call, recorded in their own words.
            if access != "write":
                errors.append("{} ({}): writeApproval only applies to access 'write'".format(where, sid))
            elif not _nonempty(entry.get("writeApprovalReason")):
                errors.append("{} ({}): writeApproval false needs writeApprovalReason — the person's own "
                              "words asking for changes without approval".format(where, sid))
        if kind == "mcp" and sid not in KNOWN_INTEGRATIONS and not sid.startswith("am_"):
            warnings.append("integration {!r} is not in the known list; attach_mcp_server refuses it if "
                            "it does not exist".format(sid))


def _validate_knowledge_bases(brief: Dict[str, Any], errors: List[str]) -> None:
    raw = brief.get("knowledgeBases")
    if raw is None:
        return
    if not isinstance(raw, list) or not all(isinstance(k, str) and SERVER_ID_RE.match(k) for k in raw):
        errors.append("knowledgeBases must be a list of knowledge-base slugs")
    elif len(set(raw)) != len(raw):
        errors.append("a knowledge base is listed twice")


def _validate_skills(brief: Dict[str, Any], contract: Dict[str, Any], errors: List[str]) -> None:
    raw = brief.get("skills")
    if raw is None:
        return
    if not isinstance(raw, list):
        errors.append("skills must be a list")
        return
    if len(raw) > contract["max_skills_per_agent"]:
        errors.append("{} skills; the cap is {}".format(len(raw), contract["max_skills_per_agent"]))
    skill_re = re.compile(contract["skill_slug_pattern"])
    ids = set()
    for i, s in enumerate(raw):
        where = "skills[{}]".format(i)
        if not isinstance(s, dict) or not _nonempty(s.get("name")):
            errors.append("{} needs a name".format(where))
            continue
        sid = slugify(s["name"])
        if not skill_re.match(sid):
            errors.append("{}: name {!r} does not make a valid skill id".format(where, s["name"]))
        if sid in ids:
            errors.append("{}: two skills share the id {!r}".format(where, sid))
        ids.add(sid)
        if not _nonempty(s.get("content")):
            errors.append("{} ({}) has no content".format(where, s["name"]))
        elif len(s["content"].encode("utf-8")) > contract["max_skill_bytes"]:
            errors.append("{} ({}) is over {} bytes".format(where, s["name"], contract["max_skill_bytes"]))


def _validate_evals(brief: Dict[str, Any], contract: Dict[str, Any], errors: List[str]) -> None:
    raw = brief.get("evals")
    if raw is None:
        return
    if not isinstance(raw, list):
        errors.append("evals must be a list")
        return
    roles = set(contract["eval_turn_roles"])
    names = set()
    for i, e in enumerate(raw):
        where = "evals[{}]".format(i)
        if not isinstance(e, dict):
            errors.append("{} must be an object".format(where))
            continue
        name = e.get("name")
        if not _nonempty(name) or len(name) > contract["eval_name_max"]:
            errors.append("{} needs a name of 1-{} characters".format(where, contract["eval_name_max"]))
        elif name in names:
            errors.append("two evals are named {!r}".format(name))
        names.add(name)
        for key in FORBIDDEN_EVAL_KEYS:
            if key in e:
                errors.append("{}: {} does not exist — an eval is a 0-100 score with no pass/fail".format(where, key))
        inputs = e.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            errors.append("{} needs at least one input turn".format(where))
        else:
            for t in inputs:
                if not isinstance(t, dict) or t.get("role") not in roles or not _nonempty(t.get("content")):
                    errors.append("{}: every input turn needs role {} and content".format(where, "/".join(sorted(roles))))
                    break
            else:
                if inputs[-1].get("role") != "user":
                    errors.append("{}: the last input turn must be the user's".format(where))
        crit = e.get("weightedCriteria")
        if crit is not None:
            _validate_weighted(where, crit, errors)
        if crit is None and not _nonempty(e.get("evaluationCriteria")) and not _nonempty(e.get("expectedOutput")):
            errors.append("{}: needs weightedCriteria, evaluationCriteria or expectedOutput to grade against".format(where))


def _validate_weighted(where: str, crit: Any, errors: List[str]) -> None:
    if not isinstance(crit, list) or not 1 <= len(crit) <= 20:
        errors.append("{}: weightedCriteria needs 1-20 criteria".format(where))
        return
    total = 0
    for c in crit:
        pts = c.get("points") if isinstance(c, dict) else None
        if not isinstance(c, dict) or not _nonempty(c.get("text")) or isinstance(pts, bool) \
                or not isinstance(pts, int) or not 0 <= pts <= 100:
            errors.append("{}: every criterion needs text and whole points 0-100".format(where))
            return
        total += pts
    if total != 100:
        errors.append("{}: criteria points total {}, they must total exactly 100".format(where, total))


def _validate_schedules(brief: Dict[str, Any], errors: List[str]) -> None:
    raw = brief.get("schedules")
    if raw is None:
        return
    if not isinstance(raw, list):
        errors.append("schedules must be a list")
        return
    for i, s in enumerate(raw):
        where = "schedules[{}]".format(i)
        if not isinstance(s, dict) or not _nonempty(s.get("name")):
            errors.append("{} needs a name".format(where))
            continue
        if not isinstance(s.get("cron"), str) or len(s["cron"].split()) != 5:
            errors.append("{} ({}): cron must have 5 fields".format(where, s["name"]))
        if not _nonempty(s.get("prompt")):
            errors.append("{} ({}): needs the prompt it runs".format(where, s["name"]))
        elif len(s["prompt"].encode("utf-8")) > AUTOMATION_PROMPT_MAX:
            errors.append("{} ({}): prompt is over {} bytes".format(where, s["name"], AUTOMATION_PROMPT_MAX))
        _validate_recipients(s.get("recipients"), where, s["name"], errors)


def _validate_recipients(raw: Any, where: str, name: str, errors: List[str]) -> None:
    """A schedule with nobody to send to cannot be created — the platform refuses
    it (AUTOMATION_RECIPIENTS_REQUIRED), so catching it here keeps the failure in
    the interview, where the question "who is this for?" can still be asked."""
    if not isinstance(raw, list) or not raw:
        errors.append("{} ({}): needs recipients — the people it runs for. Get their ids from "
                      "resolve_people (a bare call returns the person you are talking to)."
                      .format(where, name))
        return
    for j, person in enumerate(raw):
        pid = person.get("personId") if isinstance(person, dict) else person
        if not isinstance(pid, str) or not PERSON_ID_RE.match(pid):
            errors.append("{} ({}): recipients[{}] must be a platform person id (pn_…) from "
                          "resolve_people, not an email, a name or a group."
                          .format(where, name, j))


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def _write_ref(entry: Dict[str, Any]) -> str:
    prefix = "custom" if entry.get("kind", "mcp") == "custom" else "mcp"
    return "{}_{}_write".format(prefix, entry["id"])


def display_of(entry: Dict[str, Any]) -> str:
    return KNOWN_INTEGRATIONS.get(entry["id"], entry["id"])


#: The Soleon SPA and the MCP server are two vanity hostnames over the SAME
#: deployment, and each is a pure function of the env name: `mcp[-{env}].{base}`
#: and `soleon[-{env}].{base}` (the platform's `stacks/_mcp_naming.py` and
#: `stacks/ui_stack.py`, which mirror each other deliberately — prod drops the
#: suffix, every other env carries it). So the page the person opens is DERIVED
#: from the server this toolkit is connected to, never assumed.
_MCP_HOST_RE = re.compile(r"^mcp(?:-(?P<env>[a-z0-9-]+))?\.(?P<base>[a-z0-9-]+(?:\.[a-z0-9-]+)+)$")


def soleon_links(server_url: Optional[str], slug: str) -> Dict[str, str]:
    """Where to see the agent afterwards: `{agent, agents}`, or `{}` when the
    server's hostname is not one this rule covers.

    Nothing is guessed. A toolkit pointed at prod must not hand out a dev link,
    and a host that doesn't match (a tunnel, a localhost dev server, an
    execute-api URL) gets NO link rather than a plausible one — a wrong link
    reads as authoritative and either 404s or opens somebody else's env.
    """
    match = _MCP_HOST_RE.match((urlsplit(server_url or "").hostname or "").lower())
    if not match:
        return {}
    env, base = match.group("env"), match.group("base")
    spa = "soleon.{}".format(base) if env is None else "soleon-{}.{}".format(env, base)
    # `?env=` is the SPA's app-env selector and the shareable form it stamps on
    # every route (`ui/src/state/EnvContext.tsx`). APP_ENV is the partition every
    # call in the plan writes to, so the link opens the agent that was just made.
    return {"agent": "https://{}/agents/{}/edit?env={}".format(spa, slug, APP_ENV),
            "agents": "https://{}/agents?env={}".format(spa, APP_ENV)}


def plan(brief: Dict[str, Any], server_url: Optional[str] = None) -> Dict[str, Any]:
    slug = brief["slug"]
    base = {"slug": slug, "app_env": APP_ENV}
    calls: List[Dict[str, Any]] = []

    create_args: Dict[str, Any] = {
        "slug": slug, "display_name": brief["displayName"], "model": brief["model"],
        "framework": FRAMEWORK, "soul": brief["soul"], "app_env": APP_ENV,
    }
    if _nonempty(brief.get("description")):
        create_args["description"] = brief["description"]
    if brief.get("channelInstanceId"):
        create_args["channel_instance_id"] = brief["channelInstanceId"]
    calls.append({"step": "create", "tool": "create_agent", "arguments": create_args})

    for entry in _integrations(brief):
        read_only = entry["access"] == "read"
        # A read-only integration still attaches with write approval ON: the
        # write refs are switched off below, and if anyone later switches them
        # back on in the Tools page they come back gated, not wide open.
        approval = True if read_only else entry.get("writeApproval", True)
        calls.append({"step": "integration:{}".format(entry["id"]), "tool": "attach_mcp_server",
                      "arguments": dict(base, server_id=entry["id"], kind=entry.get("kind", "mcp"),
                                        write_approval=approval)})
        if read_only:
            calls.append({"step": "integration:{}:read-only".format(entry["id"]), "tool": "set_agent_tool",
                          "arguments": dict(base, tool_id=_write_ref(entry), enabled=False)})

    if brief.get("webAccess") is False:
        # ONE ref: `enabled: false` on `sys_web_prompt` makes the runtime skip
        # both its collapsed and its per-op registration paths, so web_search,
        # web_fetch and every browser_* op go together (containers/shared/
        # mcp_server.py, the per-ref enable gate — the Tools page's own toggle).
        calls.append({"step": "web:off", "tool": "set_agent_tool",
                      "arguments": dict(base, tool_id=WEB_REF, enabled=False)})

    for kb in brief.get("knowledgeBases") or []:
        calls.append({"step": "knowledge-base:{}".format(kb), "tool": "attach_knowledge_base",
                      "arguments": dict(base, kb_slug=kb)})

    for s in brief.get("skills") or []:
        skill = {"name": s["name"], "description": s.get("description") or "",
                 "content": s["content"], "enabled": True}
        calls.append({"step": "skill:{}".format(slugify(s["name"])), "tool": "put_agent_skill",
                      "arguments": dict(base, skill=skill)})

    for e in brief.get("evals") or []:
        ev = {k: e[k] for k in ("name", "inputs", "expectedOutput", "weightedCriteria",
                                "evaluationCriteria", "expectedToolCalls") if k in e}
        ev["enabled"] = True
        calls.append({"step": "eval:{}".format(slugify(e["name"])), "tool": "put_standard_eval",
                      "arguments": dict(base, eval=ev)})

    for s in brief.get("schedules") or []:
        # One automation per schedule, created up front on the draft and deployed
        # with everything else. `recipients` is the only field that cannot be
        # written from the conversation — the interview resolves it through
        # resolve_people. deliveryChannels is deliberately omitted: absent means
        # "every channel this agent is attached to", which is what a person means
        # by "send it to me" whether they read it in Soleon chat or Slack.
        calls.append({"step": "automation:{}".format(slugify(s["name"])), "tool": "put_agent_automation",
                      "arguments": dict(base, automation={
                          "name": s["name"], "type": "schedule", "schedule": s["cron"],
                          "timezone": s.get("timezone") or DEFAULT_TIMEZONE,
                          "prompt": s["prompt"], "recipients": _recipient_ids(s)})})

    calls.append({"step": "validate", "tool": "validate_agent_draft", "arguments": dict(base)})

    return {
        "calls": calls,
        "deploy": {"step": "deploy", "tool": "deploy_agent_draft", "arguments": dict(base)},
        "handoff": handoff(brief),
        "summary": summary(brief),
        "links": soleon_links(server_url, slug),
    }


_DAY_NAMES = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
              "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday"}


def _cron_in_words(cron: str) -> str:
    """Plain English for the shapes an interview actually produces, and NOTHING
    else. The raw expression is printed beside this everywhere it is used, so an
    unrecognised shape degrades to "on a schedule" rather than to a wrong reading
    — a summary that misdescribes when an agent runs is worse than one that
    declines to."""
    fields = (cron or "").split()
    if len(fields) != 5:
        return "on a schedule"
    minute, hour, dom, month, dow = fields
    if not (minute.isdigit() and hour.isdigit()) or month != "*" or dom != "*":
        return "on a schedule"
    at = "{}:{:02d}".format(int(hour), int(minute))
    if dow == "*":
        return "every day at {}".format(at)
    if dow in ("1-5", "MON-FRI", "mon-fri"):
        return "every weekday at {}".format(at)
    if dow in _DAY_NAMES:
        return "every {} at {}".format(_DAY_NAMES[dow], at)
    return "on a schedule"


def _recipient_ids(schedule: Dict[str, Any]) -> List[str]:
    """Person ids in the order the interview collected them, de-duplicated. A
    recipient may be given as a bare id or as `{personId, name}` — the name is
    for the summary, and the platform only ever stores the id."""
    ids: List[str] = []
    for person in schedule.get("recipients") or []:
        pid = person.get("personId") if isinstance(person, dict) else person
        if pid not in ids:
            ids.append(pid)
    return ids


def _recipient_names(schedule: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for person in schedule.get("recipients") or []:
        if isinstance(person, dict):
            names.append(person.get("name") or person.get("personId") or "")
        else:
            names.append(person)
    return [n for n in names if n]


def handoff(brief: Dict[str, Any]) -> Dict[str, Any]:
    """What the person has to do in Soleon themselves — named, never skipped.

    Schedules are NOT here any more: they are created by the plan
    (`put_agent_automation`). Before resolve_people existed there was no way to
    obtain a recipient id, so the whole automation was handed back and the person
    re-entered a name, a cron, a timezone and a prompt the interview had already
    written. Connecting an integration stays a handoff — it is an OAuth consent
    that only the account holder can give."""
    connect = [display_of(e) for e in _integrations(brief) if e.get("kind", "mcp") == "mcp"]
    return {"connect": connect, "schedules": []}


#: A Bedrock model id carries its own name; the region prefix, the mode suffix,
#: the release date and the `-v1`/`:0` revision are plumbing. There is NO MCP
#: tool that serves the platform's model catalogue (which is where the SPA's
#: `displayName` comes from), so the readable name is DERIVED from the id — and
#: the exact id is always printed beside it, because the id is what deploys.
_MODEL_REGIONS = ("us.", "eu.", "apac.", "global.")


def model_label(model_id: str) -> str:
    """`us.anthropic.claude-sonnet-5` → `Claude Sonnet 5`. Empty when it can't
    be read as a model id, in which case the caller shows the id alone."""
    body = str(model_id or "").strip().split("::", 1)[0]  # drop ::reasoning
    for prefix in _MODEL_REGIONS:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    body = body.partition(".")[2] or body       # drop the vendor ("anthropic.")
    body = body.split(":", 1)[0]                # drop a ":0" revision
    words = [w for w in re.split(r"[-_]", body) if w]
    while words and (re.fullmatch(r"\d{6,}", words[-1]) or re.fullmatch(r"v\d+", words[-1])):
        words.pop()                             # a release date, or "-v1"
    out: List[str] = []
    for word in words:
        # `claude-haiku-4-5` is Haiku 4.5, not "Haiku 4 5" — consecutive numbers
        # are one version number that the id spells with hyphens.
        if out and re.fullmatch(r"\d+", word) and re.fullmatch(r"[\d.]+", out[-1]):
            out[-1] = "{}.{}".format(out[-1], word)
        else:
            out.append(word if re.fullmatch(r"[\d.]+", word) else word[:1].upper() + word[1:])
    return " ".join(out)


def model_choice(brief: Dict[str, Any]) -> str:
    """The model fact: the readable name, the exact id, and WHY this one."""
    model = str(brief.get("model") or "")
    label = model_label(model)
    value = "{} (`{}`)".format(label, model) if label else "`{}`".format(model)
    reason = str(brief.get("modelReason") or "").strip()
    return "{} — {}".format(value, reason) if reason else value


def channel_choice(brief: Dict[str, Any]) -> str:
    """Where people reach it. Soleon chat is always one of them — an agent is
    reachable in Soleon whether or not a channel is bound."""
    instance = str(brief.get("channelInstanceId") or "").strip()
    if not instance:
        return "Soleon chat only — nothing else is connected to it"
    kind = str(brief.get("channelType") or "").strip()
    name = str(brief.get("channelName") or "").strip()
    # An id is not a name (and `list_channel_instances` is platform-admin only,
    # so the name is whatever the person called it). Show the name when there is
    # one, and the id after it — the id is what binds.
    named = " ".join(w for w in (kind.title() if kind else "", name and "“{}”".format(name)) if w)
    return "Soleon chat, and {} (`{}`)".format(named or "the channel you named", instance)


def summary(brief: Dict[str, Any]) -> List[Dict[str, str]]:
    """What the plan applies, as LABELLED FACTS — `{label, value}`, in a fixed
    order, one fact per entry. The skill renders them verbatim, so what the
    person approves is what runs.

    Labelled rather than prose because prose is what they have to read; a label
    is what they SCAN. The first version was a run of full sentences and the
    person could not pick the model, the channels or the approval rule out of it
    without reading all of it (USER 2026-09-23: "a long blob of text that is
    hard to process"). Same facts, addressable.
    """
    facts: List[Dict[str, str]] = []

    def fact(label: str, value: str) -> None:
        facts.append({"label": label, "value": value})

    fact("Name", "{} ({})".format(brief["displayName"], brief["slug"]))
    fact("Model", model_choice(brief))
    fact("Channels", channel_choice(brief))

    integrations = _integrations(brief)
    if not integrations and not brief.get("knowledgeBases"):
        fact("Your accounts", "none connected — it cannot read or change anything in them")
    for e in integrations:
        name = display_of(e)
        if e["access"] == "read":
            fact(name, "can read, cannot change anything")
        elif e.get("writeApproval", True):
            fact(name, "can read, and can make changes — every change asks you first")
        else:
            fact(name, "can read and make changes WITHOUT asking you (you said: \"{}\")".format(
                e["writeApprovalReason"].strip()))
    for kb in brief.get("knowledgeBases") or []:
        fact("Knowledge base", "{} — can search it, cannot change it".format(kb))
    for s in brief.get("skills") or []:
        fact("Skill", s["name"])
    for e in brief.get("evals") or []:
        fact("Tested against", e["name"])
    for s in brief.get("schedules") or []:
        who = _recipient_names(s)
        fact("Runs on its own", "\"{}\" — {} {} [{}], for {}".format(
            s["name"], _cron_in_words(s["cron"]), s.get("timezone") or DEFAULT_TIMEZONE, s["cron"],
            ", ".join(who) if who else "the people you named"))

    # Web is its OWN fact, not a clause inside the baseline: it is the one
    # baseline capability the brief can switch off, so it is a decision the
    # person is approving rather than a constant.
    fact("Web", WEB_ON_VALUE if brief.get("webAccess") is not False else WEB_OFF_VALUE)
    fact("Also built in", BASELINE_VALUE)
    return facts


#: The ref that owns web_search / web_fetch / browser_* (AHP-940's owner table).
WEB_REF = "sys_web_prompt"

#: What EVERY new agent can do, whatever the brief says — read off the
#: runtime's own registration (`list_agent_tools` on a fresh agent, dev,
#: 2026-09-22), all with `approval: false`: web_search, web_fetch and
#: browser_navigate/interact/screenshot; create_spreadsheet (.xlsx),
#: create_deck (.pptx), their edit twins and attach_file; read/write/edit/
#: list_dir on its own workspace; plus discovery_*, create_idea and
#: emit_document, which act only inside Soleon's Discovery / Ideas /
#: knowledge-base flows. None of them use the person's integrations.
#:
#: Stated on every summary because leaving them out was twice a false claim:
#: the first summary said an agent with no integrations "can only talk" while
#: it could browse the web, and the fix after it still missed the document
#: tools — which are not tool REFS, so a config readback never shows them.
#: Step 7 lists what the runtime actually registers.
#: Web is stated as its own fact because it is the one baseline capability the
#: brief switches off. With `webAccess: false` the "can browse" claim would be
#: FALSE, and an "answers only from our pricing KB" agent that can still search
#: the web quietly answers from whatever it finds online — so the summary states
#: it as a fact about the agent's TOOLS, not as a request in its instructions.
WEB_ON_VALUE = "on — it can search, read and browse the web, without asking"
WEB_OFF_VALUE = "OFF — it cannot search, read or browse the web at all"

BASELINE_VALUE = ("makes Excel and PowerPoint files and hands them to you, and keeps notes in its own "
                  "workspace — without asking, and without using any of your accounts. The full list is "
                  "shown after it is created")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="create_agent.py")
    p.add_argument("--plugin-root")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("defaults")
    d.add_argument("--agents", required=True)
    s = sub.add_parser("suggest-slug")
    s.add_argument("--name", required=True)
    s.add_argument("--agents")
    for name in ("validate", "plan"):
        c = sub.add_parser(name)
        c.add_argument("--brief", required=True)
        c.add_argument("--agents")
        if name == "plan":
            # Optional: without it the plan simply carries no links. The skill
            # passes the URL of the server it is talking to.
            c.add_argument("--server-url")
    args = p.parse_args(argv)

    contract = load_contract(_plugin_root(args.plugin_root))
    agents = agents_of(_load_json(args.agents)) if getattr(args, "agents", None) else None

    if args.cmd == "defaults":
        model, reason, in_use = default_model(agents or [])
        _emit({"takenSlugs": sorted(a.get("slug") for a in agents or [] if a.get("slug")),
               "defaultModel": model, "defaultModelReason": reason, "modelsInUse": in_use,
               "knownIntegrations": KNOWN_INTEGRATIONS})
        return 0
    if args.cmd == "suggest-slug":
        taken = {a.get("slug") for a in agents or []}
        _emit({"slug": suggest_slug(args.name, taken, re.compile(contract["slug_pattern"]))})
        return 0

    brief = _load_json(args.brief)
    errors, warnings = validate(brief, contract, agents)
    if args.cmd == "validate" or errors:
        _emit({"valid": not errors, "errors": errors, "warnings": warnings})
        return 0 if not errors else 1
    out = plan(brief, getattr(args, "server_url", None))
    out["warnings"] = warnings
    _emit(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
