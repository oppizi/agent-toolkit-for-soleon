"""Negative-oracle tests — the validator must be falsifiable.

Every mutation of a known-good item/body must be REJECTED; a vacuously-green
checker cannot pass this suite. One positive control proves the verbatim
fixture example passes.
"""
import pytest

from validate_offline import validate, validate_envelope, validate_projection
import json
from pathlib import Path

BET_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((BET_ROOT / "plugin/contract.json").read_text())
FIXTURE = json.loads((BET_ROOT / "preflight/expected_channelless_config.json").read_text())


def proj_errors(item):
    return validate_projection(item, FIXTURE, CONTRACT)


def env_errors(body):
    return validate_envelope(body, CONTRACT)


# ---------- positive control ----------

def test_verbatim_fixture_example_passes(example_item):
    assert proj_errors(example_item) == []


def test_good_pair_schema_match(example_item, good_body):
    result = validate(good_body, example_item)
    assert result["schema_match"] is True


# ---------- projection negatives ----------

def test_wrong_exact_value_bool_registration_open(example_item):
    example_item["registrationOpen"] = False  # Bool, must be the STRING "false"
    assert proj_errors(example_item)


def test_wrong_exact_value_agent_type(example_item):
    example_item["agentType"] = "channelless"
    assert proj_errors(example_item)


def test_missing_required_variable_created_at(example_item):
    del example_item["createdAt"]
    assert proj_errors(example_item)


def test_forbidden_key_channels_even_empty(example_item):
    example_item["channels"] = []
    assert proj_errors(example_item)


def test_forbidden_key_soul(example_item):
    example_item["soul"] = "text"
    assert proj_errors(example_item)


def test_unlisted_key(example_item):
    example_item["favoriteColor"] = "green"
    assert proj_errors(example_item)


def test_app_env_pk_segment_mismatch(example_item):
    example_item["appEnv"] = "staging"  # PK still says dev
    assert proj_errors(example_item)


def test_app_env_qa_passes_platform_regex_fails_fixture(example_item):
    # "qa" satisfies shared_keys' ^[a-z][a-z0-9-]{1,20}$ but not the fixture set
    example_item["PK"] = "APPENV#qa#AGENT#campaign-sla-watcher"
    example_item["appEnv"] = "qa"
    assert proj_errors(example_item)


def test_bad_created_at_format(example_item):
    example_item["createdAt"] = "2026-06-10 12:00:00"
    assert proj_errors(example_item)


def test_visibility_public_server_valid_fixture_forbidden(example_item):
    example_item["visibility"] = "public"
    assert proj_errors(example_item)


def test_slug_too_short_in_pk(example_item):
    example_item["PK"] = "APPENV#dev#AGENT#a"  # 1 char: passes PK regex, fails SLUG_PATTERN
    assert proj_errors(example_item)


def test_slug_edge_hyphen_in_pk(example_item):
    example_item["PK"] = "APPENV#dev#AGENT#bad-"
    assert proj_errors(example_item)


def test_framework_custom_stale_value(example_item):
    example_item["framework"] = "custom"
    assert proj_errors(example_item)


def test_deploy_accretion_keys_forbidden_at_create(example_item):
    example_item["deployStatus"] = "pending"
    assert proj_errors(example_item)


# ---------- envelope negatives ----------

def test_slug_nested_in_dynamo_fields(good_body):
    good_body["dynamoFields"]["slug"] = good_body.pop("slug")
    assert env_errors(good_body)


def test_model_at_config_fields_top_level(good_body):
    good_body["configFields"]["model"] = "us.anthropic.claude-opus-4-6-v1"
    assert env_errors(good_body)


def test_alias_in_config_model(good_body):
    good_body["configFields"]["config"] = {"model": "fable"}
    assert env_errors(good_body)


def test_extra_top_level_key(good_body):
    good_body["registrationOpen"] = "false"
    assert env_errors(good_body)


def test_channel_id_present(good_body):
    good_body["channelId"] = "C12345"
    assert env_errors(good_body)


def test_registration_open_in_dynamo_fields_plan_forbidden(good_body):
    # Server's _DYNAMO_ALLOWED admits it; plan policy says only visibility ships.
    good_body["dynamoFields"]["registrationOpen"] = "false"
    assert env_errors(good_body)


def test_display_name_not_stripped(good_body):
    good_body["displayName"] = "  Campaign SLA Watcher  "
    assert env_errors(good_body)


def test_display_name_too_long(good_body):
    good_body["displayName"] = "x" * 257
    assert env_errors(good_body)


def test_soul_byte_cap_boundary(good_body):
    cap = CONTRACT["max_soul_bytes"]
    good_body["configFields"]["soul"] = "x" * cap
    assert env_errors(good_body) == []  # exactly at cap passes (strict-greater)
    good_body["configFields"]["soul"] = "x" * (cap + 1)
    assert env_errors(good_body)


def test_missing_soul(good_body):
    del good_body["configFields"]["soul"]
    assert env_errors(good_body)


def test_cross_check_body_projection_divergence(example_item, good_body):
    good_body["framework"] = "nanobot"  # projection says maverick
    result = validate(good_body, example_item)
    assert result["schema_match"] is False
    assert result["cross_errors"]
