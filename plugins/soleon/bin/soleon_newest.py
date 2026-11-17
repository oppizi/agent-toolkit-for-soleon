#!/usr/bin/env python3
"""Run a plugin script from the NEWEST installed version of this plugin.

    python3 soleon_newest.py <script> [args...]

Claude Code loads a plugin once, when a session starts, and keeps using that
version's folder until the session restarts: an update installs the new
version in a folder beside it, so a running session went on running the old
hooks and scripts (2026-11-17: this session still ran 1.0.13 after 1.0.18,
1.0.19 and 1.0.20 shipped). Every hook, every pulled agent's tool server and
every skill script therefore starts through this launcher, which maps <script>
(an absolute path inside any installed version, or a path relative to the
plugin root) to the same file in the newest installed version that has it, and
runs it in THIS process (stdin and stdout stay the caller's: a hook's input
and a stdio server's wire). CLAUDE_PLUGIN_ROOT is set to that version's root.

What still needs a new session: the hook list and the skills' instructions,
which Claude Code reads only at session start.
"""
from __future__ import annotations

import os
import re
import runpy
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_MANIFEST = Path(".claude-plugin") / "plugin.json"


def _version_key(name: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in name.split("."))


def version_root(path: Path) -> Optional[Path]:
    """The installed version folder (<cache>/<marketplace>/<plugin>/<version>) holding path, or None."""
    for parent in [path] + list(path.parents):
        if _VERSION_RE.match(parent.name) and (parent / _MANIFEST).is_file():
            return parent
    return None


def newest_root(root: Path, rel: Path) -> Path:
    """The highest version beside root that ships rel (root itself when none newer does)."""
    best = root
    try:
        siblings = list(root.parent.iterdir())
    except OSError:
        return root
    for cand in siblings:
        if not (_VERSION_RE.match(cand.name) and (cand / _MANIFEST).is_file() and (cand / rel).is_file()):
            continue
        if _version_key(cand.name) > _version_key(best.name):
            best = cand
    return best


def resolve(script: str, here: Path) -> Tuple[Path, Optional[Path]]:
    """(the script to run, its plugin root). A path outside any installed version (a source checkout)
    runs as given."""
    own_root = here.parent
    target = Path(script)
    if not target.is_absolute():
        target = own_root / target
    target = target.resolve()
    root = version_root(target)
    if root is None:
        return target, None
    rel = target.relative_to(root)
    newest = newest_root(root, rel)
    return newest / rel, newest


def main(argv: List[str]) -> None:
    if not argv:
        sys.stderr.write("usage: soleon_newest.py <script> [args...]\n")
        sys.exit(2)
    target, root = resolve(argv[0], Path(__file__).resolve().parent)
    if root is not None:
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(root)
    sys.argv = [str(target)] + argv[1:]
    # The script's own folder replaces this launcher's, so a newer script never imports an older sibling.
    sys.path[0] = str(target.parent)
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:])
