"""Offline schema_match validator — asserts converter output against the
FROZEN oracle (preflight/expected_channelless_config.json) plus the envelope
rules verified against lambda/ui_admin/index.py.

schema_match: true requires BOTH:
  1. projection check — required_exact exact, required_variable rules, no
     forbidden key, no unlisted key outside optional;
  2. envelope check — the offline-green/live-400 trap: top-level keys exact,
     dynamoFields/configFields key whitelists, no top-level field nested in
     dynamoFields, no channelId, no alias in config.model.

Harness-only (never ships). Falsifiability is enforced by
tests/test_validator_negative.py — every mutation class must be rejected.

Usage: python3 harness/validate_offline.py <request_body.json> <ddb_projection.json>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BET_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = BET_ROOT / "preflight/expected_channelless_config.json"
CONTRACT_PATH = BET_ROOT / "plugin/contract.json"

PK_RE = re.compile(r"^APPENV#(dev|staging|prod)#AGENT#[a-z0-9-]+$")
CREATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
TOP_LEVEL_KEYS = {"slug", "displayName", "framework", "appEnv", "dynamoFields", "configFields"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Server-mirrored render helpers + structural validators. COPIED (not imported
# from the converter) so the validator stays independent of converter bugs —
# falsifiability is enforced by tests/test_validator_negative.py. The render
# copies are guarded against the live source by test_skill_render_parity.
# ---------------------------------------------------------------------------

def _yaml_escape(s: str) -> str:
    if s == "":
        return '""'
    if any(c in s for c in (":", "#", "\n", "\r", "\t")):
        return json.dumps(s)
    if s[0] in ("-", " ", "\t") or s[-1] in (" ", "\t"):
        return json.dumps(s)
    return s


def _skill_to_markdown(skill: dict) -> str:
    enabled_val = "true" if skill.get("enabled", True) else "false"
    display_name = skill.get("name", "") or ""
    lines = ["---", f"name: {skill['id']}"]
    if display_name and display_name != skill["id"]:
        lines.append(f"displayName: {_yaml_escape(display_name)}")
    lines.extend([
        f"description: {_yaml_escape(skill.get('description', '') or '')}",
        f"enabled: {enabled_val}",
        "---",
        "",
    ])
    return "\n".join(lines) + (skill.get("content", "") or "")


def _validate_skills_envelope(skills, contract: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(skills, list):
        return ["configFields.skills must be a list"]
    if len(skills) > contract["max_skills_per_agent"]:
        errs.append(f"too many skills (max {contract['max_skills_per_agent']})")
    skill_fields = set(contract["skill_fields"])
    seen: set[str] = set()
    for i, s in enumerate(skills):
        if not isinstance(s, dict):
            errs.append(f"skills[{i}] must be an object")
            continue
        unknown = set(s) - skill_fields
        if unknown:
            errs.append(f"skills[{i}] has unknown fields: {sorted(unknown)}")
        for key in ("id", "name", "description", "content"):
            if key in s and not isinstance(s[key], str):
                errs.append(f"skills[{i}].{key} must be a string")
        for key in ("id", "name"):
            if not str(s.get(key, "")).strip():
                errs.append(f"skills[{i}].{key} required")
        if isinstance(s.get("id"), str):
            if not re.match(contract["skill_slug_pattern"], s["id"]):
                errs.append(f"skills[{i}].id {s['id']!r} invalid slug")
            elif s["id"] in seen:
                errs.append(f"duplicate skill id {s['id']!r}")
            else:
                seen.add(s["id"])
        if "enabled" in s and not isinstance(s["enabled"], bool):
            errs.append(f"skills[{i}].enabled must be bool")
        if isinstance(s.get("id"), str) and isinstance(s.get("name", ""), str):
            try:
                rendered = len(_skill_to_markdown(s).encode("utf-8"))
                if rendered > contract["max_skill_bytes"]:
                    errs.append(
                        f"skills[{i}] renders to {rendered} bytes; cap {contract['max_skill_bytes']}"
                    )
            except Exception:  # noqa: BLE001 — malformed skill already reported above
                pass
    return errs


def _validate_config_envelope(cfg, contract: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(cfg, dict):
        return ["configFields.config must be an object"]
    if "schedules" in cfg:
        errs.append("config.schedules is unsupported in this version (deferred) — must be omitted")
    if len(json.dumps(cfg).encode("utf-8")) > contract["max_config_bytes"]:
        errs.append(f"config exceeds {contract['max_config_bytes']} bytes")
    model_id = cfg.get("model")
    if model_id is not None:
        if not isinstance(model_id, str) or not model_id.strip():
            errs.append("config.model must be a non-empty string")
        elif model_id in set(contract["model_aliases"]):
            errs.append(f"config.model {model_id!r} is a Claude Code alias — breaks at runtime")
    g = cfg.get("guardrails")
    if g is not None:
        if not isinstance(g, dict):
            errs.append("config.guardrails must be an object")
        else:
            for k in contract["guardrail_enabled_keys"]:
                if k in g and not isinstance(g[k], bool):
                    errs.append(f"config.guardrails.{k} must be boolean")
            cf = g.get("contentFilters")
            if isinstance(cf, dict):
                for k, val in cf.items():
                    if val not in contract["guardrail_strengths"]:
                        errs.append(f"config.guardrails.contentFilters.{k}={val!r} bad strength")
            pii = g.get("piiEntities")
            if isinstance(pii, dict):
                for k, val in pii.items():
                    if val not in contract["pii_actions"]:
                        errs.append(f"config.guardrails.piiEntities.{k}={val!r} bad action")
            rgx = g.get("regexFilters")
            if isinstance(rgx, list):
                for i, r in enumerate(rgx):
                    if isinstance(r, dict) and "action" in r and r["action"] not in contract["regex_actions"]:
                        errs.append(f"config.guardrails.regexFilters[{i}].action bad")
            if "requiredTopics" in g:
                rt = g["requiredTopics"]
                if not isinstance(rt, list) or not all(isinstance(t, str) and t.strip() for t in rt):
                    errs.append("config.guardrails.requiredTopics must be non-empty strings")
            if "requiredTopicsStrictness" in g and g["requiredTopicsStrictness"] not in contract["required_topic_strictness"]:
                errs.append("config.guardrails.requiredTopicsStrictness bad value")
    if "promptCaching" in cfg and not isinstance(cfg["promptCaching"], bool):
        errs.append("config.promptCaching must be a boolean")
    if "promptCacheTtl" in cfg and cfg["promptCacheTtl"] not in contract["prompt_cache_ttl_values"]:
        errs.append(f"config.promptCacheTtl must be one of {contract['prompt_cache_ttl_values']}")
    if "user_schedules_enabled" in cfg and not isinstance(cfg["user_schedules_enabled"], bool):
        errs.append("config.user_schedules_enabled must be a boolean")
    tools = cfg.get("tools")
    if tools is not None:
        if isinstance(tools, list):
            for entry in tools:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry.get("id"):
                    errs.append("config.tools list entries must be {id: str}")
                    continue
                if "subagent" in entry and not isinstance(entry["subagent"], bool):
                    errs.append("config.tools[].subagent must be bool")
                if "systemPrompt" in entry and not isinstance(entry["systemPrompt"], str):
                    errs.append("config.tools[].systemPrompt must be a string")
                ov = entry.get("upstreamOverrides")
                if ov is not None:
                    if not isinstance(ov, dict):
                        errs.append("config.tools[].upstreamOverrides must be an object")
                    else:
                        for un, uv in ov.items():
                            if not isinstance(un, str) or not un or not isinstance(uv, dict):
                                errs.append("config.tools[].upstreamOverrides bad entry")
                                continue
                            if uv.get("type") is not None and uv["type"] not in contract["tool_upstream_override_types"]:
                                errs.append("config.tools[].upstreamOverrides.type bad")
                            if uv.get("approval") is not None and not isinstance(uv["approval"], bool):
                                errs.append("config.tools[].upstreamOverrides.approval must be bool")
                            if uv.get("enabled") is not None and not isinstance(uv["enabled"], bool):
                                errs.append("config.tools[].upstreamOverrides.enabled must be bool")
        elif isinstance(tools, dict):
            allow = tools.get("mcpToolAllowlist")
            if allow is not None:
                if not isinstance(allow, dict):
                    errs.append("config.tools.mcpToolAllowlist must be an object")
                else:
                    for iid, names in allow.items():
                        if not isinstance(iid, str) or not iid:
                            errs.append("config.tools.mcpToolAllowlist keys must be non-empty strings")
                        elif not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
                            errs.append(f"config.tools.mcpToolAllowlist[{iid}] bad")
        else:
            errs.append("config.tools must be an object or list of tool refs")
    evals = cfg.get("evals")
    if evals is not None:
        if not isinstance(evals, dict):
            errs.append("config.evals must be an object")
        else:
            items = evals.get("standardEvals")
            if items is not None:
                if not isinstance(items, list):
                    errs.append("config.evals.standardEvals must be a list")
                else:
                    seen_ev: set[str] = set()
                    for i, ev in enumerate(items):
                        loc = f"config.evals.standardEvals[{i}]"
                        if not isinstance(ev, dict):
                            errs.append(f"{loc} must be an object")
                            continue
                        if not isinstance(ev.get("id"), str) or not ev["id"]:
                            errs.append(f"{loc}.id required")
                        elif ev["id"] in seen_ev:
                            errs.append(f"{loc}.id duplicate")
                        else:
                            seen_ev.add(ev["id"])
                        nm = ev.get("name")
                        if not isinstance(nm, str) or not nm:
                            errs.append(f"{loc}.name required")
                        elif len(nm) > contract["eval_name_max"]:
                            errs.append(f"{loc}.name too long")
                        if "inputs" in ev:
                            inp = ev["inputs"]
                            if not isinstance(inp, list) or not inp:
                                errs.append(f"{loc}.inputs must be a non-empty list")
                            else:
                                for j, turn in enumerate(inp):
                                    if not isinstance(turn, dict):
                                        errs.append(f"{loc}.inputs[{j}] must be an object")
                                    elif turn.get("role") not in contract["eval_turn_roles"]:
                                        errs.append(f"{loc}.inputs[{j}].role bad")
                                    elif not isinstance(turn.get("content"), str):
                                        errs.append(f"{loc}.inputs[{j}].content must be a string")
                                if isinstance(inp[-1], dict) and inp[-1].get("role") != "user":
                                    errs.append(f"{loc}.inputs final turn must be role='user'")
                        elif "inputPrompt" in ev:
                            if not isinstance(ev["inputPrompt"], str):
                                errs.append(f"{loc}.inputPrompt must be a string")
                        else:
                            errs.append(f"{loc} must have inputs or inputPrompt")
                        if not isinstance(ev.get("expectedOutput"), str):
                            errs.append(f"{loc}.expectedOutput must be a string")
                        sc = ev.get("scoringMode")
                        if sc not in contract["eval_scoring_modes"]:
                            errs.append(f"{loc}.scoringMode bad")
                        elif sc == "score" and "scoreThreshold" in ev:
                            t = ev["scoreThreshold"]
                            if not isinstance(t, (int, float)) or isinstance(t, bool) or t < 0 or t > 100:
                                errs.append(f"{loc}.scoreThreshold must be a number in [0,100]")
                        jm = ev.get("judgeModel")
                        if jm is not None and jm not in contract["eval_judge_model_allowlist"]:
                            errs.append(f"{loc}.judgeModel not in allowlist")
                        etc = ev.get("expectedToolCalls")
                        if etc is not None:
                            if not isinstance(etc, list):
                                errs.append(f"{loc}.expectedToolCalls must be a list")
                            else:
                                for j, call in enumerate(etc):
                                    if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not call["name"]:
                                        errs.append(f"{loc}.expectedToolCalls[{j}].name required")
                                    elif "args" in call and not isinstance(call["args"], dict):
                                        errs.append(f"{loc}.expectedToolCalls[{j}].args must be an object")
    return errs


def validate_projection(projection: dict, fixture: dict, contract: dict) -> list[str]:
    errors: list[str] = []

    for key, expected in fixture["required_exact"].items():
        if key not in projection:
            errors.append(f"projection missing required_exact key {key!r}")
        elif projection[key] != expected or type(projection[key]) is not type(expected):
            errors.append(
                f"projection[{key!r}] = {projection[key]!r} "
                f"({type(projection[key]).__name__}) != expected {expected!r} "
                f"({type(expected).__name__})"
            )

    pk = projection.get("PK")
    if not isinstance(pk, str) or not PK_RE.match(pk):
        errors.append(f"PK {pk!r} fails {PK_RE.pattern}")

    app_env = projection.get("appEnv")
    if app_env not in ("dev", "staging", "prod"):
        errors.append(f"appEnv {app_env!r} not in dev|staging|prod")
    elif isinstance(pk, str) and not pk.startswith(f"APPENV#{app_env}#"):
        errors.append(f"appEnv {app_env!r} does not equal the PK app_env segment of {pk!r}")

    display_name = projection.get("displayName")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 256:
        errors.append(f"displayName {display_name!r} fails 1-256 non-empty rule")

    framework = projection.get("framework")
    if framework not in ("maverick", "nanobot", "openclaw"):
        errors.append(f"framework {framework!r} not in maverick|nanobot|openclaw")

    created_at = projection.get("createdAt")
    if not isinstance(created_at, str) or not CREATED_AT_RE.match(created_at):
        errors.append(f"createdAt {created_at!r} fails %Y-%m-%dT%H:%M:%SZ format")

    for key in fixture["forbidden"]:
        if key in projection:
            errors.append(f"forbidden key {key!r} present in projection")

    allowed = (
        set(fixture["required_exact"])
        | set(fixture["required_variable"])
        | set(fixture["optional"])
    )
    for key in projection:
        if key not in allowed:
            errors.append(f"unlisted key {key!r} present in projection")

    # Harness policy (stricter than the server): the channelless fixture pins
    # visibility "private"; anything else is a fixture violation even though
    # the server would accept it.
    slug_pattern = contract["slug_pattern"]
    if isinstance(pk, str) and PK_RE.match(pk):
        slug = pk.rsplit("#", 1)[1]
        if not re.match(slug_pattern, slug):
            errors.append(f"PK slug segment {slug!r} fails real SLUG_PATTERN {slug_pattern}")

    return errors


def validate_envelope(body: dict, contract: dict) -> list[str]:
    errors: list[str] = []

    keys = set(body)
    if keys != TOP_LEVEL_KEYS:
        missing, extra = TOP_LEVEL_KEYS - keys, keys - TOP_LEVEL_KEYS
        if missing:
            errors.append(f"envelope missing top-level keys {sorted(missing)}")
        if extra:
            errors.append(f"envelope has unexpected top-level keys {sorted(extra)}")

    if "channelId" in body:
        errors.append("channelId present — channelless agents must not bind a channel")

    dynamo = body.get("dynamoFields", {})
    if not isinstance(dynamo, dict):
        errors.append("dynamoFields must be an object")
    else:
        bad = set(dynamo) - set(contract["dynamo_allowed"])
        if bad:
            errors.append(f"dynamoFields keys {sorted(bad)} not in _DYNAMO_ALLOWED")
        nested = set(dynamo) & {"slug", "appEnv", "channelId"}
        if nested:
            errors.append(
                f"top-level fields nested in dynamoFields: {sorted(nested)} — "
                "this passes offline and 400s live"
            )
        # Harness policy: we only ever send visibility (one source of truth);
        # registrationOpen etc. are server-allowed but plan-forbidden.
        policy_bad = set(dynamo) - {"visibility"}
        if policy_bad:
            errors.append(f"dynamoFields carries plan-forbidden keys {sorted(policy_bad)}")

    config = body.get("configFields", {})
    if not isinstance(config, dict):
        errors.append("configFields must be an object")
    else:
        bad = set(config) - set(contract["config_allowed"])
        if bad:
            errors.append(f"configFields keys {sorted(bad)} not in _CONFIG_ALLOWED")
        soul = config.get("soul")
        if not isinstance(soul, str) or not soul.strip():
            errors.append("configFields.soul missing or empty")
        elif len(soul.encode("utf-8")) > contract["max_soul_bytes"]:
            errors.append("soul exceeds 256KB byte cap")
        if "config" in config:
            errors.extend(_validate_config_envelope(config["config"], contract))
        if "skills" in config:
            errors.extend(_validate_skills_envelope(config["skills"], contract))

    for field in ("slug", "displayName", "framework", "appEnv"):
        if not isinstance(body.get(field), str) or not body.get(field, "").strip():
            errors.append(f"top-level {field} missing or empty")
    if isinstance(body.get("displayName"), str):
        dn = body["displayName"]
        if dn != dn.strip():
            errors.append("displayName not stripped — converter must emit the stripped value")
        if len(dn) > 256:
            errors.append("displayName exceeds 256 chars")
    if isinstance(body.get("slug"), str) and not re.match(contract["slug_pattern"], body["slug"]):
        errors.append(f"slug {body['slug']!r} fails SLUG_PATTERN")
    if body.get("framework") not in set(contract["frameworks"]):
        errors.append(f"framework {body.get('framework')!r} not in catalog")
    if body.get("appEnv") not in set(contract["app_envs"]):
        errors.append(f"appEnv {body.get('appEnv')!r} not in fixture set")

    return errors


def validate(body: dict, projection: dict) -> dict:
    fixture = _load(FIXTURE_PATH)
    contract = _load(CONTRACT_PATH)
    proj_errors = validate_projection(projection, fixture, contract)
    env_errors = validate_envelope(body, contract)
    cross: list[str] = []
    if not proj_errors and not env_errors:
        if projection.get("appEnv") != body.get("appEnv"):
            cross.append("projection.appEnv != body.appEnv")
        if projection.get("displayName") != body.get("displayName"):
            cross.append("projection.displayName != body.displayName")
        if projection.get("framework") != body.get("framework"):
            cross.append("projection.framework != body.framework")
        if not str(projection.get("PK", "")).endswith(f"#AGENT#{body.get('slug')}"):
            cross.append("projection PK slug segment != body.slug")
    return {
        "schema_match": not (proj_errors or env_errors or cross),
        "projection_errors": proj_errors,
        "envelope_errors": env_errors,
        "cross_errors": cross,
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    result = validate(_load(Path(sys.argv[1])), _load(Path(sys.argv[2])))
    print(json.dumps(result, indent=2))
    return 0 if result["schema_match"] else 1


if __name__ == "__main__":
    sys.exit(main())
