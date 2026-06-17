"""Contract drift guard — re-runs the source-text extraction and diffs against
the shipped plugin/contract.json. This is the CI tripwire that keeps
contract-as-data honest: if index.py/registry.py/shared_keys.py change, this
fails loudly instead of letting installed plugins emit offline-green payloads
that 400 live."""
import json

import pytest
from pathlib import Path

import sys

BET_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BET_ROOT / "harness"))

from sync_contract import extract  # noqa: E402

REPO_ROOT = BET_ROOT.parents[1]

# These tests re-extract the contract from agent-infra platform SOURCE — they
# can only run inside the agent-infra monorepo checkout, where the drift guard
# belongs. A standalone (public-repo) clone has no platform source to diff
# against; the shipped contract.json is the artifact of record there.
pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "lambda/ui_admin/index.py").is_file(),
    reason="contract drift guard requires the agent-infra monorepo checkout "
    "(plugin contract.json is the artifact of record in standalone clones)",
)


def test_shipped_contract_matches_fresh_extraction():
    shipped = json.loads((BET_ROOT / "plugin/contract.json").read_text())
    fresh = extract(REPO_ROOT)
    assert shipped == fresh, (
        "plugin/contract.json has drifted from repo source — regenerate with "
        "python3 harness/sync_contract.py"
    )


def test_dynamo_allowed_is_full_set():
    """F13: the envelope check's universe must be the FULL _DYNAMO_ALLOWED set,
    not a partial recollection."""
    fresh = extract(REPO_ROOT)
    assert set(fresh["dynamo_allowed"]) == {
        "displayName", "framework", "registrationOpen", "visibility",
        "grantUsers", "revokeUsers", "slackDefaultChannelId",
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
    "max_config_bytes", "prompt_cache_ttl_values", "guardrail_strengths",
    "pii_actions", "regex_actions", "required_topic_strictness",
    "guardrail_enabled_keys", "eval_scoring_modes", "eval_turn_roles",
    "eval_judge_model_allowlist", "eval_name_max", "tool_upstream_override_types",
    "model_alias_map",
}


def test_v2_keys_present_and_versioned():
    fresh = extract(REPO_ROOT)
    assert fresh["contract_version"] == 2
    assert V2_KEYS <= set(fresh)


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
    byte-identically to BOTH live server files (dual-index)."""
    from allium_to_json import _skill_to_markdown as conv_render
    from validate_offline import _skill_to_markdown as val_render
    mono = _lift_server_fn(
        (REPO_ROOT / "lambda/ui_admin/index.py").read_text(), "_skill_to_markdown"
    )
    split = _lift_server_fn(
        (REPO_ROOT / "lambda/ui_admin_agents/index.py").read_text(), "_skill_to_markdown"
    )
    for s in _adversarial_skills():
        server_mono = mono(s)
        assert server_mono == split(s), f"dual-index render drift on {s}"
        assert conv_render(s) == server_mono, f"converter render drift on {s}"
        assert val_render(s) == server_mono, f"validator render drift on {s}"
