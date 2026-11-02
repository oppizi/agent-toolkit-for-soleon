"""Regenerate the `soleon` plugin's ``oauth.scopes`` pin from the platform scope taxonomy.

Harness-only — this file never ships inside a plugin.

The plugin requests consent for EVERY active scope family the platform declares.
Scope is a ceiling on consent, never a grant: the server lists only the tools the
signed-in person's permissions allow and re-checks those permissions on every call,
so one plugin can serve every role. The pin is still **derived** from the
platform's `stacks/_mcp_scopes.py`, never hand-written: a hand-written pin silently
drifts the moment a scope family is added or retired, and a wrong-but-well-formed
pin is invisible to any structural check.

Two consumers, deliberately:

* **This script** does the cross-repo regeneration. It reads the platform source as
  TEXT and extracts the taxonomy declarations with `ast` — no import, so it needs
  neither the platform's dependencies nor its package layout. Mirrors
  `sync_contract.py`, which solves the same cross-repo problem for `contract.json`.
* **`plugins/scope_pin.json`** is the vendored snapshot this script writes
  alongside the pin. `harness/tests/test_scope_pin.py` verifies the shipped
  pin against it with **no platform source at all**, which is what makes the guard
  work in a standalone clone, in a fork, and in the sibling-worktree layout where
  every "monorepo-only" test skips.

The extraction is factored as the pure `expected_pin(source_text)` so it can be
unit-tested against an inline fixture rather than only against live platform source
that may not be present.

Usage::

    python3 harness/sync_scope_pin.py --repo-root ../agent-infra
    python3 harness/sync_scope_pin.py --repo-root ../agent-infra --check
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
MCP_JSON = PLUGINS / "soleon" / ".mcp.json"
VENDORED_ARTIFACT = PLUGINS / "scope_pin.json"
SCOPES_SOURCE_REL = Path("stacks") / "_mcp_scopes.py"

# Scope families the platform has retired for good. The pin may never request one again,
# even when --repo-root points at a platform tree from before the retirement. That
# is how the wiki scopes came back: #20 dropped them, then #21 regenerated from an
# agent-infra branch cut before AHP-975 declared them retired. The platform deletes
# a retired family from its taxonomy once no published client pins it (agent-infra
# AHP-981); this list stays, because an old tree can still be passed in.
NEVER_PIN: frozenset[str] = frozenset({"wiki.read", "wiki.write"})


class ExtractionError(RuntimeError):
    """The platform source did not carry the declarations we derive the pin from."""


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


def _retired_inert_scopes(tree: ast.Module) -> frozenset[str]:
    """The platform's ``RETIRED_INERT_SCOPES`` — taxonomy entries that grant nothing.

    A retired scope stays in the platform taxonomy only so already-published
    clients that still pin it keep a valid authorization request (Cognito rejects
    the whole request over one disallowed scope). It grants nothing, so the pin
    must not carry it.

    Optional: a platform tree from before the declaration existed retires nothing.
    Declared as ``frozenset({...})`` — a call, which ``literal_eval`` refuses — so
    the single set-literal argument is unwrapped first.
    """
    try:
        node = _assigned_value(tree, "RETIRED_INERT_SCOPES")
    except ExtractionError as exc:
        if "not found" in str(exc):
            return frozenset()
        raise
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("frozenset", "set")
        and not node.keywords
    ):
        if not node.args:
            return frozenset()
        if len(node.args) == 1:
            node = node.args[0]
    try:
        return frozenset(ast.literal_eval(node))
    except ValueError as exc:
        raise ExtractionError(
            f"RETIRED_INERT_SCOPES is not a literal set of scope names: {exc}"
        ) from exc


def expected_pin(source_text: str) -> str:
    """Derive the space-separated, fully-qualified pin from platform source.

    Every active (non-retired) family in ``MCP_SCOPE_TAXONOMY``. Pure — takes the
    source TEXT, returns the pin. No filesystem, no import, no network, so the
    always-run test suite can exercise it against an inline fixture.

    Scope order follows ``MCP_SCOPE_TAXONOMY``'s declaration order so a regenerated
    pin diffs cleanly instead of reshuffling.
    """
    tree = ast.parse(source_text)

    identifier = ast.literal_eval(_assigned_value(tree, "MCP_RESOURCE_SERVER_IDENTIFIER"))
    taxonomy = ast.literal_eval(_assigned_value(tree, "MCP_SCOPE_TAXONOMY"))
    retired = _retired_inert_scopes(tree)

    stray = sorted(retired - set(taxonomy))
    if stray:
        raise ExtractionError(
            f"RETIRED_INERT_SCOPES names scopes not in MCP_SCOPE_TAXONOMY: {stray}"
        )

    active = [name for name in taxonomy if name not in retired]
    stale = [name for name in active if name in NEVER_PIN]
    if stale:
        raise ExtractionError(
            f"the platform source still offers retired scope families {stale} — "
            "--repo-root points at a tree from before their retirement; regenerate "
            "from a current agent-infra dev checkout"
        )
    if not active:
        raise ExtractionError("MCP_SCOPE_TAXONOMY declares no active scope family")
    return " ".join(f"{identifier}/{name}" for name in active)


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def render_artifact(pin: str) -> dict:
    payload = {"scopes": pin}
    return {**payload, "sha256": payload_sha256(payload)}


def artifact_text(pin: str) -> str:
    return json.dumps(render_artifact(pin), indent=2, sort_keys=True) + "\n"


def rendered_mcp_json(scopes: str) -> str:
    """The plugin's `.mcp.json`, with only `oauth.scopes` varying.

    Reads the existing file so hand-maintained keys (`type`, `url`, the server key)
    survive regeneration — this script owns the pin, not the whole document.
    """
    if not MCP_JSON.exists():
        raise ExtractionError(f"{MCP_JSON} missing — create the plugin skeleton first")
    doc = json.loads(MCP_JSON.read_text())
    servers = doc.get("mcpServers") or {}
    if len(servers) != 1:
        raise ExtractionError(f"{MCP_JSON} must declare exactly one mcpServers entry")
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
        pin = expected_pin(source.read_text())
        targets: list[tuple[Path, str]] = [
            (VENDORED_ARTIFACT, artifact_text(pin)),
            (MCP_JSON, rendered_mcp_json(pin)),
        ]
    except ExtractionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    drifted = [p for p, want in targets if not p.exists() or p.read_text() != want]

    if args.check:
        if drifted:
            print("DRIFT — regenerate with `python3 harness/sync_scope_pin.py "
                  f"--repo-root {args.repo_root}`:", file=sys.stderr)
            for path in drifted:
                print(f"  {path.relative_to(BET_ROOT)}", file=sys.stderr)
            return 1
        print(f"OK — {len(targets)} file(s) match the platform taxonomy")
        return 0

    for path, want in targets:
        path.write_text(want)
    print(f"soleon: {len(pin.split())} scopes")
    print(f"wrote {len(targets)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
