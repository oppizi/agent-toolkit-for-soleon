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

# There is deliberately no second platform source. A previous release published
# OAuth defaults (a Cognito client id, a server URL, an env name) from
# `stacks/_plugin_publication.py` so the plugins could ship a client id; the
# server now accepts the client identity the Anthropic harnesses publish for
# themselves, so plugins carry none and that whole file is gone. Each plugin's
# `server_url` default is hand-maintained in its own manifest, which is the shape
# this repo had before that release.


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


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def render_artifact(pins: dict[str, str]) -> dict:
    """The artifact body + its digest.

    Must stay byte-identical with the platform's ``scripts/gen_scope_bundles.py``:
    both write this file, and `test_no_drift_against_live_platform_source` compares
    them. The payload is the scope taxonomy only — the digest exists so a
    hand-edited vendored pin fails the integrity self-check instead of silently
    granting the wrong consent.
    """
    payload = {"bundleOrder": list(pins), "bundles": dict(pins)}
    return {**payload, "sha256": payload_sha256(payload)}


def artifact_text(pins: dict[str, str]) -> str:
    return json.dumps(render_artifact(pins), indent=2, sort_keys=True) + "\n"


def mcp_json_path(bundle: str) -> Path:
    return PLUGINS / bundle / ".mcp.json"


def plugin_json_path(bundle: str) -> Path:
    return PLUGINS / bundle / ".claude-plugin" / "plugin.json"


def rendered_mcp_json(bundle: str, scopes: str) -> str:
    """The `.mcp.json` for a bundle: `oauth.scopes`, and deliberately nothing else.

    Reads the existing file so hand-maintained keys (`type`, `url`, the server key)
    survive regeneration — this script owns the scope pin, not the whole document.

    **There is no `clientId`, and its absence is the fix.** An earlier release
    published a literal Cognito client id here, because the server advertised no
    client-registration arm the harness could take and a plugin without one could
    not sign in at all. That literal was a workaround with three costs: it pinned
    every install to ONE environment (`${user_config.…}` provably does not expand
    inside the `oauth` block, so the field could not be parameterised — an
    interpolated value arrived at `/authorize` as
    `%24%7Buser_config.client_id%7D` even with the option explicitly configured);
    pointing a plugin elsewhere therefore needed a CLI override with a
    hand-pasted scope list, re-creating the copy-drifts failure mode the scope
    pin exists to prevent; and the marketplace sync strips the entire `oauth`
    block, so it never reached Desktop/Web users regardless.

    The server now accepts the client identity the Anthropic harnesses already
    publish — a Client ID Metadata Document URL — so the plugin carries no
    environment-specific value at all and `server_url` alone selects the target.
    Do not re-add a `clientId`: it would re-couple every install to one
    environment and buy nothing.
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
    # Actively REMOVE a stale literal rather than merely ceasing to emit one: the
    # document is read back from disk, so a `clientId` written by an earlier
    # release would survive regeneration untouched and keep shipping silently.
    oauth.pop("clientId", None)
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
    for required in (source,):
        if not required.is_file():
            print(
                f"ERROR: {required} not found. Pass --repo-root pointing at an "
                "agent-infra checkout.",
                file=sys.stderr,
            )
            return 2

    try:
        pins = expected_pins(source.read_text())
    except ExtractionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    targets: list[tuple[Path, str]] = [
        (VENDORED_ARTIFACT, artifact_text(pins))
    ]
    # NOTE: `plugin.json` is no longer generated. Its only generated field was
    # `userConfig.server_url.default`, which came from the deleted publication
    # source; it is hand-maintained per plugin again, as it was before that
    # release. This script owns the scope pin and nothing else.
    for bundle, scopes in pins.items():
        targets.append(
            (mcp_json_path(bundle), rendered_mcp_json(bundle, scopes))
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
