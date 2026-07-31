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
from sync_scope_bundles import (  # noqa: E402
    ExtractionError,
    expected_pins,
    expected_publication,
)


def _mcp_json(bundle: str) -> dict:
    return json.loads((PLUGINS_DIR / bundle / ".mcp.json").read_text())


def _oauth(bundle: str) -> dict:
    (server,) = _mcp_json(bundle)["mcpServers"].values()
    return server["oauth"]


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


# ---------------------------------------------------------------------------
# The OAuth client identity the plugins ship — which is now NONE, deliberately.
#
# The history matters, because this guard's inversion looks like a weakening and
# is not. 0.3.0 shipped no client id and could not sign in at all, so a later
# release published a literal Cognito client id. That worked, and it was a
# workaround: it pinned every install to ONE environment (`${user_config.…}` does
# not expand inside the `oauth` block, so the field could not be parameterised);
# it pushed anyone targeting another environment onto a CLI override carrying a
# hand-pasted scope list, re-creating the very copy-drift this file exists to
# prevent; and the claude.ai marketplace sync strips the whole `oauth` block, so
# Desktop/Web users never received it regardless.
#
# The server now accepts the client identity the Anthropic harnesses already
# publish — a Client ID Metadata Document URL — so a plugin needs none. The
# failure mode worth catching is therefore no longer a MISSING literal but a
# REINTRODUCED one.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_plugin_ships_no_oauth_client_id(bundle):
    """A client id must not come back — in either spelling.

    Both the documented `clientId` and the silently-ignored snake_case
    `client_id` are refused, so a well-meaning "restore the client id" edit fails
    loudly here instead of shipping an environment-locked plugin with an
    otherwise-green suite.
    """
    oauth = _oauth(bundle)
    for key in ("clientId", "client_id"):
        assert key not in oauth, (
            f"{bundle}/.mcp.json declares oauth.{key}={oauth[key]!r}. Plugins no "
            "longer carry a client id — the server accepts the harness's published "
            "Client ID Metadata Document, so `server_url` is the only "
            "environment-specific value left. Re-adding this pins every install to "
            "one environment, and the marketplace sync strips it anyway. Regenerate "
            "with `python3 harness/sync_scope_bundles.py --repo-root <agent-infra>`"
        )


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_shipped_server_url_matches_the_vendored_artifact(bundle):
    """The content check for where a plugin points, with no platform source.

    Counterpart to `test_shipped_pins_match_the_vendored_artifact`: shape checks
    cannot tell a correct URL from a well-formed wrong one.

    Compared against the RESOLVED `plugin.json` default, not the `.mcp.json`
    literal: all three `.mcp.json` files carry the identical
    `${user_config.server_url}` string, which says nothing about where the plugin
    actually points.

    This previously asserted a (server_url, clientId) PAIR, because the two were
    env-coupled: a half-completed environment switch 401'd while each value passed
    its own individual check. With the client id gone there is only one value left
    to get wrong, which is precisely the point of removing it.
    """
    artifact = json.loads(VENDORED.read_text())
    shipped = _plugin_json(bundle)["userConfig"]["server_url"]["default"]
    published = artifact["defaultServerUrl"]
    assert shipped == published, (
        f"{bundle}'s server_url default has drifted from plugins/scope_bundles.json."
        f"\n  shipped:   {shipped}\n  artifact:  {published}\n"
        "Regenerate with "
        "`python3 harness/sync_scope_bundles.py --repo-root <agent-infra>`"
    )


