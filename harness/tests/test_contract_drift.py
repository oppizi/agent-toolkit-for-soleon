"""Contract drift guard — re-runs the source-text extraction and diffs against
the shipped plugin/contract.json. This is the tripwire that keeps
contract-as-data honest: if the agents admin unit (`lambda/ui_admin_agents/`),
its shared layer, registry.py or shared_keys.py change, this fails loudly
instead of letting installed plugins emit offline-green payloads that 400 live."""
import json

import pytest
from pathlib import Path

import sys

BET_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BET_ROOT / "harness"))

from sync_contract import RETIRED_CONTRACT_KEYS, SOURCE_FILES, extract  # noqa: E402

REPO_ROOT = BET_ROOT.parents[1]

# These tests re-extract the contract from agent-infra platform SOURCE — they
# can only run inside the agent-infra monorepo checkout, where the drift guard
# belongs. A standalone (public-repo) clone has no platform source to diff
# against; the shipped contract.json is the artifact of record there.
#
# The presence probe is the file every agent validation constant lives in
# since the admin API split. It used to probe the pre-split
# `lambda/ui_admin/index.py`, which still exists but no longer carries the
# constants — so the module ran and the extractor exited (AHP-1837).
pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "lambda/ui_admin_agents/index.py").is_file(),
    reason="contract drift guard requires the agent-infra monorepo checkout "
    "(plugin contract.json is the artifact of record in standalone clones)",
)

SHARED_LAYER = "lambda/admin_shared_layer/python/admin_shared/__init__.py"
AGENTS_COMMON = "lambda/ui_admin_agents/agents_common.py"


def test_shipped_contract_matches_fresh_extraction():
    shipped = json.loads((BET_ROOT / "plugins/soleon/contract.json").read_text())
    fresh = extract(REPO_ROOT)
    assert shipped == fresh, (
        "plugins/soleon/contract.json has drifted from repo source — regenerate "
        "with python3 harness/sync_contract.py"
    )


def test_dynamo_allowed_is_full_set():
    """F13: the envelope check's universe must be the FULL _DYNAMO_ALLOWED set,
    not a partial recollection."""
    fresh = extract(REPO_ROOT)
    # Frozen on purpose — a field entering or leaving _DYNAMO_ALLOWED changes what
    # the plugin will send, so it must be a conscious edit. `description`,
    # `iconColor` and `iconName` were added 2026-07-28: the platform had carried
    # them for a while, but this module skips outside a monorepo checkout so
    # nothing surfaced the drift. `grantUsers` / `revokeUsers` left 2026-11-16
    # (AHP-1837): the platform retired the per-Slack-user grants (AHP-893) and
    # refuses them by name (`_RETIRED_DYNAMO_FIELDS`).
    assert set(fresh["dynamo_allowed"]) == {
        "displayName", "framework", "registrationOpen", "visibility",
        "slackDefaultChannelId", "description", "iconColor", "iconName",
    }


def test_projection_constants_match_frozen_fixture():
    """The contract's vendored constants must equal the frozen oracle's
    required_exact (minus PK/variable fields)."""
    fixture = json.loads(
        (BET_ROOT / "preflight/expected_channelless_config.json").read_text()
    )
    fresh = extract(REPO_ROOT)
    assert fresh["projection_constants"] == fixture["required_exact"]


# ---------- v2: skills + config keys, render parity, dual-index ----------

V2_KEYS = {
    "skill_fields", "skill_slug_pattern", "max_skill_bytes", "max_skills_per_agent",
    "max_config_bytes", "guardrail_strengths",
    "pii_actions", "regex_actions", "required_topic_strictness",
    "guardrail_enabled_keys", "eval_turn_roles",
    "eval_name_max", "tool_upstream_override_types",
    "model_alias_map",
}


def test_v2_keys_present_and_versioned():
    fresh = extract(REPO_ROOT)
    assert fresh["contract_version"] == 2
    assert V2_KEYS <= set(fresh)


def test_retired_keys_absent():
    """AHP-1837: the platform deleted eval scoring modes (standard evals are
    graded, never adjudicated), the eval judge-model allowlist (any catalogue
    model judges) and the promptCacheTtl enum (AHP-1478: caching is always on).
    The contract must not keep advertising them."""
    fresh = extract(REPO_ROOT)
    assert RETIRED_CONTRACT_KEYS == {
        "eval_scoring_modes", "eval_judge_model_allowlist", "prompt_cache_ttl_values",
    }
    assert not (RETIRED_CONTRACT_KEYS & set(fresh))
    shipped = json.loads((BET_ROOT / "plugins/soleon/contract.json").read_text())
    assert not (RETIRED_CONTRACT_KEYS & set(shipped))


def test_source_files_exist_and_are_recorded():
    """Every file the extractor reads exists in the monorepo and is the list the
    shipped contract names as its source — a moved constant fails here first."""
    for rel in SOURCE_FILES:
        assert (REPO_ROOT / rel).is_file(), rel
    assert extract(REPO_ROOT)["source"]["files"] == list(SOURCE_FILES)


def test_model_alias_map_keys_subset_of_aliases():
    fresh = extract(REPO_ROOT)
    concrete = {k for k in fresh["model_alias_map"] if not k.startswith("_")}
    assert concrete <= set(fresh["model_aliases"])
    assert concrete  # at least one real mapping


def _lift_server_fn(src_text: str, name: str):
    """Exec a single top-level function out of un-importable server source.

    `_skill_to_markdown` depends on `_yaml_escape` + the json module, so lift
    both into a shared namespace."""
    import ast
    tree = ast.parse(src_text)
    wanted = {"_yaml_escape", "_skill_to_markdown"}
    picked = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    ns: dict = {"json": json}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "<server>", "exec"), ns)
    return ns[name]


def _adversarial_skills():
    cases = [
        {"id": "a", "name": "a"},                       # name == id → no displayName line
        {"id": "a", "name": "A Name"},                  # displayName rendered
        {"id": "a", "name": "Has: colon"},              # yaml-special → quoted
        {"id": "a", "name": "#hash"},                   # leading-special
        {"id": "a", "name": "line\nbreak"},             # newline → quoted
        {"id": "a", "name": "tab\there", "description": ""},
        {"id": "a", "name": "N", "description": "desc: with colon", "content": "body\n"},
        {"id": "a", "name": "N", "description": "", "content": "x" * 100, "enabled": False},
        {"id": "a", "name": "emoji 🌱", "description": "uñicode"},
    ]
    return cases


def test_skill_render_parity_vs_live_source():
    """The converter's + validator's _skill_to_markdown copies must render
    byte-identically to BOTH live server copies: the agents unit's
    `agents_common.py` and the admin shared layer."""
    from allium_to_json import _skill_to_markdown as conv_render
    from validate_offline import _skill_to_markdown as val_render
    mono = _lift_server_fn((REPO_ROOT / AGENTS_COMMON).read_text(), "_skill_to_markdown")
    split = _lift_server_fn((REPO_ROOT / SHARED_LAYER).read_text(), "_skill_to_markdown")
    for s in _adversarial_skills():
        server_mono = mono(s)
        assert server_mono == split(s), f"server render copies drifted on {s}"
        assert conv_render(s) == server_mono, f"converter render drift on {s}"
        assert val_render(s) == server_mono, f"validator render drift on {s}"
