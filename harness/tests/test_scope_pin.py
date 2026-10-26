"""The shipped `oauth.scopes` pin must be the real derived scope set.

Split deliberately by what a standalone clone can actually prove.

**Why the split matters.** The obvious design — one drift test that re-derives the
pin from the platform's `stacks/_mcp_scopes.py` — runs in **zero** environments.
`test_contract_drift.py` computes `REPO_ROOT = BET_ROOT.parents[1]`, which in the
sibling-worktree layout resolves to the shared parent directory rather than an
agent-infra checkout, so that module skips; and CI has no platform checkout. A
guard that never executes is not a guard.

So the load-bearing assertions here need **no platform source at all**. The
generator vendors `plugins/scope_pin.json` next to the plugin, and the always-run
half checks the shipped pin against it plus the artifact's own sha256. That
catches the failure mode structural checks miss: a pin that is well-formed but
*wrong*. The monorepo-only half still re-runs the real cross-repo derivation when a
platform checkout happens to be reachable.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BET_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = BET_ROOT / "plugins"
PLUGIN = PLUGINS_DIR / "soleon"
VENDORED = PLUGINS_DIR / "scope_pin.json"

sys.path.insert(0, str(BET_ROOT / "harness"))
from sync_scope_pin import NEVER_PIN, ExtractionError, expected_pin  # noqa: E402


def _server() -> dict:
    (server,) = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"].values()
    return server


def _scopes() -> list[str]:
    return _server()["oauth"]["scopes"].split()


def _plugin_json() -> dict:
    return json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())


# ---------------------------------------------------------------------------
# Always runs — standalone-safe, no platform source.
# ---------------------------------------------------------------------------


def test_pin_is_present_and_well_formed():
    scopes = _scopes()
    assert scopes, "the oauth.scopes pin is empty"
    assert len(scopes) == len(set(scopes)), "the pin repeats a scope"
    for scope in scopes:
        assert scope.startswith("soleon-mcp/"), (
            f"pin {scope!r} is not a fully-qualified soleon-mcp scope"
        )
    assert "openid" not in scopes, (
        "the pin must omit openid — the OAuth proxy injects it, so pinning it "
        "here would duplicate it in the authorize request"
    )


def test_no_static_bearer_anywhere():
    """The static-token path is GONE, asserted rather than assumed.

    Deleting the long-lived `soleon_token` JWT from every installation's keychain —
    replaced by a short-lived OAuth access token the client refreshes — was the
    largest security win of the OAuth move. A stray leftover would silently
    reinstate it.
    """
    assert "headers" not in _server(), (
        "plugins/soleon/.mcp.json still sets headers — the Authorization bearer is "
        "gone, sign-in is the OAuth flow"
    )
    assert "soleon_token" not in json.dumps(_plugin_json()), (
        "plugins/soleon/.claude-plugin/plugin.json still declares soleon_token"
    )


def test_server_url_is_configurable_not_hardcoded():
    server = _server()
    assert server["type"] == "http"
    assert server["url"] == "${user_config.server_url}", (
        "the plugin must substitute the user config, never hardcode a URL"
    )
    user_config = _plugin_json()["userConfig"]
    assert "server_url" in user_config, "the plugin declares no server_url userConfig"
    default = user_config["server_url"]["default"]
    assert default.startswith("https://"), f"default {default!r} is not https"
    assert "execute-api" not in default, (
        f"default {default!r} is a stage-carrying execute-api URL — that breaks "
        "RFC 8414 discovery; use the stage-less custom domain"
    )


def test_shipped_pin_matches_the_vendored_artifact():
    """The assertion that catches a well-formed but WRONG pin, with no platform source.

    Everything above constrains pin *shape*. Only this constrains pin *content*, and
    it is the reason the vendored artifact exists at all.
    """
    artifact = json.loads(VENDORED.read_text())
    assert " ".join(_scopes()) == artifact["scopes"], (
        "plugins/soleon/.mcp.json's oauth.scopes has drifted from "
        "plugins/scope_pin.json — regenerate with "
        "`python3 harness/sync_scope_pin.py --repo-root <agent-infra>`"
    )


def test_vendored_artifact_sha256_covers_its_own_content():
    """Without this the recorded digest would be decorative — it must actually be a
    checksum over the payload it ships beside."""
    artifact = json.loads(VENDORED.read_text())
    recorded = artifact.pop("sha256")
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert actual == recorded, (
        "plugins/scope_pin.json's sha256 does not match its own content — the "
        "vendored snapshot was hand-edited instead of regenerated"
    )


def test_readme_lists_exactly_the_pinned_scopes():
    """The README is the consent a user reads before installing; it drifted from
    the pins twice (#20, #21). Checks the listed set, that nothing repeats, and
    the count the line above the code block states (as digits)."""
    text = (PLUGIN / "README.md").read_text()
    heading = "## Scopes this plugin requests"
    assert heading in text, f"plugins/soleon/README.md has no {heading!r} section"
    parts = text.split(heading, 1)[1].split("```", 2)
    assert len(parts) == 3, f"plugins/soleon/README.md: no code block under {heading!r}"
    count_line = next(line for line in parts[0].splitlines() if line.strip())
    listed = parts[1].split()
    pin = _scopes()
    assert len(listed) == len(set(listed)), "plugins/soleon/README.md repeats a scope"
    assert set(listed) == set(pin), (
        "plugins/soleon/README.md scope list differs from the pin: "
        f"missing {sorted(set(pin) - set(listed))}, extra {sorted(set(listed) - set(pin))}"
    )
    assert [int(n) for n in re.findall(r"\d+", count_line)] == [len(pin)], (
        f"plugins/soleon/README.md says {count_line!r}; expected the digit {len(pin)}"
    )


def test_no_pin_requests_a_retired_scope():
    """A retired family grants nothing, and the platform drops it from Cognito once
    no published client pins it — so a pin that still requests it is one deploy
    away from failing the whole sign-in."""
    artifact = json.loads(VENDORED.read_text())
    for scopes in (_scopes(), artifact["scopes"].split()):
        leaked = [s for s in scopes if s.split("/", 1)[-1] in NEVER_PIN]
        assert not leaked, f"the pin requests retired scopes {leaked}"


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
'''


def test_expected_pin_is_every_family_in_taxonomy_order():
    assert expected_pin(_FIXTURE_SOURCE) == (
        "demo-rs/alpha.read demo-rs/alpha.write demo-rs/secret.read"
    )


def _with_retired_scope(retired_decl: str) -> str:
    """The fixture plus a retired-inert ``old.read`` family, platform-shaped."""
    source = _FIXTURE_SOURCE.replace(
        '    "secret.read": "read secret",\n}',
        '    "secret.read": "read secret",\n    "old.read": "Nothing — retired",\n}',
    )
    return source + f"\nRETIRED_INERT_SCOPES: frozenset[str] = {retired_decl}\n"


def test_expected_pin_leaves_retired_inert_scopes_out():
    """The platform keeps a retired family in its taxonomy (so pinned clients stay
    valid), but it grants nothing and must not reach the pin."""
    pin = expected_pin(_with_retired_scope('frozenset({"old.read"})'))
    assert pin == expected_pin(_FIXTURE_SOURCE)


def test_expected_pin_without_the_retirement_declaration_retires_nothing():
    """Older platform trees predate RETIRED_INERT_SCOPES: every family is active."""
    source = _with_retired_scope("frozenset()").replace(
        "RETIRED_INERT_SCOPES: frozenset[str] = frozenset()\n", ""
    )
    assert "demo-rs/old.read" in expected_pin(source).split()


def test_expected_pin_rejects_retiring_a_scope_outside_the_taxonomy():
    with pytest.raises(ExtractionError, match="not in MCP_SCOPE_TAXONOMY"):
        expected_pin(_with_retired_scope('frozenset({"old.read", "ghost.read"})'))


def _with_wiki(retired_decl: str | None) -> str:
    """The fixture plus wiki.read — active, or retired by the platform when
    ``retired_decl`` is given (the shape AHP-975 shipped)."""
    source = _FIXTURE_SOURCE.replace(
        '    "secret.read": "read secret",\n}',
        '    "secret.read": "read secret",\n    "wiki.read": "Agent wiki",\n}',
    )
    if retired_decl is None:
        return source
    return source + f"\nRETIRED_INERT_SCOPES: frozenset[str] = {retired_decl}\n"


def test_expected_pin_refuses_a_platform_tree_from_before_a_retirement():
    """The #21 failure: regenerating from a tree cut before the wiki retirement put
    the scopes back. The generator must refuse that tree, not re-add them."""
    with pytest.raises(ExtractionError, match="retired scope families"):
        expected_pin(_with_wiki(retired_decl=None))


def test_expected_pin_accepts_a_tree_that_retired_or_deleted_the_family():
    """Both later platform states pin nothing retired: AHP-975 keeps the family as
    retired-inert, AHP-981 deletes it from the taxonomy."""
    assert expected_pin(_with_wiki('frozenset({"wiki.read"})')) == expected_pin(
        _FIXTURE_SOURCE
    )


def test_expected_pin_fails_loud_on_missing_declarations():
    with pytest.raises(ExtractionError, match="MCP_SCOPE_TAXONOMY not found"):
        expected_pin('MCP_RESOURCE_SERVER_IDENTIFIER = "x"\n')


def test_expected_pin_rejects_an_empty_taxonomy():
    with pytest.raises(ExtractionError, match="no active scope family"):
        expected_pin('MCP_RESOURCE_SERVER_IDENTIFIER = "x"\nMCP_SCOPE_TAXONOMY = {}\n')


# ---------------------------------------------------------------------------
# Positive control — a --check that never fails is indistinguishable from a
# working one, so prove it detects a mutation.
# ---------------------------------------------------------------------------


def _run(clone: Path, platform: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(clone / "harness/sync_scope_pin.py"),
         "--repo-root", str(platform), *extra],
        capture_output=True, text=True,
    )


def test_check_flags_a_mutated_pin(tmp_path):
    """Mutate the shipped `.mcp.json` in a throwaway copy; `--check` must exit non-zero."""
    clone = tmp_path / "toolkit"
    shutil.copytree(BET_ROOT / "plugins", clone / "plugins")
    (clone / "harness").mkdir()
    shutil.copy(BET_ROOT / "harness/sync_scope_pin.py", clone / "harness")

    platform = tmp_path / "fake-agent-infra"
    (platform / "stacks").mkdir(parents=True)
    (platform / "stacks" / "_mcp_scopes.py").write_text(_FIXTURE_SOURCE)

    # Regenerate so the clone is self-consistent against the fake platform...
    seed = _run(clone, platform)
    assert seed.returncode == 0, seed.stderr
    assert _run(clone, platform, "--check").returncode == 0, "clean tree must report clean"

    # ...then mutate the pin and require the guard to notice.
    target = clone / "plugins/soleon/.mcp.json"
    doc = json.loads(target.read_text())
    (server,) = doc["mcpServers"].values()
    server["oauth"]["scopes"] = "demo-rs/secret.read"
    target.write_text(json.dumps(doc, indent=2) + "\n")

    mutated = _run(clone, platform, "--check")
    assert mutated.returncode != 0, (
        "--check reported clean on a mutated pin — the drift guard is inert"
    )
    assert "soleon/.mcp.json" in mutated.stderr


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
        [sys.executable, str(BET_ROOT / "harness/sync_scope_pin.py"),
         "--repo-root", str(REPO_ROOT), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"the pin has drifted from live platform source:\n{result.stderr}"
    )
