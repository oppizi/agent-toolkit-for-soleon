#!/bin/bash
# Auto-bump a plugin's PATCH version when that plugin changes during a Claude
# Code session.
#
# The repo ships one plugin per role bundle (plugins/observer, plugins/builder,
# plugins/admin), so this iterates every plugins/*/.claude-plugin/plugin.json and
# bumps ONLY the bundles whose own directory actually changed. An edit touching
# two bundles bumps both; an edit touching one bumps one.
#
# Wired as a PostToolUse(Edit|Write|MultiEdit) hook (see ../settings.json).
# Behavior is idempotent per commit-round so it fires ONCE per change-set,
# not once per edit:
#   - bumps only when the bundle's dir (excluding its own version file) differs
#     from git HEAD — so unrelated edits, and pure reverts, do nothing;
#   - skips if that bundle's version was already bumped since the last commit
#     (current version != HEAD's), so repeated edits in the same round don't
#     re-bump.
# It writes plugin.json directly (not via the Edit tool), so it never
# re-triggers PostToolUse, and each diff scope excludes that bundle's own
# plugin.json, so editing a version alone never triggers a bump.
#
# Humans still decide minor/major bumps by hand; this only guarantees a version
# always MOVES when its plugin's content changes, so no two distinct plugin
# states ever share a version.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"   # .claude/hooks -> .claude -> repo root

command -v jq >/dev/null 2>&1 || exit 0          # no jq → no-op (don't break the session)
cd "$REPO_ROOT" || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

shopt -s nullglob
MANIFESTS=("$REPO_ROOT"/plugins/*/.claude-plugin/plugin.json)
# Say something rather than silently no-op: the pre-AHP-317 version of this hook
# did `[ -f "$PJSON" ] || exit 0` against a hardcoded path, so the `plugin/` →
# `plugins/builder/` move would have stopped every bump with zero signal.
if [ ${#MANIFESTS[@]} -eq 0 ]; then
  echo "bump-plugin-version: no plugins/*/.claude-plugin/plugin.json found —" \
       "layout changed? versions will NOT be bumped." >&2
  exit 0
fi

for PJSON in "${MANIFESTS[@]}"; do
  BUNDLE_DIR_REL="plugins/$(basename "$(dirname "$(dirname "$PJSON")")")"
  VERSION_FILE_REL="$BUNDLE_DIR_REL/.claude-plugin/plugin.json"

  # 1. Did this bundle's content (excluding its version file) change vs HEAD?
  if git diff --quiet HEAD -- "$BUNDLE_DIR_REL" ":(exclude)$VERSION_FILE_REL" 2>/dev/null; then
    continue
  fi

  # 2. Already bumped this round? (working-tree version differs from HEAD's)
  CUR="$(jq -r '.version' "$PJSON" 2>/dev/null || echo "")"
  [ -n "$CUR" ] || continue
  HEAD_VER="$(git show "HEAD:$VERSION_FILE_REL" 2>/dev/null | jq -r '.version' 2>/dev/null || echo "")"
  if [ -n "$HEAD_VER" ] && [ "$HEAD_VER" != "$CUR" ]; then
    continue
  fi

  # 3. Bump the patch component (semver-ish: a.b.c -> a.b.(c+1); fallback: append .1).
  NEW="$(printf '%s' "$CUR" | awk -F. 'NF==3 && $3 ~ /^[0-9]+$/ {printf "%s.%s.%d",$1,$2,$3+1; next} {printf "%s.1",$0}')"
  [ -n "$NEW" ] && [ "$NEW" != "$CUR" ] || continue

  NAME="$(jq -r '.name // "plugin"' "$PJSON" 2>/dev/null || echo "plugin")"
  tmp="$(mktemp)"
  jq --arg v "$NEW" '.version=$v' "$PJSON" > "$tmp" && mv "$tmp" "$PJSON"
  echo "$NAME: $BUNDLE_DIR_REL changed — bumped version $CUR -> $NEW" >&2
done

exit 0
