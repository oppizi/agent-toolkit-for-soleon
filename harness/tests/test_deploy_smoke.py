"""Mock-MCP deploy smoke — re-runs the OFFLINE pipeline for representative
scenarios and feeds the produced request body through a FAKE
`private_deploy_agent` that stands in for the stateless soleon-agent-toolkit
MCP server. No network, no live deploy.

What it proves at the offline -> deploy boundary (skill Step 7):
  1. the converter still produces a schema_match body for each scenario
     (regression guard on the offline result itself);
  2. the body Step 7 would send is byte-identical to the converter output
     (no mutation between convert and call — the faithful-map rule);
  3. the fake server, applying the SAME server-mirrored envelope contract,
     ACCEPTS every offline-green body (offline-green => live-accept);
  4. auth is enforced (no token => 401) and the offline-green/live-400 trap
     is caught at the boundary (a tampered body => 400) — before any live call.

This is the mock that lets us confirm we still get the same offline results
right up to the deploy, without standing up the remote server.

Harness-only (never ships).
"""
import json

import pytest

import engine  # noqa: F401 — ensures the assets path is importable via conftest
from allium_to_json import convert
from conftest import write_spec
from validate_offline import CONTRACT_PATH, _load, validate, validate_envelope

from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugin"


class DeployRejected(Exception):
    """The fake server's analogue of a non-2xx HTTP response."""

    def __init__(self, status: int, messages: list[str]):
        super().__init__(f"{status}: {messages}")
        self.status = status
        self.messages = messages


class FakeSoleonMcp:
    """Stand-in for the stateless `soleon-agent-toolkit` MCP server's
    `private_deploy_agent` tool. Validates with the SAME contract the live
    server mirrors, and records the exact body it received so the caller can
    assert nothing was mutated on the way to the wire."""

    def __init__(self, contract: dict, valid_token: str = "fake.jwt.token"):
        self.contract = contract
        self.valid_token = valid_token
        self.received: dict | None = None

    def private_deploy_agent(self, body: dict, *, token: str | None = None) -> dict:
        if token != self.valid_token:  # mirrors a 401 on a bad/absent Bearer
            raise DeployRejected(401, ["unauthorized: bad or missing bearer token"])
        # snapshot exactly what crossed the wire
        self.received = json.loads(json.dumps(body))
        errs = validate_envelope(body, self.contract)
        if errs:  # mirrors the live 400 on a contract-invalid body
            raise DeployRejected(400, errs)
        return {"agentId": f"agent-{body['slug']}", "status": "staged",
                "visibility": "private"}


SCENARIOS = ["plain", "config_proposal", "skills"]


def _build(scenario: str, tmp_path) -> dict:
    soul = "watch carefully\n"
    if scenario == "config_proposal":
        spec = write_spec(
            tmp_path, soul=soul,
            config_extra="    config_proposal: String = "
            + json.dumps(json.dumps({"promptCaching": True, "promptCacheTtl": "1h"})),
        )
        return convert(spec, "dev", tmp_path / "out")
    if scenario == "skills":
        d = tmp_path / "skills" / "cite-sources"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\ndescription: Always cite the source.\n---\nAppend [source].\n",
            encoding="utf-8",
        )
        spec = write_spec(tmp_path, soul=soul)
        return convert(spec, "dev", tmp_path / "out", skill_inputs=[str(d)])
    spec = write_spec(tmp_path, soul=soul)
    return convert(spec, "dev", tmp_path / "out")


@pytest.fixture()
def server():
    return FakeSoleonMcp(_load(CONTRACT_PATH))


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_offline_result_unchanged_and_mock_deploy_accepts(scenario, tmp_path, monkeypatch, server):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    result = _build(scenario, tmp_path)
    body, projection = result["body"], result["projection"]

    # (1) the offline oracle is still green for this scenario
    verdict = validate(body, projection)
    assert verdict["schema_match"], verdict

    before = json.loads(json.dumps(body))  # what the converter produced

    # (3) the fake server accepts the offline-green body
    resp = server.private_deploy_agent(body, token=server.valid_token)
    assert resp["status"] == "staged"
    assert resp["agentId"] == f"agent-{body['slug']}"

    # (2) what crossed the wire is byte-identical to the converter output,
    #     and the deploy mutated nothing locally
    assert server.received == before
    assert body == before


def test_missing_token_is_401_and_sends_nothing(tmp_path, monkeypatch, server):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    body = _build("plain", tmp_path)["body"]
    with pytest.raises(DeployRejected) as exc:
        server.private_deploy_agent(body, token=None)
    assert exc.value.status == 401
    assert server.received is None  # auth gate fired before any acceptance


def test_offline_green_live_400_trap_caught_at_boundary(tmp_path, monkeypatch, server):
    """Nesting a top-level key into dynamoFields passes naive checks but 400s
    live. A faulty Step 7 mapping that did this is caught by the fake server
    (server-mirrored contract) — surfacing the trap at the deploy boundary,
    not in production."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    body = _build("plain", tmp_path)["body"]
    body["dynamoFields"]["slug"] = body["slug"]  # tamper
    with pytest.raises(DeployRejected) as exc:
        server.private_deploy_agent(body, token=server.valid_token)
    assert exc.value.status == 400
    assert any("dynamoFields" in m for m in exc.value.messages)
