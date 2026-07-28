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
                value = node.value if isinstance(node, ast.Assign) else node.value
                if value is None:
                    raise ExtractionError(f"{name} is annotated but not assigned")
                return value
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
    payload = {"bundleOrder": list(pins), "bundles": dict(pins)}
    return {**payload, "sha256": payload_sha256(payload)}


def artifact_text(pins: dict[str, str]) -> str:
    return json.dumps(render_artifact(pins), indent=2, sort_keys=True) + "\n"


def mcp_json_path(bundle: str) -> Path:
    return PLUGINS / bundle / ".mcp.json"


def rendered_mcp_json(bundle: str, scopes: str) -> str:
    """The `.mcp.json` for a bundle, with only `oauth.scopes` varying.

    Reads the existing file so hand-maintained keys (`type`, `url`, the server key)
    survive regeneration — this script owns the pin, not the whole document.
    """
    path = mcp_json_path(bundle)
    if not path.exists():
        raise ExtractionError(f"{path} missing — create the plugin skeleton first")
    doc = json.loads(path.read_text())
    servers = doc.get("mcpServers") or {}
    if len(servers) != 1:
        raise ExtractionError(f"{path} must declare exactly one mcpServers entry")
    (server,) = servers.values()
    server.setdefault("oauth", {})["scopes"] = scopes
    return json.dumps(doc, indent=2) + "\n"


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
    if not source.is_file():
        print(
            f"ERROR: {source} not found. Pass --repo-root pointing at an agent-infra "
            "checkout.",
            file=sys.stderr,
        )
        return 2

    try:
        pins = expected_pins(source.read_text())
    except ExtractionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    targets: list[tuple[Path, str]] = [(VENDORED_ARTIFACT, artifact_text(pins))]
    for bundle, scopes in pins.items():
        targets.append((mcp_json_path(bundle), rendered_mcp_json(bundle, scopes)))

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
