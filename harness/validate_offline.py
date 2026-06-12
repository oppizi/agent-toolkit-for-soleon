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
        model_id = (config.get("config") or {}).get("model") if isinstance(config.get("config"), dict) else None
        if model_id in set(contract["model_aliases"]):
            errors.append(
                f"config.model {model_id!r} is a Claude Code alias — registers cleanly, "
                "breaks at runtime"
            )
        soul = config.get("soul")
        if not isinstance(soul, str) or not soul.strip():
            errors.append("configFields.soul missing or empty")
        elif len(soul.encode("utf-8")) > contract["max_soul_bytes"]:
            errors.append("soul exceeds 256KB byte cap")

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
