import json
import sys
from pathlib import Path

import pytest

BET_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = BET_ROOT / "plugin"
ASSETS = PLUGIN / "skills/deploy-agent/assets"

sys.path.insert(0, str(ASSETS))
sys.path.insert(0, str(BET_ROOT / "harness"))


@pytest.fixture(scope="session")
def fixture_doc():
    return json.loads((BET_ROOT / "preflight/expected_channelless_config.json").read_text())


@pytest.fixture(scope="session")
def contract():
    return json.loads((PLUGIN / "contract.json").read_text())


@pytest.fixture()
def example_item(fixture_doc):
    # Function-scoped fresh copy — tests mutate it.
    return json.loads(json.dumps(fixture_doc["example_concrete_item"]))


@pytest.fixture()
def good_body():
    return {
        "slug": "campaign-sla-watcher",
        "displayName": "Campaign SLA Watcher",
        "framework": "maverick",
        "appEnv": "dev",
        "dynamoFields": {"visibility": "private"},
        "configFields": {"soul": "You are a careful watcher."},
    }


def write_spec(tmp_path: Path, *, slug="campaign-sla-watcher",
               display_name="Campaign SLA Watcher", framework="maverick",
               visibility="private", soul="You are a careful watcher.\n",
               version_marker=True, extra="", config_extra="") -> Path:
    lines = []
    if version_marker:
        lines.append("-- allium: 3\n")
    lines.append("enum Framework { maverick | nanobot | openclaw }\n")
    lines.append("config {")
    lines.append(f'    slug: String = {json.dumps(slug)}')
    lines.append(f'    display_name: String = {json.dumps(display_name)}')
    lines.append(f"    framework: Framework = {framework}")
    lines.append(f'    visibility: String = {json.dumps(visibility)}')
    lines.append(f'    soul: String = {json.dumps(soul)}')
    if config_extra:
        lines.append(config_extra)
    lines.append("}\n")
    if extra:
        lines.append(extra)
    path = tmp_path / "spec.allium"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
