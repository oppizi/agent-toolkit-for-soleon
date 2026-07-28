"""The shipped `oauth.scopes` pins must be the real derived scopes.

Split deliberately by what a standalone clone can actually prove.

**Why the split matters.** The obvious design — one drift test that re-derives the
pins from the platform's `stacks/_mcp_scopes.py` — runs in **zero** environments.
`test_contract_drift.py` computes `REPO_ROOT = BET_ROOT.parents[1]`, which in the
sibling-worktree layout resolves to the shared parent directory rather than an
agent-infra checkout, so that module skips; and neither repo has CI. A guard that
never executes is not a guard.

So the load-bearing assertions here need **no platform source at all**. The
generator vendors `plugins/scope_bundles.json` next to the plugins, and the
always-run half checks each shipped pin against it plus the artifact's own sha256.
That catches the failure mode structural checks miss: a pin that is well-formed but
*wrong*. The monorepo-only half still re-runs the real cross-repo derivation when a
platform checkout happens to be reachable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BET_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = BET_ROOT / "plugins"
VENDORED = PLUGINS_DIR / "scope_bundles.json"
BUNDLES = sorted(p.name for p in PLUGINS_DIR.iterdir() if p.is_dir())

sys.path.insert(0, str(BET_ROOT / "harness"))
from sync_scope_bundles import ExtractionError, expected_pins  # noqa: E402


def _mcp_json(bundle: str) -> dict:
    return json.loads((PLUGINS_DIR / bundle / ".mcp.json").read_text())


def _scopes(bundle: str) -> list[str]:
    (server,) = _mcp_json(bundle)["mcpServers"].values()
    return server["oauth"]["scopes"].split()


def _plugin_json(bundle: str) -> dict:
    return json.loads((PLUGINS_DIR / bundle / ".claude-plugin/plugin.json").read_text())


# ---------------------------------------------------------------------------
# Always runs — standalone-safe, no platform source.
# ---------------------------------------------------------------------------


def test_repo_ships_the_three_role_bundles():
    assert BUNDLES == ["admin", "builder", "observer"], (
        f"expected the three role bundles, found {BUNDLES}"
    )


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_pin_is_present_and_well_formed(bundle):
    scopes = _scopes(bundle)
    assert scopes, f"{bundle} has an empty oauth.scopes pin"
    assert len(scopes) == len(set(scopes)), f"{bundle} pin repeats a scope"
    for scope in scopes:
        assert scope.startswith("soleon-mcp/"), (
            f"{bundle} pin {scope!r} is not a fully-qualified soleon-mcp scope"
        )
    assert "openid" not in scopes, (
        f"{bundle} pin must omit openid — the OAuth proxy injects it, so pinning it "
        "here would duplicate it in the authorize request"
    )


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_no_static_bearer_anywhere(bundle):
    """The static-token path is GONE, asserted rather than assumed.

    Deleting the long-lived `soleon_token` JWT from every installation's keychain —
    replaced by a short-lived OAuth access token the client refreshes — is the
    largest security win in this restructure. A stray leftover would silently
    reinstate it.
    """
    (server,) = _mcp_json(bundle)["mcpServers"].values()
    assert "headers" not in server, (
        f"{bundle}/.mcp.json still sets headers — the Authorization bearer is gone, "
        "sign-in is the OAuth flow"
    )
    assert "soleon_token" not in json.dumps(_plugin_json(bundle)), (
        f"{bundle}'s plugin.json still declares soleon_token"
    )


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_server_url_is_configurable_not_hardcoded(bundle):
    (server,) = _mcp_json(bundle)["mcpServers"].values()
    assert server["type"] == "http"
    assert server["url"] == "${user_config.server_url}", (
        f"{bundle} must substitute the user config, never hardcode a URL"
    )
    user_config = _plugin_json(bundle)["userConfig"]
    assert "server_url" in user_config, f"{bundle} declares no server_url userConfig"
    default = user_config["server_url"]["default"]
    assert default.startswith("https://"), f"{bundle} default {default!r} is not https"
    assert "execute-api" not in default, (
        f"{bundle} default {default!r} is a stage-carrying execute-api URL — that "
        "breaks RFC 8414 discovery; use the stage-less custom domain"
    )


def test_bundles_nest_strictly_as_shipped():
    observer, builder, admin = (set(_scopes(b)) for b in ("observer", "builder", "admin"))
    assert observer < builder, "shipped observer pin must be a proper subset of builder"
    assert builder < admin, "shipped builder pin must be a proper subset of admin"


def test_observer_pin_grants_no_write_consent():
    """Observer is the read-only bundle AND the server's PRM default — a write scope
    leaking into it silently widens what an unpinned client requests."""
    writes = [s for s in _scopes("observer") if not s.endswith(".read")]
    assert not writes, f"observer pin carries non-read scopes: {writes}"


def test_shipped_pins_match_the_vendored_artifact():
    """The assertion that catches a well-formed but WRONG pin, with no platform source.

    Everything above constrains pin *shape*. Only this constrains pin *content*, and
    it is the reason the vendored artifact exists at all.
    """
    artifact = json.loads(VENDORED.read_text())
    assert artifact["bundleOrder"] == ["observer", "builder", "admin"]
    for bundle in ("observer", "builder", "admin"):
        assert " ".join(_scopes(bundle)) == artifact["bundles"][bundle], (
            f"{bundle}/.mcp.json's oauth.scopes has drifted from "
            "plugins/scope_bundles.json — regenerate with "
            "`python3 harness/sync_scope_bundles.py --repo-root <agent-infra>`"
        )


def test_vendored_artifact_sha256_covers_its_own_content():
    """Without this the recorded digest would be decorative — it must actually be a
    checksum over the payload it ships beside."""
    artifact = json.loads(VENDORED.read_text())
    recorded = artifact.pop("sha256")
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert actual == recorded, (
        "plugins/scope_bundles.json's sha256 does not match its own content — the "
        "vendored snapshot was hand-edited instead of regenerated"
    )


# ---------------------------------------------------------------------------
# The extractor itself — proven against an inline fixture, so this runs anywhere.
# ---------------------------------------------------------------------------

_FIXTURE_SOURCE = '''
MCP_RESOURCE_SERVER_IDENTIFIER = "demo-rs"

MCP_SCOPE_TAXONOMY: dict[str, str] = {
    "alpha.read": "read alpha",
    "alpha.write": "write alpha",
    "secret.read": "read secret",
}

BUNDLE_ORDER: tuple[str, ...] = ("watcher", "maker", "boss")

SCOPE_MIN_BUNDLE: dict[str, str] = {
    "alpha.read": "watcher",
    "alpha.write": "maker",
    "secret.read": "boss",
}
'''


def test_expected_pins_derives_cumulative_sets_in_taxonomy_order():
    pins = expected_pins(_FIXTURE_SOURCE)
    assert pins == {
        "watcher": "demo-rs/alpha.read",
        "maker": "demo-rs/alpha.read demo-rs/alpha.write",
        "boss": "demo-rs/alpha.read demo-rs/alpha.write demo-rs/secret.read",
    }


def test_expected_pins_rejects_an_unassigned_scope_family():
    """The whole point of the platform's per-scope map: a new family cannot land
    silently. Here it must fail extraction rather than quietly drop the scope."""
    broken = _FIXTURE_SOURCE.replace('    "alpha.write": "maker",\n', "")
    with pytest.raises(ExtractionError, match="no bundle assignment"):
        expected_pins(broken)


def test_expected_pins_rejects_an_unknown_bundle_name():
    broken = _FIXTURE_SOURCE.replace('"alpha.write": "maker"', '"alpha.write": "makr"')
    with pytest.raises(ExtractionError, match="unknown bundle"):
        expected_pins(broken)


def test_expected_pins_fails_loud_on_missing_declarations():
    with pytest.raises(ExtractionError, match="SCOPE_MIN_BUNDLE not found"):
        expected_pins('MCP_RESOURCE_SERVER_IDENTIFIER = "x"\n'
                      'MCP_SCOPE_TAXONOMY = {}\n'
                      'BUNDLE_ORDER = ()\n')


# ---------------------------------------------------------------------------
# Positive control — a --check that never fails is indistinguishable from a
# working one, so prove it detects a mutation.
# ---------------------------------------------------------------------------


def _fake_platform_root(tmp_path: Path) -> Path:
    """A minimal agent-infra-shaped tree carrying only the file we extract from."""
    root = tmp_path / "fake-agent-infra"
    (root / "stacks").mkdir(parents=True)
    (root / "stacks" / "_mcp_scopes.py").write_text(_FIXTURE_SOURCE)
    return root


def _run_check(repo_root: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(cwd / "harness/sync_scope_bundles.py"),
         "--repo-root", str(repo_root), "--check"],
        capture_output=True, text=True,
    )


def test_check_flags_a_mutated_pin(tmp_path):
    """Mutate a shipped `.mcp.json` in a throwaway copy; `--check` must exit non-zero."""
    clone = tmp_path / "toolkit"
    shutil.copytree(BET_ROOT / "plugins", clone / "plugins")
    (clone / "harness").mkdir()
    shutil.copy(BET_ROOT / "harness/sync_scope_bundles.py", clone / "harness")

    platform = _fake_platform_root(tmp_path)
    # Rename the fixture bundles onto the real directory names so the generator
    # addresses the same plugins the repo ships.
    (platform / "stacks" / "_mcp_scopes.py").write_text(
        _FIXTURE_SOURCE.replace('("watcher", "maker", "boss")',
                                '("observer", "builder", "admin")')
        .replace('"watcher"', '"observer"')
        .replace('"maker"', '"builder"')
        .replace('"boss"', '"admin"')
    )

    # Regenerate so the clone is self-consistent against the fake platform...
    seed = subprocess.run(
        [sys.executable, str(clone / "harness/sync_scope_bundles.py"),
         "--repo-root", str(platform)],
        capture_output=True, text=True,
    )
    assert seed.returncode == 0, seed.stderr
    assert _run_check(platform, clone).returncode == 0, "clean tree must report clean"

    # ...then mutate one pin and require the guard to notice.
    target = clone / "plugins/observer/.mcp.json"
    doc = json.loads(target.read_text())
    (server,) = doc["mcpServers"].values()
    server["oauth"]["scopes"] = "demo-rs/secret.read"
    target.write_text(json.dumps(doc, indent=2) + "\n")

    mutated = _run_check(platform, clone)
    assert mutated.returncode != 0, (
        "--check reported clean on a mutated pin — the drift guard is inert"
    )
    assert "observer/.mcp.json" in mutated.stderr


# ---------------------------------------------------------------------------
# Monorepo-only — real cross-repo derivation, when a platform checkout is reachable.
# ---------------------------------------------------------------------------

REPO_ROOT = BET_ROOT.parents[1]
_HAS_PLATFORM_SOURCE = (REPO_ROOT / "stacks" / "_mcp_scopes.py").is_file()


@pytest.mark.skipif(
    not _HAS_PLATFORM_SOURCE,
    reason=(
        "agent-infra source not reachable at the assumed monorepo path — the shipped "
        "artifact + its sha256 are the record (see the always-run half above)"
    ),
)
def test_no_drift_against_live_platform_source():
    result = subprocess.run(
        [sys.executable, str(BET_ROOT / "harness/sync_scope_bundles.py"),
         "--repo-root", str(REPO_ROOT), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"pins have drifted from live platform source:\n{result.stderr}"
    )
