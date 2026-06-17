"""allium-to-json — convert an agent_identity.allium spec into the POST /agents
request body + the derived create-time DynamoDB CONFIG-row projection.

Usage:
  python3 allium_to_json.py <spec.allium> --app-env dev [--out-dir DIR]
                            [--skill PATH ...]

Outputs (slug-namespaced, written to --out-dir, default: beside the spec):
  <slug>.request_body.json    the real POST /agents envelope
  <slug>.ddb_projection.json  predicted create-time CONFIG row (offline oracle target)
  <slug>.report.json          audit trail: config summary, skills, decode decisions

configFields carries soul + (distilled, confirmed) config + (operator-supplied)
skills. soul/config/skills all go to S3 server-side, never onto the CONFIG row,
so build_projection is independent of them.

Pipeline (check-first ordering is load-bearing):
  1. engine.check  — gate on parsed severity=="error" only (exit code lies)
  2. engine.model  — config params (incl. config_proposal); hard-fail iff config array absent
  3. engine.parse  — @guidance + invariant walk (report only)
  4. local validation against contract.json (full parity with the server)
  5. emit body + projection + report

Stdlib only; reads plugin/contract.json, never repo source.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Spec config params the converter understands. `model` is an explicit Bedrock
# catalog id (alias-guarded); `config_proposal` is the JSON-escaped distilled
# config (model/evals/guardrails/promptCaching) the elicit step confirmed.
KNOWN_PARAMS = {
    "slug", "display_name", "framework", "visibility", "soul", "model",
    "config_proposal",
}


class ConvertError(Exception):
    pass


class SpecError(ConvertError):
    pass


class DecodeError(ConvertError):
    pass


class ValidationError(ConvertError):
    pass


# ---------------------------------------------------------------------------
# Server-mirrored skill rendering (byte-exact copies of
# lambda/ui_admin/index.py:_yaml_escape / _skill_to_markdown — the 64 KB skill
# cap is measured on this rendered output, so the copies must match the server
# exactly; a render-parity test guards them against the live source).
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
    frontmatter_lines = [
        "---",
        f"name: {skill['id']}",
    ]
    if display_name and display_name != skill["id"]:
        frontmatter_lines.append(f"displayName: {_yaml_escape(display_name)}")
    frontmatter_lines.extend([
        f"description: {_yaml_escape(skill.get('description', '') or '')}",
        f"enabled: {enabled_val}",
        "---",
        "",
    ])
    return "\n".join(frontmatter_lines) + (skill.get("content", "") or "")


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Minimal YAML-frontmatter reader for local Claude Code SKILL.md.

    Returns ({}, raw) when no frontmatter. Only `key: value` lines are parsed;
    a value that is a complete JSON string literal is unquoted. Body byte-exact.
    """
    if not raw.startswith("---"):
        return {}, raw
    remainder = raw[3:]
    if remainder.startswith("\r\n"):
        remainder = remainder[2:]
    elif remainder.startswith("\n"):
        remainder = remainder[1:]
    else:
        return {}, raw
    closing = remainder.find("\n---")
    if closing < 0:
        return {}, raw
    block = remainder[:closing]
    body_start = closing + len("\n---")
    if body_start < len(remainder) and remainder[body_start] == "\r":
        body_start += 1
    if body_start < len(remainder) and remainder[body_start] == "\n":
        body_start += 1
    body = remainder[body_start:]
    fm: dict = {}
    for line in block.split("\n"):
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if val[:1] == '"' and val[-1:] == '"':
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                pass
        fm[key] = val
    return fm, body


