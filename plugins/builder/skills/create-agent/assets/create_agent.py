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
                 → {calls, deploy, handoff, summary}; exit 1 when invalid

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

APP_ENV = "dev"
FRAMEWORK = "maverick"
AUTOMATION_PROMPT_MAX = 4096
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


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def _write_ref(entry: Dict[str, Any]) -> str:
    prefix = "custom" if entry.get("kind", "mcp") == "custom" else "mcp"
    return "{}_{}_write".format(prefix, entry["id"])


def display_of(entry: Dict[str, Any]) -> str:
    return KNOWN_INTEGRATIONS.get(entry["id"], entry["id"])


def plan(brief: Dict[str, Any]) -> Dict[str, Any]:
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

    calls.append({"step": "validate", "tool": "validate_agent_draft", "arguments": dict(base)})

    return {
        "calls": calls,
        "deploy": {"step": "deploy", "tool": "deploy_agent_draft", "arguments": dict(base)},
        "handoff": handoff(brief),
        "summary": summary(brief),
    }


def handoff(brief: Dict[str, Any]) -> Dict[str, Any]:
    """What the person has to do in Soleon themselves — named, never skipped."""
    connect = [display_of(e) for e in _integrations(brief) if e.get("kind", "mcp") == "mcp"]
    schedules = [{"name": s["name"], "cron": s["cron"], "timezone": s.get("timezone") or "America/New_York",
                  "prompt": s["prompt"]} for s in brief.get("schedules") or []]
    return {"connect": connect, "schedules": schedules}


def summary(brief: Dict[str, Any]) -> List[str]:
    """Plain-English lines for exactly what the plan applies. The skill shows
    these VERBATIM, so what the person approves is what runs."""
    lines = ["Name: {} ({})".format(brief["displayName"], brief["slug"]),
             "Model: {}".format(brief["model"])]
    ci = brief.get("channelInstanceId")
    lines.append("Reached through: Soleon chat" + (" and the channel {}".format(ci) if ci else ""))
    integrations = _integrations(brief)
    if not integrations and not brief.get("knowledgeBases"):
        lines.append("Integrations: none — it cannot read or change anything in your accounts")
    for e in integrations:
        name = display_of(e)
        if e["access"] == "read":
            lines.append("{}: can read, cannot change anything".format(name))
        elif e.get("writeApproval", True):
            lines.append("{}: can read, and can make changes — every change asks you first".format(name))
        else:
            lines.append("{}: can read and make changes WITHOUT asking you (you said: \"{}\")".format(
                name, e["writeApprovalReason"].strip()))
    for kb in brief.get("knowledgeBases") or []:
        lines.append("Knowledge base {}: can search it, cannot change it".format(kb))
    for s in brief.get("skills") or []:
        lines.append("Skill: {}".format(s["name"]))
    for e in brief.get("evals") or []:
        lines.append("Tested against: {}".format(e["name"]))
    for s in brief.get("schedules") or []:
        lines.append("Schedule \"{}\" ({} {}): you add this in Soleon — it needs your person id, "
                     "which no tool can look up".format(s["name"], s["cron"], s.get("timezone") or "America/New_York"))
    lines.append(BASELINE_NO_WEB_LINE if brief.get("webAccess") is False else BASELINE_LINE)
    return lines


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
BASELINE_LINE = ("Like every new Soleon agent it can also, without asking: search, read and browse the web; "
                 "make Excel and PowerPoint files and hand them to you; and keep notes in its own workspace. "
                 "None of that uses your accounts — the full list is shown after it is created")

#: With `webAccess: false` the baseline claim above would be FALSE, and an
#: "answers only from our pricing KB" agent that can still search the web
#: quietly answers from whatever it finds online. So the summary says the web
#: is off, as a fact about the agent's tools, not a request in its instructions.
BASELINE_NO_WEB_LINE = ("Web: switched off — it cannot search, read or browse the web. Like every new Soleon "
                        "agent it can still, without asking: make Excel and PowerPoint files and hand them to "
                        "you, and keep notes in its own workspace. None of that uses your accounts — the full "
                        "list is shown after it is created")


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
    out = plan(brief)
    out["warnings"] = warnings
    _emit(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
