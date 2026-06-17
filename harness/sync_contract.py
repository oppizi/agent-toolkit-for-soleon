"""Generate plugin/contract.json from agent-infra repo source TEXT.

Build-time tool (harness-only, never ships). Reads source as text because
`lambda/ui_admin/index.py` is un-importable outside a deployed env (reads
USER_POOL_ID at import time) and `scripts/agent_manager/registry.py` imports
boto3. The drift test (tests/test_contract_drift.py) re-runs this extraction
and diffs against the shipped contract.json — that is the CI guard that keeps
single-source-of-truth honest.

The validation constants live in TWO server files kept in lockstep
(`lambda/ui_admin/index.py` monolith + `lambda/ui_admin_agents/index.py`
split). We extract from both and assert they agree, so a one-sided drift
fails here loudly instead of silently shipping a contract that only matches
one path.

Usage: python3 harness/sync_contract.py [--repo-root PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

CONTRACT_VERSION = 2
ENGINE_VERSION = "3.2.4"

# Claude Code frontmatter `model:` aliases — never valid Bedrock catalog ids.
# An alias registers cleanly and silently breaks the agent at runtime.
MODEL_ALIASES = ["fable", "opus", "sonnet", "haiku", "inherit", "default"]

# Vendored, POINT-IN-TIME suggestion map: Claude Code `model:` alias →
# candidate Bedrock cross-region catalog id (us.* prefix, per the repo's
# documented agent model-field convention). This CANNOT be validated offline
# (the server does no config.model validation at create, and the live catalog
# is environment-refreshed), so it is advisory only: the /deploy-agent elicit
# ALWAYS confirms the proposed id with the operator before it is emitted, and
# an unmapped alias becomes a question rather than a guess. `inherit`/`default`
# are intentionally absent — they mean "use the platform template default", so
# the proposal omits config.model entirely.
MODEL_ALIAS_MAP = {
    "_note": (
        "point-in-time suggestions (snapshot 2026-06); the elicit step MUST "
        "confirm against the live model catalog — these are NOT validated offline"
    ),
    "fable": "us.anthropic.claude-fable-5",
    "opus": "us.anthropic.claude-opus-4-8",
    "sonnet": "us.anthropic.claude-sonnet-4-6",
    "haiku": "us.anthropic.claude-haiku-4-5-20251001",
}

FRAMEWORK_GLOSSES = {
    "maverick": "Oppizi's in-house framework — the default choice; pick this unless told otherwise.",
    "nanobot": "Lightweight third-party framework for minimal single-purpose agents.",
    "openclaw": "Full-featured third-party framework matching the OpenClaw reference implementation.",
}


def _grep(text: str, pattern: str, source: str, flags: int = re.M) -> str:
    m = re.search(pattern, text, flags)
    if not m:
        raise SystemExit(f"sync_contract: pattern not found in {source}: {pattern}")
    return m.group(1)


def _set_literal(text: str, name: str, source: str) -> list[str]:
    raw = _grep(text, rf"^{name}\s*=\s*(\{{[^}}]*\}})", source)
    values = ast.literal_eval(raw)
    if not isinstance(values, set) or not all(isinstance(v, str) for v in values):
        raise SystemExit(f"sync_contract: {name} did not parse to a set of strings")
    return sorted(values)


def _tuple_literal(text: str, name: str, source: str) -> list[str]:
    # Multi-line tuples are common (no nested parens in these constants), so
    # capture up to the first ')' with DOTALL.
    raw = _grep(text, rf"^{name}\s*=\s*(\([^)]*\))", source, flags=re.M | re.S)
    values = ast.literal_eval(raw)
    if not isinstance(values, tuple) or not all(isinstance(v, str) for v in values):
        raise SystemExit(f"sync_contract: {name} did not parse to a tuple of strings")
    return list(values)


def _int_kib(text: str, name: str, source: str) -> int:
    return int(_grep(text, rf"^{name}\s*=\s*(\d+)\s*\*\s*1024", source)) * 1024


def _index_values(index_py: str) -> dict:
    """All fields derived from an ui_admin*/index.py source text.

    Computed for BOTH server files and asserted equal (dual-index parity)."""
    # The promptCacheTtl + tools.upstreamOverrides.type enums are inline tuple
    # literals inside the validators rather than named constants. Hardcode them
    # but assert the source still spells them that way (mirrors the agent_pk
    # composition assert) so a server change to either breaks here loudly.
    if '("5m", "1h")' not in index_py:
        raise SystemExit('sync_contract: promptCacheTtl enum ("5m", "1h") not found in index source')
    if '("read", "write")' not in index_py:
        raise SystemExit('sync_contract: tools upstreamOverride type enum ("read", "write") not found')
    return {
        "dynamo_allowed": _set_literal(index_py, "_DYNAMO_ALLOWED", "index.py"),
        "config_allowed": _set_literal(index_py, "_CONFIG_ALLOWED", "index.py"),
        "frameworks": _set_literal(index_py, "_FRAMEWORK_VALUES", "index.py"),
        "visibility_values": _set_literal(index_py, "_VISIBILITY_VALUES", "index.py"),
        "max_soul_bytes": _int_kib(index_py, "_MAX_SOUL_BYTES", "index.py"),
        "max_config_bytes": _int_kib(index_py, "_MAX_CONFIG_BYTES", "index.py"),
        "max_skill_bytes": _int_kib(index_py, "_MAX_SKILL_BYTES", "index.py"),
        "max_skills_per_agent": int(
            _grep(index_py, r"^_MAX_SKILLS_PER_AGENT\s*=\s*(\d+)", "index.py")
        ),
        "skill_slug_pattern": _grep(
            index_py, r'_SKILL_SLUG_RE\s*=\s*re\.compile\(r"([^"]+)"\)', "index.py"
        ),
        "skill_fields": _set_literal(index_py, "_SKILL_FIELDS", "index.py"),
        "prompt_cache_ttl_values": ["5m", "1h"],
        "guardrail_strengths": _set_literal(index_py, "_GUARDRAIL_STRENGTHS", "index.py"),
        "pii_actions": _set_literal(index_py, "_PII_ACTIONS", "index.py"),
        "regex_actions": _set_literal(index_py, "_REGEX_ACTIONS", "index.py"),
        "required_topic_strictness": _set_literal(
            index_py, "_REQUIRED_TOPIC_STRICTNESS", "index.py"
        ),
        "guardrail_enabled_keys": _tuple_literal(
            index_py, "_GUARDRAIL_ENABLED_KEYS", "index.py"
        ),
        "eval_scoring_modes": _tuple_literal(index_py, "_EVAL_SCORING_MODES", "index.py"),
        "eval_turn_roles": _tuple_literal(index_py, "_EVAL_TURN_ROLES", "index.py"),
        "eval_judge_model_allowlist": _tuple_literal(
            index_py, "_EVAL_JUDGE_MODEL_ALLOWLIST", "index.py"
        ),
        "eval_name_max": int(_grep(index_py, r"^_EVAL_NAME_MAX\s*=\s*(\d+)", "index.py")),
        "tool_upstream_override_types": ["read", "write"],
    }


def extract(repo_root: Path) -> dict:
    index_py = (repo_root / "lambda/ui_admin/index.py").read_text(encoding="utf-8")
    index_agents_py = (repo_root / "lambda/ui_admin_agents/index.py").read_text(encoding="utf-8")
    registry_py = (repo_root / "scripts/agent_manager/registry.py").read_text(encoding="utf-8")
    shared_keys_py = (repo_root / "shared_keys.py").read_text(encoding="utf-8")

    # Dual-index parity: both server files must agree on every validation
    # constant, or the vendored mirrors in the converter/validator would only
    # match one live path.
    monolith = _index_values(index_py)
    split = _index_values(index_agents_py)
    if monolith != split:
        diff = {k: (monolith[k], split[k]) for k in monolith if monolith[k] != split[k]}
        raise SystemExit(
            "sync_contract: ui_admin and ui_admin_agents validation constants "
            f"have drifted apart (dual-index broken): {diff}"
        )
    idx = monolith

    slug_pattern = _grep(
        registry_py, r'SLUG_PATTERN\s*=\s*re\.compile\(r"([^"]+)"\)', "registry.py"
    )
    app_env_pattern = _grep(
        shared_keys_py, r'_APP_ENV_RE\s*=\s*re\.compile\(r"([^"]+)"\)', "shared_keys.py"
    )
    # PK composition: agent_pk() = APPENV#{app_env}#AGENT#{slug}. Assert the
    # source still composes it that way rather than trusting a template blindly.
    prefix = _grep(shared_keys_py, r'APP_ENV_PREFIX\s*=\s*"([^"]+)"', "shared_keys.py")
    agent_pk_body = _grep(
        shared_keys_py, r'def agent_pk\([^)]*\).*?return (f"[^"]+")', "shared_keys.py",
        flags=re.S,
    )
    if agent_pk_body != 'f"{_app(app_env)}#AGENT#{slug}"':
        raise SystemExit(f"sync_contract: agent_pk composition changed: {agent_pk_body}")

    contract = {
        "contract_version": CONTRACT_VERSION,
        "engine_version": ENGINE_VERSION,
        "source": {
            "repo": "oppizi/agent-infra",
            "files": [
                "lambda/ui_admin/index.py",
                "lambda/ui_admin_agents/index.py",
                "scripts/agent_manager/registry.py",
                "shared_keys.py",
            ],
        },
        "pk_template": prefix + "{app_env}#AGENT#{slug}",
        "slug_pattern": slug_pattern,
        "app_env_pattern": app_env_pattern,
        # The frozen fixture is stricter than shared_keys' regex; the fixture wins.
        "app_envs": ["dev", "staging", "prod"],
        "framework_glosses": FRAMEWORK_GLOSSES,
        # Server predicate (create_agent_route): non-empty after .strip(), cap
        # applies to the value sent. Converter emits the stripped value.
        "display_name": {"max_len": 256, "must_be_nonempty_stripped": True},
        "model_aliases": MODEL_ALIASES,
        "model_alias_map": MODEL_ALIAS_MAP,
        # Create-time CONFIG-row constants for a channelless agent
        # (mirrors preflight/expected_channelless_config.json required_exact).
        "projection_constants": {
            "SK": "CONFIG",
            "agentType": "slack",
            "appId": "",
            "secretName": "",
            "runtimeArn": "",
            "endpointId": "",
            "slackAuthorized": False,
            "registrationOpen": "false",
            "visibility": "private",
        },
        **idx,
    }

    sanity = {"maverick", "nanobot", "openclaw"}
    if set(contract["frameworks"]) != sanity:
        raise SystemExit(f"sync_contract: framework set drifted: {contract['frameworks']}")
    if set(contract["framework_glosses"]) != set(contract["frameworks"]):
        raise SystemExit("sync_contract: glosses out of sync with frameworks")
    # The alias map's concrete keys must be a subset of the recognized aliases.
    map_aliases = {k for k in contract["model_alias_map"] if not k.startswith("_")}
    if not map_aliases <= set(contract["model_aliases"]):
        raise SystemExit("sync_contract: model_alias_map keys not a subset of model_aliases")
    return contract


def main() -> None:
    here = Path(__file__).resolve()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", type=Path, default=here.parents[3])
    ap.add_argument("--out", type=Path, default=here.parents[1] / "plugin/contract.json")
    args = ap.parse_args()

    contract = extract(args.repo_root)
    args.out.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