def decode_default_expr(name: str, raw: str):
    """Decode allium model's raw-source default_expr.

    json.loads first; identifier-as-string fallback ONLY for bare identifiers
    (enum variants like `maverick`). A quote-led expr that fails json.loads is
    corruption, never silently passed downstream.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    if IDENTIFIER_RE.match(raw):
        return raw
    raise DecodeError(
        f"config param {name!r}: default_expr is neither valid JSON nor a bare "
        f"identifier — refusing to guess. Raw expr: {raw[:200]!r}"
    )


def read_spec(binary: str, spec_path: Path) -> tuple[dict, dict]:
    """check → model → parse. Returns (params, report_fragment)."""
    diagnostics = engine.check(binary, spec_path)
    errors = [d for d in diagnostics if d.get("severity") == "error"]
    if errors:
        lines = "\n".join(
            f"  {d['location']['file']}:{d['location']['line']}:{d['location']['col']} "
            f"{d['message']}"
            for d in errors
        )
        raise SpecError(f"spec has {len(errors)} error(s):\n{lines}")
    warnings = [d for d in diagnostics if d.get("severity") != "error"]

    model_out = engine.model(binary, spec_path)
    params: dict = {}
    decode_log = []
    for entry in model_out["config"]:
        name = entry["name"]
        if name in params:
            raise SpecError(f"duplicate config param {name!r} in spec — refusing dict-last-wins")
        value = decode_default_expr(name, entry["default_expr"])
        params[name] = value
        decode_log.append({"name": name, "type_expr": entry.get("type_expr")})

    parse_out = engine.parse(binary, spec_path)
    guidance, invariants = [], []

    def walk(node):
        if isinstance(node, dict):
            kind = node.get("kind")
            if isinstance(kind, dict) and "Annotation" in kind:
                ann = kind["Annotation"]
                if ann.get("kind") == "Guidance":
                    guidance.append(ann.get("body", []))
            inv = node.get("Invariant")
            if isinstance(inv, dict) and isinstance(inv.get("name"), dict):
                invariants.append(inv["name"].get("name"))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(parse_out)
    report = {
        "check_warnings": [
            f"{d['location']['line']}:{d['location']['col']} {d['message']}" for d in warnings
        ],
        "config_params": decode_log,
        "guidance_blocks": guidance,
        "invariants": invariants,
    }
    return params, report


# ---------------------------------------------------------------------------
# Full-parity config validation (mirrors lambda/ui_admin/index.py
# _validate_config_fields + its sub-validators). schedules is DEFERRED.
# ---------------------------------------------------------------------------

def _validate_guardrails(g, contract: dict) -> None:
    if not isinstance(g, dict):
        raise ValidationError("config.guardrails must be an object")
    for k in contract["guardrail_enabled_keys"]:
        if k in g and not isinstance(g[k], bool):
            raise ValidationError(f"config.guardrails.{k} must be boolean")
    cf = g.get("contentFilters")
    if isinstance(cf, dict):
        for k, v in cf.items():
            if v not in contract["guardrail_strengths"]:
                raise ValidationError(
                    f"config.guardrails.contentFilters.{k}={v!r} not in "
                    f"{sorted(contract['guardrail_strengths'])}"
                )
    pii = g.get("piiEntities")
    if isinstance(pii, dict):
        for k, v in pii.items():
            if v not in contract["pii_actions"]:
                raise ValidationError(
                    f"config.guardrails.piiEntities.{k}={v!r} not in "
                    f"{sorted(contract['pii_actions'])}"
                )
    regex = g.get("regexFilters")
    if isinstance(regex, list):
        for i, r in enumerate(regex):
            if isinstance(r, dict) and "action" in r and r["action"] not in contract["regex_actions"]:
                raise ValidationError(
                    f"config.guardrails.regexFilters[{i}].action={r['action']!r} not in "
                    f"{sorted(contract['regex_actions'])}"
                )
    if "requiredTopics" in g:
        rt = g["requiredTopics"]
        if not isinstance(rt, list):
            raise ValidationError("config.guardrails.requiredTopics must be a list of strings")
        for i, t in enumerate(rt):
            if not isinstance(t, str) or not t.strip():
                raise ValidationError(f"config.guardrails.requiredTopics[{i}] must be a non-empty string")
    if "requiredTopicsStrictness" in g:
        rts = g["requiredTopicsStrictness"]
        if rts not in contract["required_topic_strictness"]:
            raise ValidationError(
                f"config.guardrails.requiredTopicsStrictness={rts!r} not in "
                f"{sorted(contract['required_topic_strictness'])}"
            )


def _validate_evals(evals, contract: dict) -> None:
    if not isinstance(evals, dict):
        raise ValidationError("config.evals must be an object")
    items = evals.get("standardEvals")
    if items is None:
        return
    if not isinstance(items, list):
        raise ValidationError("config.evals.standardEvals must be a list")
    seen: set[str] = set()
    for i, ev in enumerate(items):
        loc = f"config.evals.standardEvals[{i}]"
        if not isinstance(ev, dict):
            raise ValidationError(f"{loc} must be an object")
        if not isinstance(ev.get("id"), str) or not ev["id"]:
            raise ValidationError(f"{loc}.id required (non-empty string)")
        if ev["id"] in seen:
            raise ValidationError(f"{loc}.id={ev['id']!r} duplicates an earlier eval")
        seen.add(ev["id"])
        name = ev.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError(f"{loc}.name required (non-empty string)")
        if len(name) > contract["eval_name_max"]:
            raise ValidationError(f"{loc}.name exceeds {contract['eval_name_max']} chars")
        if "inputs" in ev:
            inputs = ev["inputs"]
            if not isinstance(inputs, list) or not inputs:
                raise ValidationError(f"{loc}.inputs must be a non-empty list")
            for j, turn in enumerate(inputs):
                if not isinstance(turn, dict):
                    raise ValidationError(f"{loc}.inputs[{j}] must be an object")
                if turn.get("role") not in contract["eval_turn_roles"]:
                    raise ValidationError(
                        f"{loc}.inputs[{j}].role must be one of {sorted(contract['eval_turn_roles'])}"
                    )
                if not isinstance(turn.get("content"), str):
                    raise ValidationError(f"{loc}.inputs[{j}].content must be a string")
            if inputs[-1].get("role") != "user":
                raise ValidationError(f"{loc}.inputs final turn must be role='user'")
        elif "inputPrompt" in ev:
            if not isinstance(ev["inputPrompt"], str):
                raise ValidationError(f"{loc}.inputPrompt must be a string")
        else:
            raise ValidationError(f"{loc} must have `inputs` (or legacy `inputPrompt`)")
        if not isinstance(ev.get("expectedOutput"), str):
            raise ValidationError(f"{loc}.expectedOutput must be a string")
        scoring = ev.get("scoringMode")
        if scoring not in contract["eval_scoring_modes"]:
            raise ValidationError(
                f"{loc}.scoringMode must be one of {sorted(contract['eval_scoring_modes'])}"
            )
        if scoring == "score" and "scoreThreshold" in ev:
            t = ev["scoreThreshold"]
            if not isinstance(t, (int, float)) or isinstance(t, bool) or t < 0 or t > 100:
                raise ValidationError(f"{loc}.scoreThreshold must be a number in [0, 100]")
        jm = ev.get("judgeModel")
        if jm is not None and jm not in contract["eval_judge_model_allowlist"]:
            raise ValidationError(
                f"{loc}.judgeModel={jm!r} not in allowlist {contract['eval_judge_model_allowlist']}"
            )
        etc = ev.get("expectedToolCalls")
        if etc is not None:
            if not isinstance(etc, list):
                raise ValidationError(f"{loc}.expectedToolCalls must be a list")
            for j, call in enumerate(etc):
                if not isinstance(call, dict):
                    raise ValidationError(f"{loc}.expectedToolCalls[{j}] must be an object")
                if not isinstance(call.get("name"), str) or not call["name"]:
                    raise ValidationError(f"{loc}.expectedToolCalls[{j}].name required (non-empty string)")
                if "args" in call and not isinstance(call["args"], dict):
                    raise ValidationError(f"{loc}.expectedToolCalls[{j}].args must be an object")


def _validate_tools(tools, contract: dict) -> None:
    if isinstance(tools, list):
        for entry in tools:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry.get("id"):
                raise ValidationError("config.tools list entries must be {id: str, approval: bool}")
            if "subagent" in entry and not isinstance(entry["subagent"], bool):
                raise ValidationError("config.tools[].subagent must be a boolean")
            if "systemPrompt" in entry and not isinstance(entry["systemPrompt"], str):
                raise ValidationError("config.tools[].systemPrompt must be a string")
            overrides = entry.get("upstreamOverrides")
            if overrides is not None:
                if not isinstance(overrides, dict):
                    raise ValidationError("config.tools[].upstreamOverrides must be an object")
                for ut_name, ut_val in overrides.items():
                    if not isinstance(ut_name, str) or not ut_name:
                        raise ValidationError("config.tools[].upstreamOverrides keys must be non-empty strings")
                    if not isinstance(ut_val, dict):
                        raise ValidationError(f"config.tools[].upstreamOverrides[{ut_name!r}] must be an object")
                    t = ut_val.get("type")
                    if t is not None and t not in contract["tool_upstream_override_types"]:
                        raise ValidationError(
                            f"config.tools[].upstreamOverrides[{ut_name!r}].type must be "
                            f"one of {contract['tool_upstream_override_types']}"
                        )
                    if ut_val.get("approval") is not None and not isinstance(ut_val["approval"], bool):
                        raise ValidationError(f"config.tools[].upstreamOverrides[{ut_name!r}].approval must be a boolean")
                    if ut_val.get("enabled") is not None and not isinstance(ut_val["enabled"], bool):
                        raise ValidationError(f"config.tools[].upstreamOverrides[{ut_name!r}].enabled must be a boolean")
        return
    if not isinstance(tools, dict):
        raise ValidationError("config.tools must be an object or list of tool refs")
    allow = tools.get("mcpToolAllowlist")
    if allow is not None:
        if not isinstance(allow, dict):
            raise ValidationError("config.tools.mcpToolAllowlist must be an object")
        for iid, names in allow.items():
            if not isinstance(iid, str) or not iid:
                raise ValidationError("config.tools.mcpToolAllowlist keys must be non-empty strings")
            if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
                raise ValidationError(
                    f"config.tools.mcpToolAllowlist[{iid}] must be a list of non-empty strings"
                )


def validate_config(cfg: dict, contract: dict) -> None:
    """Faithful offline mirror of _validate_config_fields' `config` branch.

    schedules is DEFERRED (faithful cron validity needs a third-party dep that
    would break the plugin's stdlib-only guarantee) — reject it loudly rather
    than pass through.
    """
    if not isinstance(cfg, dict):
        raise ValidationError("config must be an object")
    if "schedules" in cfg:
        raise ConvertError(
            "config.schedules is not supported in this version — faithful cron "
            "validation needs a dependency the plugin deliberately avoids. Omit "
            "schedules (a named follow-up); see the plugin README."
        )
    if len(json.dumps(cfg).encode()) > contract["max_config_bytes"]:
        raise ValidationError(f"config exceeds {contract['max_config_bytes']} bytes")
    model_id = cfg.get("model")
    if model_id is not None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValidationError("config.model must be a non-empty string")
        if model_id in set(contract["model_aliases"]):
            raise ValidationError(
                f"config.model {model_id!r} is a Claude Code alias, not a Bedrock catalog id — "
                "it would register cleanly and silently break the agent at runtime. The elicit "
                "step must map it to a catalog id."
            )
    if "guardrails" in cfg:
        _validate_guardrails(cfg["guardrails"], contract)
    if "promptCaching" in cfg and not isinstance(cfg["promptCaching"], bool):
        raise ValidationError("config.promptCaching must be a boolean")
    if "promptCacheTtl" in cfg and cfg["promptCacheTtl"] not in contract["prompt_cache_ttl_values"]:
        raise ValidationError(
            f"config.promptCacheTtl must be one of {contract['prompt_cache_ttl_values']}"
        )
    if "user_schedules_enabled" in cfg and not isinstance(cfg["user_schedules_enabled"], bool):
        raise ValidationError("config.user_schedules_enabled must be a boolean")
    if "tools" in cfg:
        _validate_tools(cfg["tools"], contract)
    if "evals" in cfg:
        _validate_evals(cfg["evals"], contract)


# ---------------------------------------------------------------------------
# Skills — operator-supplied bundles (NOT distilled from the identity).
# ---------------------------------------------------------------------------

def _title_case_slug(slug: str) -> str:
    return slug.replace("-", " ").title()


def _normalize_skill_id(raw: str) -> str:
    return raw.strip().lower().replace("_", "-").replace(" ", "-")


def load_skill_input(path_str: str) -> list[dict]:
    """Load one --skill input into platform skill object(s) with source metadata.

    A .json input is a pre-built skill object (or list) — `name` required.
    A directory / .md input is a local Claude Code SKILL.md — id from the dir
    slug, description from frontmatter, content = body byte-exact, enabled=true,
    name defaulted to the Title-Cased slug (the elicit step confirms it). CC's
    `argument-hint` (and any non-skill frontmatter key) is dropped and recorded.
    """
    path = Path(path_str)
    if not path.exists():
        raise ConvertError(f"--skill path not found: {path}")
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        objs = data if isinstance(data, list) else [data]
        out = []
        for o in objs:
            if not isinstance(o, dict):
                raise ConvertError(f"skill JSON {path} entries must be objects")
            o = dict(o)
            o["_source"] = str(path)
            o["_dropped"] = []
            out.append(o)
        return out
    md_path = path / "SKILL.md" if path.is_dir() else path
    if not md_path.is_file():
        raise ConvertError(f"--skill dir has no SKILL.md: {md_path}")
    slug = _normalize_skill_id((path.name if path.is_dir() else path.stem))
    fm, body = _split_frontmatter(md_path.read_text(encoding="utf-8"))
    dropped = sorted(k for k in fm if k != "description")
    return [{
        "id": slug,
        "name": _title_case_slug(slug),
        "description": fm.get("description", "") or "",
        "content": body,
        "enabled": True,
        "_source": str(md_path),
        "_dropped": dropped,
    }]


def validate_skills(raw_objs: list[dict], contract: dict) -> tuple[list[dict], list[dict]]:
    """Faithful mirror of _validate_skills. Returns (emit_objs, report_meta).

    emit_objs carry only _SKILL_FIELDS; report_meta carries source + rendered
    size + dropped fields for the audit trail.
    """
    if not isinstance(raw_objs, list):
        raise ValidationError("skills must be a list")
    if len(raw_objs) > contract["max_skills_per_agent"]:
        raise ValidationError(f"too many skills (max {contract['max_skills_per_agent']})")
    skill_fields = set(contract["skill_fields"])
    slug_re = contract["skill_slug_pattern"]
    seen: dict[str, str] = {}
    emit, meta = [], []
    for i, raw in enumerate(raw_objs):
        if not isinstance(raw, dict):
            raise ValidationError(f"skills[{i}] must be an object")
        source = raw.get("_source", f"skills[{i}]")
        skill = {k: v for k, v in raw.items() if not k.startswith("_")}
        unknown = set(skill) - skill_fields
        if unknown:
            raise ValidationError(f"skills[{i}] has unknown fields: {sorted(unknown)}")
        for key in ("id", "name", "description", "content"):
            if key in skill and not isinstance(skill[key], str):
                raise ValidationError(f"skills[{i}].{key} must be a string")
        for key in ("id", "name"):
            if not str(skill.get(key, "")).strip():
                raise ValidationError(f"skills[{i}].{key} required (from {source})")
        if not re.match(slug_re, skill["id"]):
            raise ValidationError(
                f"skills[{i}].id {skill['id']!r} invalid slug {slug_re} (from {source})"
            )
        if skill["id"] in seen:
            raise ValidationError(
                f"duplicate skill id {skill['id']!r} from {source} and {seen[skill['id']]}"
            )
        seen[skill["id"]] = source
        if "enabled" in skill and not isinstance(skill["enabled"], bool):
            raise ValidationError(f"skills[{i}].enabled must be bool")
        rendered = len(_skill_to_markdown(skill).encode("utf-8"))
        if rendered > contract["max_skill_bytes"]:
            raise ValidationError(
                f"skills[{i}] ({skill['id']}) renders to {rendered} bytes; "
                f"cap is {contract['max_skill_bytes']}"
            )
        emit.append(skill)
        meta.append({
            "id": skill["id"], "name": skill.get("name", ""),
            "enabled": skill.get("enabled", True), "rendered_bytes": rendered,
            "source": source, "dropped_fields": raw.get("_dropped", []),
        })
    return emit, meta


def validate_params(params: dict, app_env: str, contract: dict) -> dict:
    """Validate decoded params against the contract; return normalized values.

    The final `config` dict is assembled from `config_proposal` (the distilled,
    confirmed config) plus the legacy `model` param, then full-parity validated.
    """
    unknown = set(params) - KNOWN_PARAMS
    if unknown:
        raise ValidationError(
            f"spec config contains params this converter does not map: {sorted(unknown)}. "
            f"Known params: {sorted(KNOWN_PARAMS)}"
        )

    for required in ("slug", "display_name", "framework", "soul"):
        if required not in params:
            raise ValidationError(f"spec config is missing required param {required!r}")

    slug = params["slug"]
    if not isinstance(slug, str) or not re.match(contract["slug_pattern"], slug):
        raise ValidationError(
            f"slug {slug!r} violates pattern {contract['slug_pattern']} "
            "(2-64 chars, lowercase alphanumeric + hyphens, no edge hyphens)"
        )

    display_name = params["display_name"]
    if not isinstance(display_name, str):
        raise ValidationError("display_name must be a string")
    display_name = display_name.strip()
    dn_rules = contract["display_name"]
    if not display_name or len(display_name) > dn_rules["max_len"]:
        raise ValidationError(
            f"display_name must be 1-{dn_rules['max_len']} chars after strip "
            f"(got {len(display_name)})"
        )

    framework = params["framework"]
    if framework not in contract["frameworks"]:
        raise ValidationError(
            f"framework {framework!r} must be one of {sorted(contract['frameworks'])}"
        )

    visibility = params.get("visibility", "private")
    if visibility not in contract["visibility_values"]:
        raise ValidationError(
            f"visibility {visibility!r} must be one of {sorted(contract['visibility_values'])}"
        )

    soul = params["soul"]
    if not isinstance(soul, str) or not soul.strip():
        raise ValidationError("soul must be a non-empty string")
    soul_bytes = len(soul.encode("utf-8"))
    if soul_bytes > contract["max_soul_bytes"]:  # server rejects strictly-greater
        raise ValidationError(
            f"soul is {soul_bytes} UTF-8 bytes; cap is {contract['max_soul_bytes']}"
        )

    if app_env not in contract["app_envs"]:
        raise ValidationError(
            f"app_env {app_env!r} must be one of {contract['app_envs']} "
            "(the frozen fixture is stricter than the platform regex; the fixture wins)"
        )

    # Assemble the final config from the distilled proposal + legacy model param.
    config: dict = {}
    proposal = params.get("config_proposal")
    if proposal is not None:
        if isinstance(proposal, str):
            try:
                proposal = json.loads(proposal)
            except json.JSONDecodeError as e:
                raise DecodeError(f"config_proposal is not valid JSON: {e}") from e
        if not isinstance(proposal, dict):
            raise ValidationError("config_proposal must decode to a JSON object")
        config = dict(proposal)
    model_param = params.get("model")
    if model_param is not None:
        if config.get("model") not in (None, model_param):
            raise ValidationError(
                "model param conflicts with config_proposal.model — set one source of truth"
            )
        config["model"] = model_param
    config = config or None
    if config is not None:
        validate_config(config, contract)

    return {
        "slug": slug,
        "display_name": display_name,
        "framework": framework,
        "visibility": visibility,
        "soul": soul,
        "config": config,
        "skills": [],
        "app_env": app_env,
    }


def build_request_body(v: dict) -> dict:
    """The real POST /agents envelope. slug/displayName/framework/appEnv are
    TOP-LEVEL (nesting them in dynamoFields fails _DYNAMO_ALLOWED validation —
    the offline-green/live-400 trap). config + skills are configFields siblings
    of soul, emitted only when non-empty so soul-only output is byte-stable."""
    config_fields: dict = {"soul": v["soul"]}
    if v.get("config"):
        config_fields["config"] = v["config"]
    if v.get("skills"):
        config_fields["skills"] = v["skills"]
    return {
        "slug": v["slug"],
        "displayName": v["display_name"],
        "framework": v["framework"],
        "appEnv": v["app_env"],
        "dynamoFields": {"visibility": v["visibility"]},
        "configFields": config_fields,
    }


def build_projection(v: dict, contract: dict, created_at: str | None = None) -> dict:
    """Predicted create-time CONFIG row (what register_agent will put_item).

    UNCHANGED by config/skills — soul/config/skills go to S3, never the row.
    PK composed from the contract's template (mirrors shared_keys.agent_pk).
    `channels` is never present for a channelless agent.
    """
    pk = contract["pk_template"].format(app_env=v["app_env"], slug=v["slug"])
    projection = {
        "PK": pk,
        "appEnv": v["app_env"],
        "displayName": v["display_name"],
        "framework": v["framework"],
        "createdAt": created_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    projection.update(contract["projection_constants"])
    if v["visibility"] != "private":
        projection["visibility"] = v["visibility"]
    return projection


def convert(spec_path: Path, app_env: str, out_dir: Path | None = None,
            skill_inputs: list[str] | None = None) -> dict:
    root = engine.plugin_root()
    contract = engine.load_contract(root)
    binary, engine_version = engine.resolve(contract, root)

    params, spec_report = read_spec(binary, spec_path)
    v = validate_params(params, app_env, contract)

    raw_skills: list[dict] = []
    for inp in (skill_inputs or []):
        raw_skills.extend(load_skill_input(inp))
    skills, skills_meta = validate_skills(raw_skills, contract)
    v["skills"] = skills

    body = build_request_body(v)
    projection = build_projection(v, contract)

    out_dir = out_dir or spec_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = v["slug"]
    paths = {
        "request_body": out_dir / f"{slug}.request_body.json",
        "ddb_projection": out_dir / f"{slug}.ddb_projection.json",
        "report": out_dir / f"{slug}.report.json",
    }
    cfg = v["config"] or {}
    report = {
        "spec": str(spec_path),
        "engine_binary": binary,
        "engine_version": engine_version,
        "contract_version": contract["contract_version"],
        "model_included": bool(cfg.get("model")),
        "config_keys": sorted(cfg.keys()),
        "skills_included": skills_meta,
        **spec_report,
    }
    paths["request_body"].write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    paths["ddb_projection"].write_text(json.dumps(projection, indent=2) + "\n", encoding="utf-8")
    paths["report"].write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return {"body": body, "projection": projection, "report": report,
            "paths": {k: str(p) for k, p in paths.items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path, help="path to agent_identity.allium")
    ap.add_argument("--app-env", default="dev", help="deploy target app env (default: dev)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="output directory (default: beside the spec)")
    ap.add_argument("--skill", action="append", default=[], metavar="PATH",
                    help="local .claude/skills/<dir>/ (or SKILL.md, or a skill-object "
                         ".json); repeatable. Bundled into configFields.skills.")
    args = ap.parse_args()

    if not args.spec.is_file():
        print(f"ERROR: spec file not found: {args.spec}", file=sys.stderr)
        return 2
    try:
        result = convert(args.spec, args.app_env, args.out_dir, args.skill)
    except (ConvertError, engine.EngineError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for kind, path in result["paths"].items():
        print(f"{kind}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