def test_no_client_id_user_config_field_is_advertised():
    """Do not offer an override that cannot work.

    `${user_config.client_id}` is unusable inside the `oauth` block, so a
    `client_id` userConfig field would prompt installers for a value that is read
    by nothing. The real override is the CLI `--client-id` flag, documented in the
    plugin READMEs.
    """
    for bundle in ("observer", "builder", "admin"):
        user_config = _plugin_json(bundle)["userConfig"]
        assert "client_id" not in user_config, (
            f"{bundle} declares a client_id userConfig field. It cannot reach the "
            "oauth block, so it is an override that silently does nothing — worse "
            "than no override at all. Document `claude mcp add --client-id` instead."
        )


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_server_url_config_screen_does_not_warn_about_a_client_id(bundle):
    """The Configure screen's one action must now be the one that WORKS.

    This guard is inverted from its previous form, and the inversion is the whole
    point of the release. The screen offers `server_url` and necessarily not a
    client id. While the plugin shipped a client-id literal, repointing the URL
    alone was a guaranteed `401 unauthorized_client`, so the description had to
    warn against the single action the screen made easy — and name a CLI override
    instead.

    The plugin no longer carries a client id, so the URL alone is sufficient and
    that warning is now FALSE. Leaving it would be worse than useless: it would
    talk users out of the supported path and toward a hand-built second server.
    """
    desc = _plugin_json(bundle)["userConfig"]["server_url"]["description"]
    lowered = desc.lower()
    assert "client id" not in lowered and "client_id" not in lowered, (
        f"{bundle}'s server_url description still mentions a client ID: {desc!r}. "
        "There isn't one any more — the URL alone selects the environment, and "
        "this text would send users down a path that no longer exists."
    )
    assert "--client-id" not in desc, (
        f"{bundle}'s server_url description still names the `--client-id` "
        "override, which is obsolete: {desc!r}"
    )
    assert "break sign-in" not in lowered, (
        f"{bundle}'s server_url description still warns that changing the URL "
        f"breaks sign-in. It does not: {desc!r}"
    )


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_readme_documents_no_hand_built_server_recipe(bundle):
    """The README must not send users back to a hand-pasted server definition.

    Also inverted, for the same reason as the Configure-screen guard. While a
    client-id literal shipped, changing environments REQUIRED registering a
    second server by hand, and the recipe had to use `claude mcp add-json` with
    the bundle's exact scope pin embedded — because `claude mcp add` cannot set
    `oauth.scopes` at all (its `--scope` flag is the *config* scope, an unrelated
    setting with a colliding name), so a server added that way silently fell back
    to the read-only observer set. Verified live: `add --client-id` produced 8
    observer scopes for a request that should have carried builder's 16.

    That recipe was itself a defect. A scope list pasted into a README is a
    hand-maintained copy of the pin, and hand-maintained copies drift — which is
    the exact failure the vendored artifact and these tests exist to prevent. The
    URL setting now does the whole job, so the recipe is gone and must not
    return.
    """
    readme = (PLUGINS_DIR / bundle / "README.md").read_text()
    artifact_pin = json.loads(VENDORED.read_text())["bundles"][bundle]

    assert "claude mcp add-json" not in readme, (
        f"{bundle}/README.md documents a hand-built `claude mcp add-json` server. "
        "Changing environments is now the server-URL setting alone; a hand-built "
        "definition re-introduces a second place for the scope pin to drift."
    )
    assert "claude mcp add --transport" not in readme, (
        f"{bundle}/README.md documents `claude mcp add --transport`, which cannot "
        "carry oauth.scopes and silently yields a read-only token"
    )
    assert artifact_pin not in readme, (
        f"{bundle}/README.md embeds the bundle's full scope pin as literal text. "
        "That is a hand-maintained copy of the artifact and will drift from it — "
        "the plugin's own .mcp.json is the only place the pin belongs."
    )


