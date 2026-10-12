"""Compose each plugin bundle from the shared capability catalogue.

Harness-only — this file never ships inside a plugin.

A Claude Code plugin manifest **cannot reference anything outside its own root**:
the `skills`/`agents`/`hooks` path fields are schema-constrained to `./…`, the
runtime rejects escapes ("Skill path … escapes plugin directory"), and marketplace
install copies only the plugin's own `source` subdirectory into the cache. So a
capability shared by two bundles has to reach each one as *content*.

Symlinks are the documented mechanism and they are a trap here. Cross-directory
links are dereferenced only on a marketplace install that git-clones the repo; per
the reference docs, "for plugins installed with `--plugin-dir` or from a local
path, only symlinks that resolve within the plugin's own directory are preserved.
All others are skipped." That silently breaks the local-clone install our README
documents, plus Windows checkouts and ZIP downloads — and this repo has no CI to
notice.

So bundles carry **real copies**, and this script keeps them honest. The convention
is adapted from `aws/agent-toolkit-for-aws`'s `tools/sync-plugin-skills.py` (whose
packaging pattern our NOTICE already credits):

    the bundle directory declares WHICH capabilities it subscribes to;
    the catalogue owns WHAT IS IN THEM.

An entry under `plugins/<bundle>/<kind>/` whose name also exists in
`plugins/<kind>/` is a subscription, and the catalogue copy is the source of truth.
An entry with no catalogue match is left completely alone, so bundle-private
capabilities need no special casing. There is no manifest of subscriptions to keep
in sync, and drift is still testable because the catalogue is the source.

Three kinds, three different runtime mechanics — this is the whole reason the
script is more than a copy loop:

* **skills** — `<name>/SKILL.md`. Auto-scanned from `<pluginRoot>/skills/`, and the
  manifest key is *additive*, so nothing needs generating.
* **agents** — `<name>.md`. `agents/**/*.md` is auto-scanned recursively. We keep
  catalogue agents as flat files so the id stays `soleon-<bundle>:<name>`, and we
  never set the `agents` manifest key — unlike `skills`, it *replaces* the default
  scan, so setting it would hide every unlisted agent.
* **hooks** — `<name>/hooks.json`. Only the single path `hooks/hooks.json` is
  auto-discovered; there is no `hooks/*.json` scan. Every subscription must
  therefore be listed in that bundle's `plugin.json` `hooks` array (which *is*
  additive and merged), so this script generates that array. A bundle may still
  keep its own private `hooks/hooks.json` — it is auto-loaded alongside.

Usage::

    python3 harness/sync_bundles.py                              # sync every bundle
    python3 harness/sync_bundles.py --check                      # exit 1 on drift
    python3 harness/sync_bundles.py --bundle admin               # one bundle
    python3 harness/sync_bundles.py --add admin skills write-evals
    python3 harness/sync_bundles.py --remove admin skills write-evals
"""

from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import subprocess
import sys
from pathlib import Path

BET_ROOT = Path(__file__).resolve().parents[1]
PLUGINS = BET_ROOT / "plugins"
BUMP_HOOK = BET_ROOT / ".claude" / "hooks" / "bump-plugin-version.sh"

# Build artefacts that must never travel into a bundle, and must never make an
# otherwise-identical pair look like drift. One definition, used by BOTH the
# comparison and the copy — if these diverged, `--check` would report drift that
# a sync could not fix.
IGNORE_NAMES = frozenset({"__pycache__", ".DS_Store"})
IGNORE_SUFFIXES = frozenset({".pyc", ".pyo"})
COPY_IGNORE = shutil.ignore_patterns(*IGNORE_NAMES, "*.pyc", "*.pyo")


class Kind:
    """One catalogue kind: how its entries are shaped and where they land."""

    def __init__(self, name: str, *, is_dir: bool, marker: str = "", suffix: str = ""):
        self.name = name          # also the directory name, in catalogue and bundle
        self.is_dir = is_dir      # entry is a directory (skills, hooks) or a file (agents)
        self.marker = marker      # required file inside a directory entry
        self.suffix = suffix      # required suffix for a file entry

    def entry_name(self, path: Path) -> str:
        return path.name if self.is_dir else path.name[: -len(self.suffix)]

    def entry_path(self, root: Path, name: str) -> Path:
        return root / self.name / (name if self.is_dir else name + self.suffix)


