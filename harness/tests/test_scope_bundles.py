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
# The OAuth client identity the plugins ship.
#
# A plugin with no `oauth.clientId` cannot sign in AT ALL — the Soleon
# authorization proxy rejects `/authorize` without one, and Claude Code, finding
# no client id and no dynamic-registration endpoint, fails with "Incompatible
# auth server: does not support dynamic client registration". That shipped in
# 0.3.0 with a fully green suite, because every check above constrains the SCOPE
# pin and nothing constrained the client identity.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_plugin_ships_a_usable_oauth_client_id(bundle):
    """Present, correctly named, and not a shape that fails at runtime."""
    oauth = _oauth(bundle)
    assert "client_id" not in oauth, (
        f"{bundle}/.mcp.json uses the snake_case key `client_id`. The documented "
        "schema key is `clientId`; `client_id` is silently IGNORED, which leaves the "
        "plugin unable to sign in while every other check here passes."
    )
    client_id = oauth.get("clientId")
    assert isinstance(client_id, str) and client_id, (
        f"{bundle}/.mcp.json declares no oauth.clientId — the plugin cannot complete "
        "an OAuth sign-in. Regenerate with "
        "`python3 harness/sync_scope_bundles.py --repo-root <agent-infra>`"
    )
    assert client_id == client_id.strip(), (
        f"{bundle} clientId {client_id!r} carries surrounding whitespace — the proxy "
        "compares client_id with an exact match, so this is a silent 401 that every "
        "non-empty check passes"
    )


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_client_id_is_a_literal_not_a_user_config_reference(bundle):
    """`${user_config.…}` does NOT expand inside the `oauth` block.

    This is the trap the whole file exists to prevent, because the line directly
    above `clientId` in the same document IS a `${user_config.…}` reference — so
    the symmetric-looking edit is the natural one to make, and it fails silently:
    Claude Code sends the unexpanded string verbatim as the client_id and the proxy
    401s. Verified empirically against Claude Code 2.1.220, with the option both
    unset and explicitly configured.
    """
    client_id = _oauth(bundle)["clientId"]
    assert "${" not in client_id, (
        f"{bundle} clientId is {client_id!r} — a `${{user_config.…}}` reference. "
        "Claude Code expands those in the server `url` but NOT inside `oauth`, so "
        "this ships the literal placeholder as the client id and no user can sign "
        "in. Use the literal value; the documented override is "
        "`claude mcp add --transport http --client-id <id> <url>`."
    )
    assert "user_config" not in client_id


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_shipped_oauth_defaults_match_the_vendored_artifact_as_a_pair(bundle):
    """The content check for the client identity, with no platform source.

    Counterpart to `test_shipped_pins_match_the_vendored_artifact`: shape checks
    cannot tell a correct client id from a well-formed wrong one.

    Asserted as a PAIR because the two values are env-coupled — a `server_url`
    pointing at staging with dev's client id is a guaranteed 401, and each value
    passes its own individual check. The `server_url` side is compared against the
    RESOLVED `plugin.json` default, not the `.mcp.json` literal: all three
    `.mcp.json` files carry the identical `${user_config.server_url}` string, which
    carries no information about where the plugin actually points.
    """
    artifact = json.loads(VENDORED.read_text())
    shipped = (
        _plugin_json(bundle)["userConfig"]["server_url"]["default"],
        _oauth(bundle)["clientId"],
    )
    published = (artifact["defaultServerUrl"], artifact["defaultClientId"])
    assert shipped == published, (
        f"{bundle}'s (server_url, clientId) pair has drifted from "
        f"plugins/scope_bundles.json.\n  shipped:   {shipped}\n  artifact:  {published}\n"
        "These are env-coupled: a half-completed environment switch is a 401 that "
        "every individual check passes. Regenerate with "
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
def test_server_url_config_screen_warns_that_the_pair_is_coupled(bundle):
    """The `/plugin` Configure screen must not invite a half-completed override.

    That screen offers `server_url` and — necessarily — NOT `client_id`, because
    a `client_id` field could not be read from the `oauth` block
    (`test_no_client_id_user_config_field_is_advertised`). So the ONE action the
    screen makes easy is exactly the action that breaks sign-in: repoint the URL
    at another environment while still sending this one's client id, yielding
    `401 unauthorized_client` with nothing in the UI explaining why.

    The description is the only text rendered there, so it is the only place that
    warning can live. It previously read "point at another environment for
    development or testing" — an active invitation to the broken path.
    """
    desc = _plugin_json(bundle)["userConfig"]["server_url"]["description"]
    assert "client id" in desc.lower() or "client_id" in desc.lower(), (
        f"{bundle}'s server_url description does not mention the client ID, so the "
        "Configure screen invites changing the URL alone — a guaranteed 401"
    )
    assert "--client-id" in desc, (
        f"{bundle}'s server_url description must name the working override "
        "(`claude mcp add --client-id`), not just warn that the URL is insufficient"
    )


@pytest.mark.parametrize("bundle", ["observer", "builder", "admin"])
def test_readme_override_recipe_uses_add_json_with_this_bundles_exact_pin(bundle):
    """The documented override must not silently downgrade the token.

    `claude mcp add` has NO way to set `oauth.scopes` (its `--scope` flag is the
    *config* scope — local/user/project — an unrelated setting with a colliding
    name). A server added that way falls back to whatever the resource advertises
    as its default, which is the **read-only observer set**. Verified live against
    ahp-396: `add --client-id` produced the 8 observer scopes for a request that
    should have carried builder's 16.

    So a README telling a builder/admin user to use `claude mcp add` hands them a
    read-only token while everything looks correctly installed — the same
    "green but broken" shape as the missing clientId this release fixes.

    The recipe must therefore use `add-json` AND embed this bundle's exact pin.
    """
    readme = (PLUGINS_DIR / bundle / "README.md").read_text()
    artifact_pin = json.loads(VENDORED.read_text())["bundles"][bundle]

    assert "claude mcp add-json" in readme, (
        f"{bundle}/README.md does not document `claude mcp add-json`"
    )
    assert "claude mcp add --transport" not in readme, (
        f"{bundle}/README.md still documents `claude mcp add --transport`, which "
        "cannot carry oauth.scopes and silently yields a read-only token"
    )
    assert artifact_pin in readme, (
        f"{bundle}/README.md's override recipe does not embed the bundle's exact "
        "pin from plugins/scope_bundles.json — a hand-edited or stale scope string "
        "would grant the wrong consent. Regenerate the recipe from the artifact."
    )


def test_published_client_id_is_not_marked_sensitive_anywhere():
    """It is an identifier, not a credential — and `sensitive` has a real cost.

    Sensitive values are routed to the OS keychain, which shares a small budget
    with the OAuth tokens themselves. Spending it on a public PKCE identifier buys
    nothing.
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
        assert server["oauth"]["clientId"] == "fixtureclientid0000000000"
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