def test_no_user_config_field_is_marked_sensitive():
    """None of these values is a credential — and `sensitive` has a real cost.

    Sensitive values are routed to the OS keychain, which shares a small budget
    with the OAuth tokens themselves. Spending it on a server URL buys nothing.

    (This assertion has always ranged over every `userConfig` field; it was named
    for the published client id, which no longer exists.)
    """
    for bundle in ("observer", "builder", "admin"):
        for name, field in _plugin_json(bundle)["userConfig"].items():
            assert not field.get("sensitive"), (
                f"{bundle}'s userConfig field {name!r} is marked sensitive; none of "
                "these values are secrets"
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


def test_expected_publication_maps_constants_onto_artifact_keys():
    published = expected_publication(
        'PUBLISHED_ENV = "demo"\n'
        'PUBLISHED_CLIENT_ID = "abc123"\n'
        'PUBLISHED_SERVER_URL = "https://mcp-demo.example.com/mcp"\n'
    )
    assert published == {
        "defaultEnv": "demo",
        "defaultClientId": "abc123",
        "defaultServerUrl": "https://mcp-demo.example.com/mcp",
    }


def test_expected_publication_fails_loud_on_missing_declarations():
    with pytest.raises(ExtractionError, match="PUBLISHED_SERVER_URL not found"):
        expected_publication('PUBLISHED_ENV = "demo"\nPUBLISHED_CLIENT_ID = "abc"\n')


@pytest.mark.parametrize("bad", ['" abc123"', '"abc123 "', '""'])
def test_expected_publication_rejects_whitespace_padded_or_empty_values(bad):
    """A padded client id survives every "is it set?" check and 401s at runtime."""
    with pytest.raises(ExtractionError, match="PUBLISHED_CLIENT_ID"):
        expected_publication(
            'PUBLISHED_ENV = "demo"\n'
            f"PUBLISHED_CLIENT_ID = {bad}\n"
            'PUBLISHED_SERVER_URL = "https://mcp-demo.example.com/mcp"\n'
        )


# ---------------------------------------------------------------------------
# Positive control — a --check that never fails is indistinguishable from a
# working one, so prove it detects a mutation.
# ---------------------------------------------------------------------------


# The publication half of the fake platform tree. Deliberately NOT the real
# published values: a fixture that happened to match production would let a
# generator bug that ignores the source entirely still pass.
_FIXTURE_PUBLICATION = '''\
PUBLISHED_ENV = "fixture"
PUBLISHED_CLIENT_ID = "fixtureclientid0000000000"
PUBLISHED_SERVER_URL = "https://mcp-fixture.example.com/mcp"
'''


def _fake_platform_root(tmp_path: Path) -> Path:
    """A minimal agent-infra-shaped tree carrying only the files we extract from."""
    root = tmp_path / "fake-agent-infra"
    (root / "stacks").mkdir(parents=True)
    (root / "stacks" / "_mcp_scopes.py").write_text(_FIXTURE_SOURCE)
    (root / "stacks" / "_plugin_publication.py").write_text(_FIXTURE_PUBLICATION)
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


def test_generator_takes_the_client_id_from_platform_source(tmp_path):
    """The published defaults are DERIVED, not baked into the generator.

    Without this, a generator that hardcoded the production client id would pass
    every other test in this file: the shipped files would match the artifact, the
    artifact would match its own digest, and `--check` would report clean — while
    the value silently ignored the source it claims to read. The fake platform tree
    carries deliberately non-production values, so anything hardcoded shows up as a
    mismatch here.

    It also pins the client id into the digest chain: mutating it in the vendored
    artifact alone must make `--check` fail.
    """
    clone = tmp_path / "toolkit"
    shutil.copytree(BET_ROOT / "plugins", clone / "plugins")
    (clone / "harness").mkdir()
    shutil.copy(BET_ROOT / "harness/sync_scope_bundles.py", clone / "harness")

    platform = _fake_platform_root(tmp_path)
    (platform / "stacks" / "_mcp_scopes.py").write_text(
        _FIXTURE_SOURCE.replace('("watcher", "maker", "boss")',
                                '("observer", "builder", "admin")')
        .replace('"watcher"', '"observer"')
        .replace('"maker"', '"builder"')
        .replace('"boss"', '"admin"')
    )

    seed = subprocess.run(
        [sys.executable, str(clone / "harness/sync_scope_bundles.py"),
         "--repo-root", str(platform)],
        capture_output=True, text=True,
    )
    assert seed.returncode == 0, seed.stderr

    artifact = json.loads((clone / "plugins/scope_bundles.json").read_text())
    assert artifact["defaultClientId"] == "fixtureclientid0000000000", (
        "the generator did not take the client id from platform source — it is "
        f"hardcoded or read from elsewhere (got {artifact['defaultClientId']!r})"
    )

    for bundle in ("observer", "builder", "admin"):
        doc = json.loads((clone / f"plugins/{bundle}/.mcp.json").read_text())
        (server,) = doc["mcpServers"].values()
        # The generator must NOT write a client id into a plugin, even though the
        # platform source still declares one. The two facts are now separate: the
        # constant remains readable for operators diagnosing a legacy-arm
        # mismatch, but nothing published to users carries it.
        assert "clientId" not in server["oauth"], (
            "the generator wrote a clientId into a plugin — plugins are "
            "environment-neutral now and must carry only the scope pin"
        )
        manifest = json.loads(
            (clone / f"plugins/{bundle}/.claude-plugin/plugin.json").read_text()
        )
        assert (
            manifest["userConfig"]["server_url"]["default"]
            == "https://mcp-fixture.example.com/mcp"
        ), f"{bundle}'s server_url default was not generated from platform source"

    # And the client id must be inside the digested payload, not beside it.
    target = clone / "plugins/scope_bundles.json"
    doc = json.loads(target.read_text())
    doc["defaultClientId"] = "tamperedclientid000000000"
    target.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    assert _run_check(platform, clone).returncode != 0, (
        "--check reported clean after the vendored client id was hand-edited — a "
        "dead client id would ship with a green suite"
    )


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


def test_changelog_top_section_matches_the_shipped_version():
    """A changelog whose newest heading isn't the shipped version is worse than none.

    The rollback plan leans on the version being meaningful, and the version is
    auto-bumped by a hook on any bundle content change — so the heading drifts
    silently unless something checks it. (It did: the READMEs bumped 0.3.1 -> 0.3.2
    while the heading still said 0.3.1.)
    """
    changelog = (BET_ROOT / "CHANGELOG.md").read_text()
    top = next(
        line[3:].strip()
        for line in changelog.splitlines()
        if line.startswith("## ")
    )
    versions = {_plugin_json(b)["version"] for b in ("observer", "builder", "admin")}
    assert len(versions) == 1, f"bundles disagree on version: {versions}"
    shipped = versions.pop()
    assert top == shipped, (
        f"CHANGELOG's newest section is {top!r} but the plugins ship {shipped!r}. "
        "The version-bump hook fires on any bundle content change; update the "
        "heading in the same commit."
    )
