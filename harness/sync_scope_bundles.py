"""Regenerate each plugin's ``oauth.scopes`` pin from the platform scope taxonomy.

Harness-only — this file never ships inside a plugin.

The three role bundles (`soleon-observer` / `soleon-builder` / `soleon-admin`) pin
the exact OAuth scopes they request. Those pins are **derived** from the platform's
`stacks/_mcp_scopes.py`, never hand-written: a hand-written pin silently drifts the
moment a scope family is added, and a wrong-but-well-formed pin is invisible to any
structural check.

Two consumers, deliberately:

* **This script** does the cross-repo regeneration. It reads the platform source as
  TEXT and extracts the four declarations with `ast` — no import, so it needs
  neither the platform's dependencies nor its package layout. Mirrors
  `sync_contract.py`, which solves the same cross-repo problem for `contract.json`.
* **`plugins/scope_bundles.json`** is the vendored snapshot this script writes
  alongside the pins. `harness/tests/test_scope_bundles.py` verifies the shipped
  pins against it with **no platform source at all**, which is what makes the guard
  work in a standalone clone, in a fork, and in the sibling-worktree layout where
  every "monorepo-only" test skips.

The extraction is factored as the pure `expected_pins(source_text)` so it can be
unit-tested against an inline fixture rather than only against live platform source
that may not be present.

Usage::

    python3 harness/sync_scope_bundles.py --repo-root ../agent-infra
    python3 harness/sync_scope_bundles.py --repo-root ../agent-infra --check
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

BET_ROOT = Path(__file__).resolve().parents[1]
PLUGINS = BET_ROOT / "plugins"
VENDORED_ARTIFACT = PLUGINS / "scope_bundles.json"
SCOPES_SOURCE_REL = Path("stacks") / "_mcp_scopes.py"
# The OAuth defaults the plugins publish. A SECOND platform source file, because
# these are deploy-time values (a Cognito client id, a custom-domain URL) that no
# amount of reading the scope taxonomy produces.
PUBLICATION_SOURCE_REL = Path("stacks") / "_plugin_publication.py"

# The publication constants, and the artifact key each one is published under.
# Key names and the serialization below must stay byte-identical with the
# platform's own `scripts/gen_scope_bundles.py`, since both write this artifact.
PUBLICATION_FIELDS = {
    "PUBLISHED_CLIENT_ID": "defaultClientId",
    "PUBLISHED_SERVER_URL": "defaultServerUrl",
    "PUBLISHED_ENV": "defaultEnv",
}


class ExtractionError(RuntimeError):
    """The platform source did not carry the declarations we derive pins from."""


def _assigned_value(tree: ast.Module, name: str) -> ast.expr:
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign)
            else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                # `AnnAssign.value` is None for a bare annotation (`X: dict`).
                if node.value is None:
                    raise ExtractionError(f"{name} is annotated but not assigned")
                return node.value
    raise ExtractionError(
        f"{name} not found in {SCOPES_SOURCE_REL} — the platform source changed "
        "shape, or --repo-root points at the wrong tree"
    )


def expected_pins(source_text: str) -> dict[str, str]:
    """Derive ``{bundle: "space separated fully-qualified scopes"}`` from platform source.

    Pure — takes the source TEXT, returns the pins. No filesystem, no import, no
    network, so the always-run test suite can exercise it against an inline fixture.

    Scope order follows ``MCP_SCOPE_TAXONOMY``'s declaration order so a regenerated
    pin diffs cleanly instead of reshuffling.
    """
    tree = ast.parse(source_text)

    identifier = ast.literal_eval(_assigned_value(tree, "MCP_RESOURCE_SERVER_IDENTIFIER"))
    taxonomy = ast.literal_eval(_assigned_value(tree, "MCP_SCOPE_TAXONOMY"))
    bundle_order = tuple(ast.literal_eval(_assigned_value(tree, "BUNDLE_ORDER")))
    min_bundle = ast.literal_eval(_assigned_value(tree, "SCOPE_MIN_BUNDLE"))

    unassigned = [name for name in taxonomy if name not in min_bundle]
    if unassigned:
        raise ExtractionError(
            f"scope families with no bundle assignment: {unassigned} — assign them "
            "in the platform's SCOPE_MIN_BUNDLE first"
        )
    unknown = sorted({b for b in min_bundle.values() if b not in bundle_order})
    if unknown:
        raise ExtractionError(f"unknown bundle names in SCOPE_MIN_BUNDLE: {unknown}")

    pins: dict[str, str] = {}
    for index, bundle in enumerate(bundle_order):
        included = set(bundle_order[: index + 1])
        pins[bundle] = " ".join(
            f"{identifier}/{name}"
            for name in taxonomy
            if min_bundle[name] in included
        )
    return pins


def expected_publication(source_text: str) -> dict[str, str]:
    """Derive the published OAuth defaults from the platform's publication source.

    Same shape as :func:`expected_pins` — pure, takes source TEXT, no import — but
    a different question. The scope pins are *derived* from a taxonomy; these are
    *reviewed constants*, because a Cognito client id and a custom-domain URL are
    deploy-time facts that no source file computes.

    Extracting them here rather than hand-copying them is the whole point: it makes
    the value the plugins ship provably the value the platform believes it
    published, and the artifact's sha256 then carries that guarantee into a
    standalone clone with no platform source at all.
    """
    tree = ast.parse(source_text)
    published: dict[str, str] = {}
    for const_name, artifact_key in PUBLICATION_FIELDS.items():
        value = ast.literal_eval(_assigned_value(tree, const_name))
        if not isinstance(value, str) or not value or value != value.strip():
            raise ExtractionError(
                f"{const_name} must be a non-empty string with no surrounding "
                f"whitespace, got {value!r}. The OAuth proxy compares client_id with "
                "an exact match, so a stray space is a silent 401."
            )
        published[artifact_key] = value
    return published


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def render_artifact(pins: dict[str, str], publication: dict[str, str]) -> dict:
    """The artifact body + its digest.

    Must stay byte-identical with the platform's ``scripts/gen_scope_bundles.py``:
    both write this file, and `test_no_drift_against_live_platform_source` compares
    them. The publication defaults ride INSIDE the digested payload so a hand-edited
    vendored copy fails the integrity self-check rather than shipping a dead
    client id.
    """
    payload = {"bundleOrder": list(pins), "bundles": dict(pins), **publication}
    return {**payload, "sha256": payload_sha256(payload)}


def artifact_text(pins: dict[str, str], publication: dict[str, str]) -> str:
    return json.dumps(render_artifact(pins, publication), indent=2, sort_keys=True) + "\n"


def mcp_json_path(bundle: str) -> Path:
    return PLUGINS / bundle / ".mcp.json"


def plugin_json_path(bundle: str) -> Path:
    return PLUGINS / bundle / ".claude-plugin" / "plugin.json"


def rendered_mcp_json(bundle: str, scopes: str, client_id: str) -> str:
    """The `.mcp.json` for a bundle: `oauth.scopes` + `oauth.clientId`.

    Reads the existing file so hand-maintained keys (`type`, `url`, the server key)
    survive regeneration — this script owns the pin and the client identity, not the
    whole document.

    **`clientId` is a LITERAL, and that is not a shortcut.** `url` next to it is
    `${user_config.server_url}`, so the obvious symmetry would be
    `${user_config.client_id}`. It does not work: Claude Code expands
    `${user_config.…}` in the server `url` but NOT inside the `oauth` block, and it
    fails silently — the unexpanded string is sent verbatim as the `client_id` and
    the authorization proxy 401s. Verified empirically against Claude Code 2.1.220
    with both controls: an interpolated `clientId` arrived at `/authorize` as
    `%24%7Buser_config.client_id%7D` *even with the option explicitly configured*,
    while a literal arrived intact. Reverting to interpolation here reintroduces
    exactly the bug this change fixes, with a green test suite.

    The consequence is that pointing a plugin at another environment needs the CLI
    override (`claude mcp add --transport http --client-id …`), which the plugin
    READMEs document.
    """
    path = mcp_json_path(bundle)
    if not path.exists():
        raise ExtractionError(f"{path} missing — create the plugin skeleton first")
    doc = json.loads(path.read_text())
    servers = doc.get("mcpServers") or {}
    if len(servers) != 1:
        raise ExtractionError(f"{path} must declare exactly one mcpServers entry")
    (server,) = servers.values()
    oauth = server.setdefault("oauth", {})
    oauth["clientId"] = client_id
    oauth["scopes"] = scopes
    # `callbackPort` stays unset on purpose: Claude Code then picks an ephemeral
    # loopback port, which the proxy's redirect allowlist already accepts (any
    # numeric port + exactly `/callback`). Pinning one would only create a way for
    # the flow to fail when that port is busy.
    #
    # `ensure_ascii=False`: these are human-authored marketplace files. The default
    # would rewrite every em-dash in a description as `—`, producing a large
    # spurious diff on regeneration and shipping escapes to a public listing.
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def rendered_plugin_json(bundle: str, server_url: str) -> str:
    """The plugin manifest, with `userConfig.server_url.default` bound to the artifact.

    The server URL and the client id are ENV-COUPLED — a default pointing at one
    environment with a client id from another is a guaranteed 401. Generating both
    from the same artifact is what stops them drifting apart independently.

    There is deliberately no `client_id` userConfig field: it could not be
    referenced from the `oauth` block anyway (see :func:`rendered_mcp_json`), and
    declaring one would advertise an override that silently does nothing.
    """
    path = plugin_json_path(bundle)
    if not path.exists():
        raise ExtractionError(f"{path} missing — create the plugin skeleton first")
    doc = json.loads(path.read_text())
    user_config = doc.get("userConfig") or {}
    if "server_url" not in user_config:
        raise ExtractionError(f"{path} declares no server_url userConfig field")
    user_config["server_url"]["default"] = server_url
    # See `rendered_mcp_json` on `ensure_ascii=False`.
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=BET_ROOT.parents[1] / "agent-infra",
        help=(
            "path to the agent-infra checkout. The default GUESSES a sibling layout "
            "and is usually wrong — pass it explicitly."
        ),
    )
    parser.add_argument(
        "--check", action="store_true",
        help="exit non-zero on drift; write nothing",
    )
    args = parser.parse_args(argv)

    source = args.repo_root / SCOPES_SOURCE_REL
    publication_source = args.repo_root / PUBLICATION_SOURCE_REL
    for required in (source, publication_source):
        if not required.is_file():
            print(
                f"ERROR: {required} not found. Pass --repo-root pointing at an "
                "agent-infra checkout.",
                file=sys.stderr,
            )
            return 2

    try:
        pins = expected_pins(source.read_text())
        publication = expected_publication(publication_source.read_text())
    except ExtractionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    targets: list[tuple[Path, str]] = [
        (VENDORED_ARTIFACT, artifact_text(pins, publication))
    ]
    client_id = publication["defaultClientId"]
    server_url = publication["defaultServerUrl"]
    for bundle, scopes in pins.items():
        targets.append(
            (mcp_json_path(bundle), rendered_mcp_json(bundle, scopes, client_id))
        )
        targets.append(
            (plugin_json_path(bundle), rendered_plugin_json(bundle, server_url))
        )

    drifted = [p for p, want in targets if not p.exists() or p.read_text() != want]

    if args.check:
        if drifted:
            print("DRIFT — regenerate with `python3 harness/sync_scope_bundles.py "
                  f"--repo-root {args.repo_root}`:", file=sys.stderr)
            for path in drifted:
                print(f"  {path.relative_to(BET_ROOT)}", file=sys.stderr)
            return 1
        print(f"OK — {len(targets)} file(s) match the platform taxonomy")
        return 0

    for path, want in targets:
        path.write_text(want)
    for bundle, scopes in pins.items():
        print(f"{bundle}: {len(scopes.split())} scopes")
    print(f"wrote {len(targets)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
