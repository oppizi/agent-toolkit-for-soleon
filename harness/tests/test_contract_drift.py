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
