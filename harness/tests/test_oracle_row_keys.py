"""The frozen channelless oracle must name the keys the platform's writer
actually writes (AHP-1837).

`contract.json` `register_agent_row_keys` is extracted from agent-infra's
`register_agent` (harness/sync_contract.py; kept honest against source by
test_contract_drift.py inside a monorepo checkout). This test runs everywhere,
so an oracle that predicts a key the platform stopped writing, or forbids one
it now writes, fails here instead of drifting unseen — the oracle once
predicted appId / secretName / slackAuthorized and forbade version / deployId
for exactly that reason.
"""
import copy
import json
from pathlib import Path

import pytest

BET_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((BET_ROOT / "plugins/soleon/contract.json").read_text())
FIXTURE = json.loads((BET_ROOT / "preflight/expected_channelless_config.json").read_text())


def row_key_drift(fixture: dict, row_keys: dict) -> list[str]:
    always = set(row_keys["always"])
    written = always | set(row_keys["conditional"])
    required = set(fixture["required_exact"]) | set(fixture["required_variable"])
    allowed = required | set(fixture["optional"])
    forbidden = set(fixture["forbidden"])
    errors = []
    if required - always:
        errors.append(f"oracle requires keys register_agent does not always write: {sorted(required - always)}")
    if always - allowed:
        errors.append(f"register_agent always writes keys the oracle does not allow: {sorted(always - allowed)}")
    if always & forbidden:
        errors.append(f"oracle forbids keys register_agent always writes: {sorted(always & forbidden)}")
    if set(fixture["optional"]) - written:
        errors.append(f"oracle allows keys register_agent never writes: {sorted(set(fixture['optional']) - written)}")
    return errors


def test_oracle_matches_register_agent_row_keys():
    assert row_key_drift(FIXTURE, CONTRACT["register_agent_row_keys"]) == []


def test_example_item_carries_every_required_key():
    required = set(FIXTURE["required_exact"]) | set(FIXTURE["required_variable"])
    assert required <= set(FIXTURE["example_concrete_item"])


@pytest.mark.parametrize("mutate", [
    # The pre-AHP-1837 oracle's drift, one class at a time.
    lambda f: f["required_exact"].update({"slackAuthorized": False}),
    lambda f: f["required_exact"].update({"appId": "", "secretName": ""}),
    lambda f: f["forbidden"].extend(["version", "deployId"]),
    lambda f: f["optional"].pop("tenantId"),
    lambda f: f["required_variable"].pop("gsi2sk"),
    lambda f: f["optional"].update({"retiredField": "never written"}),
], ids=["predicts-slackAuthorized", "predicts-appId-secretName", "forbids-version-deployId",
        "omits-tenantId", "omits-gsi2sk", "allows-unwritten-key"])
def test_drift_is_caught(mutate):
    stale = copy.deepcopy(FIXTURE)
    mutate(stale)
    assert row_key_drift(stale, CONTRACT["register_agent_row_keys"])
