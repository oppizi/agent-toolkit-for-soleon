"""Generate plugins/soleon/contract.json from agent-infra repo source TEXT.

Build-time tool (harness-only, never ships). Reads source as text because the
admin Lambdas are un-importable outside a deployed env and
`scripts/agent_manager/registry.py` imports boto3. The drift test
(tests/test_contract_drift.py) re-runs this extraction and diffs against the
shipped contract.json — that is the guard that keeps single-source-of-truth
honest.

Where each field lives (AHP-1837, re-sourced after the admin API split; the
pre-split `lambda/ui_admin/index.py` still exists but carries none of these):

- every agent validation constant — `lambda/ui_admin_agents/index.py`, the one
  server file that validates agent create/patch/config (there is no second
  index copy to keep in lockstep any more);
- the skill renderer `_skill_to_markdown` — `lambda/ui_admin_agents/agents_common.py`
  and its twin in `lambda/admin_shared_layer/python/admin_shared/__init__.py`
  (the drift test asserts both render identically to the plugin's copies);
- the agent slug — `scripts/agent_manager/registry.py`;
- the app-env pattern and the agent PK composition — `shared_keys.py`.

Fields the platform DELETED are not shipped (`RETIRED_CONTRACT_KEYS`): eval
scoring modes and the scoreThreshold (standard evals are graded, never
adjudicated; the platform strips them), the eval judge-model allowlist (any
catalogue model judges — `_judge_model_problem`), and the promptCacheTtl enum
(AHP-1478 `RETIRED_AGENT_FIELDS`: caching is always on, the field is dropped).

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

# Hand-authored prose (not extractable from platform source), but the KEY SET must
# track `_FRAMEWORK_VALUES` — the guard at the bottom of build_contract enforces it.
# `nanobot` and `openclaw` were dropped 2026-07-28: the platform archived both, and
# offering them here made the plugin validate agents the API answers 400.
FRAMEWORK_GLOSSES = {
    "maverick": "Oppizi's in-house framework — the default choice; pick this unless told otherwise.",
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


AGENTS_INDEX = "lambda/ui_admin_agents/index.py"
AGENTS_COMMON = "lambda/ui_admin_agents/agents_common.py"
SHARED_LAYER = "lambda/admin_shared_layer/python/admin_shared/__init__.py"
REGISTRY = "scripts/agent_manager/registry.py"
SHARED_KEYS = "shared_keys.py"
SOURCE_FILES = (AGENTS_INDEX, AGENTS_COMMON, SHARED_LAYER, REGISTRY, SHARED_KEYS)

# Contract keys earlier versions shipped for fields the platform has since
# deleted. Never re-add one without a platform validator to extract it from.
RETIRED_CONTRACT_KEYS = frozenset({
    "eval_scoring_modes",          # scoringMode/scoreThreshold stripped (USER 2026-09-03)
    "eval_judge_model_allowlist",  # any catalogue model judges (_judge_model_problem)
    "prompt_cache_ttl_values",     # AHP-1478 RETIRED_AGENT_FIELDS
})

# Frameworks the platform accepts that the plugin must never offer: `mock` is
# the E2E mock lane (AHP-956 — its runtime is the `mock://echo` sentinel and
# the dispatcher answers in-process), not a framework a builder deploys on.
PLATFORM_ONLY_FRAMEWORKS = frozenset({"mock"})


def _require(text: str, needle: str, source: str, what: str) -> None:
    if needle not in text:
        raise SystemExit(f"sync_contract: {what} ({needle}) not found in {source}")


def _register_agent_row_keys(registry_py: str) -> dict:
    """The CONFIG-row attribute names ``register_agent`` writes, read from its
    AST: ``always`` = the ``item = {...}`` literal's keys plus every
    ``item["k"] = ...`` at the function's top level; ``conditional`` = the
    ``item["k"] = ...`` writes nested under an ``if``. The avatar is always
    written too (``item.update(random_avatar())`` when no icon was chosen), so
    its two keys join ``always`` behind a text guard. The frozen oracle is
    checked against this set offline (tests/test_oracle_row_keys.py), so a key
    the platform adds or retires fails a test instead of drifting silently."""
    fn = next(
        (n for n in ast.walk(ast.parse(registry_py))
         if isinstance(n, ast.FunctionDef) and n.name == "register_agent"),
        None,
    )
    if fn is None:
        raise SystemExit(f"sync_contract: register_agent not found in {REGISTRY}")

    def item_subscript_key(node: ast.AST) -> str | None:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].value, ast.Name)
                and node.targets[0].value.id == "item"
                and isinstance(node.targets[0].slice, ast.Constant)
                and isinstance(node.targets[0].slice.value, str)):
            return node.targets[0].slice.value
        return None

    always: set[str] = set()
    literal_found = False
    for stmt in fn.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "item"
                and isinstance(stmt.value, ast.Dict)):
            literal_found = True
            for k in stmt.value.keys:
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    raise SystemExit("sync_contract: register_agent item literal has a non-string key")
                always.add(k.value)
        key = item_subscript_key(stmt)
        if key:
            always.add(key)
    if not literal_found:
        raise SystemExit("sync_contract: register_agent no longer builds `item = {...}`")
    conditional: set[str] = set()
    for node in ast.walk(fn):
        key = item_subscript_key(node)
        if key and key not in always:
            conditional.add(key)
    _require(registry_py, 'if not item.get("iconName"):\n        item.update(random_avatar())',
             REGISTRY, "always-written random avatar")
    always |= {"iconName", "iconColor"}
    return {"always": sorted(always), "conditional": sorted(conditional - always)}


def _agent_values(index_py: str) -> dict:
    """All fields derived from `lambda/ui_admin_agents/index.py` source text."""
    src = AGENTS_INDEX
    # The tools.upstreamOverrides.type enum is an inline tuple literal inside
    # the validator rather than a named constant. Hardcode it but assert the
    # source still spells it that way, so a server change breaks here loudly.
    _require(index_py, 't not in ("read", "write")', src, "tools upstreamOverride type enum")
    # The retired fields must still be RETIRED on the platform (stripped, not
    # validated); if one comes back, this contract has to grow it back too.
    # Presence of each name on its retirement line, not an exact spelling, so a
    # reformat (reordered tuple, trailing comma) does not stop extraction.
    for line_re, names, what in (
        (r"^RETIRED_AGENT_FIELDS\s*=.*$", ("promptCacheTtl", "promptCaching"), "promptCacheTtl retirement"),
        (r"^\s*for \w+ in \(.*\"scoringMode\".*$", ("scoringMode", "scoreThreshold", "allowSideEffects"),
         "eval scoringMode retirement"),
    ):
        m = re.search(line_re, index_py, re.M)
        if not m or not all('"{}"'.format(n) in m.group(0) for n in names):
            raise SystemExit(f"sync_contract: {what} ({', '.join(names)}) not found in {src}")
    for gone in ("_EVAL_SCORING_MODES", "_EVAL_JUDGE_MODEL_ALLOWLIST"):
        if re.search(rf"^{gone}\s*=", index_py, re.M):
            raise SystemExit(f"sync_contract: {gone} is back in {src}; un-retire its contract key")
    platform_frameworks = _set_literal(index_py, "_FRAMEWORK_VALUES", src)
    return {
        "dynamo_allowed": _set_literal(index_py, "_DYNAMO_ALLOWED", src),
        "config_allowed": _set_literal(index_py, "_CONFIG_ALLOWED", src),
        "platform_frameworks": platform_frameworks,
        "frameworks": sorted(set(platform_frameworks) - PLATFORM_ONLY_FRAMEWORKS),
        "visibility_values": _set_literal(index_py, "_VISIBILITY_VALUES", src),
        "max_soul_bytes": _int_kib(index_py, "_MAX_SOUL_BYTES", src),
        "max_config_bytes": _int_kib(index_py, "_MAX_CONFIG_BYTES", src),
        "max_skill_bytes": _int_kib(index_py, "_MAX_SKILL_BYTES", src),
        "max_skills_per_agent": int(
            _grep(index_py, r"^_MAX_SKILLS_PER_AGENT\s*=\s*(\d+)", src)
        ),
        "skill_slug_pattern": _grep(
            index_py, r'^_SKILL_SLUG_RE\s*=\s*re\.compile\(r"([^"]+)"\)', src
        ),
        "skill_fields": _set_literal(index_py, "_SKILL_FIELDS", src),
        "guardrail_strengths": _set_literal(index_py, "_GUARDRAIL_STRENGTHS", src),
        "pii_actions": _set_literal(index_py, "_PII_ACTIONS", src),
        "regex_actions": _set_literal(index_py, "_REGEX_ACTIONS", src),
        "required_topic_strictness": _set_literal(index_py, "_REQUIRED_TOPIC_STRICTNESS", src),
        "guardrail_enabled_keys": _tuple_literal(index_py, "_GUARDRAIL_ENABLED_KEYS", src),
        "eval_turn_roles": _tuple_literal(index_py, "_EVAL_TURN_ROLES", src),
        "eval_name_max": int(_grep(index_py, r"^_EVAL_NAME_MAX\s*=\s*(\d+)", src)),
        "tool_upstream_override_types": ["read", "write"],
    }


def extract(repo_root: Path) -> dict:
    def read(rel: str) -> str:
        path = repo_root / rel
        if not path.is_file():
            raise SystemExit(f"sync_contract: platform source {rel} not found under {repo_root}")
        return path.read_text(encoding="utf-8")

    index_py = read(AGENTS_INDEX)
    registry_py = read(REGISTRY)
    shared_keys_py = read(SHARED_KEYS)
    # The renderer twins are compared by the drift test; here we only require
    # that both still define it, so a move surfaces at extraction time.
    for rel in (AGENTS_COMMON, SHARED_LAYER):
        _require(read(rel), "def _skill_to_markdown(", rel, "skill renderer")

    # The create-time CONFIG row's visibility default (register_agent): a
    # non-public agent is written "restricted". projection_constants carries
    # that value, so a server change here must break extraction loudly.
    _require(registry_py, '"visibility": visibility or ("public" if registration_open else "restricted")',
             REGISTRY, "create-time visibility default")

    # The other create-time constants projection_constants carries.
    _require(registry_py, '"endpointId": "",', REGISTRY, "create-time endpointId")
    _require(registry_py, '"version": 1,', REGISTRY, "create-time config version")
    _require(registry_py,
             'channels: list[str] = [] if lifecycle_state == "in_build" else ["soleon"]',
             REGISTRY, "create-time Soleon Chat membership")
    row_keys = _register_agent_row_keys(registry_py)

    idx = _agent_values(index_py)
    platform_frameworks = idx.pop("platform_frameworks")

    slug_pattern = _grep(
        registry_py, r'SLUG_PATTERN\s*=\s*re\.compile\(r"([^"]+)"\)', REGISTRY
    )
    app_env_pattern = _grep(
        shared_keys_py, r'^_APP_ENV_RE\s*=\s*re\.compile\(r"([^"]+)"\)', SHARED_KEYS
    )
    # PK composition: agent_pk() = APPENV#{app_env}#AGENT#{slug}. Assert the
    # source still composes it that way rather than trusting a template blindly.
    prefix = _grep(shared_keys_py, r'APP_ENV_PREFIX\s*=\s*"([^"]+)"', SHARED_KEYS)
    agent_pk_body = _grep(
        shared_keys_py, r'def agent_pk\([^)]*\).*?return (f"[^"]+")', SHARED_KEYS,
        flags=re.S,
    )
    if agent_pk_body != 'f"{_app(app_env)}#AGENT#{slug}"':
        raise SystemExit(f"sync_contract: agent_pk composition changed: {agent_pk_body}")

    contract = {
        "contract_version": CONTRACT_VERSION,
        "engine_version": ENGINE_VERSION,
        "source": {
            "repo": "oppizi/agent-infra",
            "files": list(SOURCE_FILES),
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
        # No runtimeArn: since AHP-390 WP-B the CONFIG row carries none. No
        # appId / secretName / slackAuthorized: AHP-1117 moved Slack state onto
        # the binding, and register_agent writes none of them (AHP-1837).
        # `version` starts at 1 and `channels` is the Soleon Chat membership
        # every live create gets — each guarded against the source above.
        "projection_constants": {
            "SK": "CONFIG",
            "agentType": "slack",
            "endpointId": "",
            "registrationOpen": "false",
            "visibility": "restricted",
            "version": 1,
            "channels": ["soleon"],
        },
        "register_agent_row_keys": row_keys,
        **idx,
    }

    # A frozen tripwire, deliberately: a framework appearing or disappearing on the
    # platform changes what the shipped contract validates, so a human must
    # consciously edit this line rather than let a regeneration absorb it silently.
    #
    # Updated 2026-07-28: the platform archived `nanobot` and `openclaw`, leaving
    # `_FRAMEWORK_VALUES = {"maverick"}`. This tripwire had been firing ever since —
    # unseen, because every test that reaches it lives in the module that skips
    # outside a monorepo checkout, and neither repo has CI. Meanwhile the shipped
    # contract.json still offered all three, so the plugin would locally validate an
    # agent the platform answers 400. Regenerated together with this fix.
    #
    # Updated 2026-11-16 (AHP-1837): the platform set is {"maverick", "mock"}
    # (AHP-956); `mock` is the E2E lane and stays out of what the plugin offers.
    sanity = {"maverick", "mock"}
    if set(platform_frameworks) != sanity:
        raise SystemExit(
            f"sync_contract: framework set drifted: {platform_frameworks} "
            f"(expected {sorted(sanity)}). If the platform really changed its "
            "framework set, update `sanity` here in the same commit."
        )
    if RETIRED_CONTRACT_KEYS & contract.keys():
        raise SystemExit(f"sync_contract: retired keys would ship: {sorted(RETIRED_CONTRACT_KEYS & contract.keys())}")
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
    ap.add_argument("--out", type=Path, default=here.parents[1] / "plugins/soleon/contract.json")
    args = ap.parse_args()

    contract = extract(args.repo_root)
    args.out.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