KINDS = {
    k.name: k
    for k in (
        Kind("skills", is_dir=True, marker="SKILL.md"),
        Kind("agents", is_dir=False, suffix=".md"),
        Kind("hooks", is_dir=True, marker="hooks.json"),
    )
}

HOOK_ENTRY = "./hooks/{name}/hooks.json"


class SyncError(RuntimeError):
    """An operator-facing problem — bad bundle/kind/name, or a malformed manifest."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def bundles() -> list[Path]:
    """The shipped role bundles.

    Identified by MANIFEST PRESENCE, never by `is_dir()`: `plugins/` also holds the
    catalogue directories and `scope_bundles.json`, and a directory-based check
    would enumerate those as bundles.
    """
    return sorted(p for p in PLUGINS.iterdir() if (p / ".claude-plugin/plugin.json").is_file())


def catalogue_index(kind: Kind) -> dict[str, Path]:
    """``{entry name: catalogue path}`` for one kind."""
    root = PLUGINS / kind.name
    if not root.is_dir():
        return {}
    index: dict[str, Path] = {}
    for path in sorted(root.iterdir()):
        if kind.is_dir:
            if path.is_dir() and (path / kind.marker).is_file():
                index[path.name] = path
        elif path.is_file() and path.name.endswith(kind.suffix):
            index[kind.entry_name(path)] = path
    return index


def subscriptions(bundle: Path, kind: Kind, index: dict[str, Path]) -> list[str]:
    """Entry names this bundle subscribes to — those matching a catalogue entry.

    Anything else under the bundle's component directory is bundle-private and is
    left untouched, exactly as upstream does.
    """
    root = bundle / kind.name
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.iterdir()):
        if kind.is_dir:
            if not path.is_dir():
                continue
        elif not (path.is_file() and path.name.endswith(kind.suffix)):
            continue
        name = kind.entry_name(path)
        if name in index:
            found.append(name)
    return found


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _ignored(rel: Path) -> bool:
    return bool(IGNORE_NAMES.intersection(rel.parts)) or rel.suffix in IGNORE_SUFFIXES


def _tree_files(root: Path) -> set[Path]:
    return {
        rel
        for p in root.rglob("*")
        if p.is_file() and not _ignored(rel := p.relative_to(root))
    }


def entries_match(src: Path, dst: Path) -> bool:
    """True when a bundle copy is byte-for-byte the catalogue entry."""
    if not dst.exists():
        return False
    if src.is_file():
        return dst.is_file() and filecmp.cmp(src, dst, shallow=False)
    if not dst.is_dir():
        return False
    files = _tree_files(src)
    # Compare the path SETS first: an extra or missing file is drift even when
    # every shared file is identical.
    if files != _tree_files(dst):
        return False
    return all(filecmp.cmp(src / f, dst / f, shallow=False) for f in files)


# ---------------------------------------------------------------------------
# The manifest `hooks` array (the one generated thing)
# ---------------------------------------------------------------------------


def manifest_path(bundle: Path) -> Path:
    return bundle / ".claude-plugin" / "plugin.json"


def desired_hooks(bundle: Path, index: dict[str, Path], existing: object) -> list[str]:
    """The `hooks` array this bundle should declare.

    We own only the entries that point at a catalogue subscription. Anything else
    a maintainer put there by hand — a bundle-private extra hook file — is
    preserved in its original position, so this stays a read-modify-write of the
    part we generate rather than a takeover of the key.
    """
    ours = {HOOK_ENTRY.format(name=n) for n in index}
    keep = [e for e in existing if e not in ours] if isinstance(existing, list) else []
    subscribed = sorted(
        HOOK_ENTRY.format(name=n)
        for n in subscriptions(bundle, KINDS["hooks"], index)
    )
    return keep + subscribed


def reconcile_hooks(bundle: Path, index: dict[str, Path]) -> "str | None":
    """The bundle's plugin.json with the `hooks` array reconciled, or None if it
    already agrees.

    Compares the PARSED `hooks` value, never the serialized document. Re-rendering
    and diffing text would report drift for pure formatting — `json.dumps` escapes
    the em dashes in these descriptions — and would rewrite manifests that are
    perfectly correct.

    Reads the existing document so every hand-maintained key survives, the same
    contract `sync_scope_bundles.rendered_mcp_json` keeps for `.mcp.json`.
    """
    path = manifest_path(bundle)
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise SyncError(f"{path} is not a JSON object")

    current = doc.get("hooks")
    hooks = desired_hooks(bundle, index, current)

    if not hooks and not isinstance(current, list):
        # Nothing subscribed, and any existing `hooks` is the inline-object form a
        # maintainer wrote by hand. Not ours to normalize.
        return None
    if hooks == (current if isinstance(current, list) else None):
        return None

    if hooks:
        doc["hooks"] = hooks
    else:
        # Drop the key rather than leaving `"hooks": []` — an empty array is a
        # declaration that this bundle has hooks, and it does not.
        doc.pop("hooks", None)
    # ensure_ascii=False: these descriptions contain em dashes, and escaping them
    # to — would be gratuitous churn in a file humans read.
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync_bundle(bundle: Path, *, check_only: bool) -> tuple[list[str], bool]:
    """Reconcile one bundle. Returns (drift messages, changed)."""
    drift: list[str] = []
    changed = False

    for kind in KINDS.values():
        index = catalogue_index(kind)
        for name in subscriptions(bundle, kind, index):
            src = index[name]
            dst = kind.entry_path(bundle, name)
            if entries_match(src, dst):
                continue
            rel_src = src.relative_to(BET_ROOT)
            if check_only:
                drift.append(
                    f"{bundle.name}: {kind.name}/{name} differs from {rel_src} "
                    f"— edit {rel_src}, then re-run without --check"
                )
                continue
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            elif dst.exists() or dst.is_symlink():
                dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, ignore=COPY_IGNORE)
            else:
                shutil.copy2(src, dst)
            print(f"  {bundle.name}: synced {kind.name}/{name} <- {rel_src}")
            changed = True

    # The manifest `hooks` array, last: it is derived from the hooks subscriptions
    # this pass may just have created.
    want = reconcile_hooks(bundle, catalogue_index(KINDS["hooks"]))
    if want is not None:
        path = manifest_path(bundle)
        rel = path.relative_to(BET_ROOT)
        if check_only:
            drift.append(
                f"{bundle.name}: {rel} `hooks` array does not match its hooks "
                "subscriptions — re-run without --check"
            )
        else:
            path.write_text(want, encoding="utf-8")
            print(f"  {bundle.name}: regenerated `hooks` in {rel}")
            changed = True

    return drift, changed


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe
# ---------------------------------------------------------------------------


def resolve(bundle_name: str, kind_name: str, entry: str) -> tuple[Path, Kind, Path]:
    kind = KINDS.get(kind_name)
    if kind is None:
        raise SyncError(f"unknown kind {kind_name!r} — expected one of {', '.join(KINDS)}")
    bundle = PLUGINS / bundle_name
    if not manifest_path(bundle).is_file():
        known = ", ".join(b.name for b in bundles())
        raise SyncError(f"{bundle_name!r} is not a bundle — expected one of {known}")
    index = catalogue_index(kind)
    if entry not in index:
        known = ", ".join(index) or "(catalogue is empty)"
        raise SyncError(
            f"no {kind_name} entry {entry!r} in plugins/{kind_name}/ — available: {known}"
        )
    return bundle, kind, index[entry]


def add(bundle_name: str, kind_name: str, entry: str) -> bool:
    """Subscribe a bundle to a catalogue entry.

    This is the only supported way to subscribe: git cannot track an empty
    directory, so "just create the folder and let sync fill it" is not a workflow
    that survives a commit.
    """
    bundle, kind, src = resolve(bundle_name, kind_name, entry)
    dst = kind.entry_path(bundle, entry)
    if dst.exists():
        print(f"{bundle.name} already subscribes to {kind.name}/{entry}")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, ignore=COPY_IGNORE)
    else:
        shutil.copy2(src, dst)
    print(f"{bundle.name}: added {kind.name}/{entry} <- {src.relative_to(BET_ROOT)}")
    return True


def remove(bundle_name: str, kind_name: str, entry: str) -> bool:
    bundle, kind, _ = resolve(bundle_name, kind_name, entry)
    dst = kind.entry_path(bundle, entry)
    if not dst.exists():
        print(f"{bundle.name} does not subscribe to {kind.name}/{entry}")
        return False
    if dst.is_dir():
        shutil.rmtree(dst)
    else:
        dst.unlink()
    # Leave the component directory behind only if something else lives in it.
    parent = dst.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    print(f"{bundle.name}: removed {kind.name}/{entry}")
    return True


# ---------------------------------------------------------------------------
# Version bump
# ---------------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=BET_ROOT, capture_output=True, text=True, check=False
    )


def bump_versions() -> None:
    """Let the existing PostToolUse hook bump every bundle this run changed.

    Two reasons this is not just "run the hook":

    1. The hook fires on Edit/Write/MultiEdit, and this script runs under Bash —
       so nothing would bump on a sync, and the hook's own invariant ("a version
       always MOVES when its plugin's content changes") would break in silence.
    2. The hook decides via `git diff HEAD`, which ignores UNTRACKED files. A
       newly added subscription is entirely new paths, so it would be invisible.
       `git add -N` records intent-to-add, which makes those paths show up in
       `git diff` without staging their content.

    Calling the hook rather than reimplementing it keeps ONE bump implementation.
    """
    if _git("rev-parse", "--git-dir").returncode != 0:
        return  # not a checkout — nothing to diff against, same guard the hook uses
    if not BUMP_HOOK.is_file():
        # Say something rather than silently no-op, matching the hook's own
        # precedent for a layout change it cannot see.
        print(
            f"WARNING: {BUMP_HOOK.relative_to(BET_ROOT)} not found — plugin versions "
            "were NOT bumped. Bump them by hand, or fix this path.",
            file=sys.stderr,
        )
        return
    _git("add", "-N", "--", "plugins")
    proc = subprocess.run(
        ["bash", str(BUMP_HOOK)], cwd=BET_ROOT, capture_output=True, text=True, check=False
    )
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit non-zero on drift; write nothing")
    parser.add_argument("--bundle", help="limit to one bundle")
    parser.add_argument(
        "--add", nargs=3, metavar=("BUNDLE", "KIND", "NAME"),
        help="subscribe a bundle to a catalogue entry",
    )
    parser.add_argument(
        "--remove", nargs=3, metavar=("BUNDLE", "KIND", "NAME"),
        help="unsubscribe a bundle from a catalogue entry",
    )
    args = parser.parse_args(argv)

    if args.add and args.remove:
        print("ERROR: pass --add or --remove, not both", file=sys.stderr)
        return 2
    if args.check and (args.add or args.remove):
        print("ERROR: --check writes nothing, so it cannot combine with --add/--remove",
              file=sys.stderr)
        return 2

    changed = False
    try:
        if args.add:
            changed = add(*args.add)
        elif args.remove:
            changed = remove(*args.remove)
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    targets = bundles()
    if args.bundle:
        targets = [b for b in targets if b.name == args.bundle]
        if not targets:
            known = ", ".join(b.name for b in bundles())
            print(f"ERROR: {args.bundle!r} is not a bundle — expected one of {known}",
                  file=sys.stderr)
            return 2

    all_drift: list[str] = []
    for bundle in targets:
        drift, bundle_changed = sync_bundle(bundle, check_only=args.check)
        all_drift.extend(drift)
        changed = changed or bundle_changed

    if all_drift:
        print(f"\nDRIFT — {len(all_drift)} problem(s):", file=sys.stderr)
        for message in all_drift:
            print(f"  {message}", file=sys.stderr)
        print("\nThe catalogue under plugins/{skills,agents,hooks}/ is the source of "
              "truth. Fix it there, then run `python3 harness/sync_bundles.py`.",
              file=sys.stderr)
        return 1

    if args.check:
        print(f"OK — {len(targets)} bundle(s) match the catalogue")
        return 0

    if changed:
        bump_versions()
    else:
        print("All bundles already match the catalogue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
