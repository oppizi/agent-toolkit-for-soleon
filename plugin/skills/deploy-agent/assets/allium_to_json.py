"""allium-to-json — convert an agent_identity.allium spec into the POST /agents
request body + the derived create-time DynamoDB CONFIG-row projection.

Usage:
  python3 allium_to_json.py <spec.allium> --app-env dev [--out-dir DIR]

Outputs (slug-namespaced, written to --out-dir, default: beside the spec):
  <slug>.request_body.json    the real POST /agents envelope
  <slug>.ddb_projection.json  predicted create-time CONFIG row (offline oracle target)
  <slug>.report.json          audit trail: dropped aliases, guidance nodes,
                              decode decisions, resolved engine version

Pipeline (check-first ordering is load-bearing):
  1. engine.check  — gate on parsed severity=="error" only (exit code lies)
  2. engine.model  — config params; hard-fail iff config array absent
  3. engine.parse  — @guidance + invariant walk (report only, never output)
  4. local validation against contract.json
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

# Spec config params the converter understands. `model` is deliberately NOT
# read from frontmatter aliases — only an explicit Bedrock catalog id that
# survived elicit confirmation may appear here.
KNOWN_PARAMS = {"slug", "display_name", "framework", "visibility", "soul", "model"}


class ConvertError(Exception):
    pass


class SpecError(ConvertError):
    pass


class DecodeError(ConvertError):
    pass


class ValidationError(ConvertError):
    pass


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
            # Top-level invariant declarations appear as {"Invariant": {"name": {"name": ...}}}
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


def validate_params(params: dict, app_env: str, contract: dict) -> dict:
    """Validate decoded params against the contract; return normalized values."""
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

    # Server predicate: non-empty after strip; cap applies to the value sent.
    # We emit the stripped value, so validate the stripped value's length.
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

    model_id = params.get("model")
    if model_id is not None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValidationError("model, when present, must be a non-empty string")
        if model_id in contract["model_aliases"]:
            raise ValidationError(
                f"model {model_id!r} is a Claude Code alias, not a Bedrock catalog id — "
                "it would register cleanly and silently break the agent at runtime. "
                "Omit it (platform template default wins) or supply an explicit catalog id."
            )

    return {
        "slug": slug,
        "display_name": display_name,
        "framework": framework,
        "visibility": visibility,
        "soul": soul,
        "model": model_id,
        "app_env": app_env,
    }


def build_request_body(v: dict) -> dict:
    """The real POST /agents envelope. slug/displayName/framework/appEnv are
    TOP-LEVEL (nesting them in dynamoFields fails _DYNAMO_ALLOWED validation —
    the offline-green/live-400 trap)."""
    config_fields: dict = {"soul": v["soul"]}
    if v["model"]:
        config_fields["config"] = {"model": v["model"]}
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

    PK composed from the contract's template — mirrors shared_keys.agent_pk
    (raw PK literals are lint-banned in the repo; the template IS the vendored
    form of that helper). `channels` is never present for a channelless agent.
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


def convert(spec_path: Path, app_env: str, out_dir: Path | None = None) -> dict:
    root = engine.plugin_root()
    contract = engine.load_contract(root)
    binary, engine_version = engine.resolve(contract, root)

    params, spec_report = read_spec(binary, spec_path)
    v = validate_params(params, app_env, contract)
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
    report = {
        "spec": str(spec_path),
        "engine_binary": binary,
        "engine_version": engine_version,
        "contract_version": contract["contract_version"],
        "model_included": bool(v["model"]),
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
    args = ap.parse_args()

    if not args.spec.is_file():
        print(f"ERROR: spec file not found: {args.spec}", file=sys.stderr)
        return 2
    try:
        result = convert(args.spec, args.app_env, args.out_dir)
    except (ConvertError, engine.EngineError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for kind, path in result["paths"].items():
        print(f"{kind}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
