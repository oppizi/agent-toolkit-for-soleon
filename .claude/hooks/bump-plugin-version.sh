#!/bin/bash
# Auto-bump the soleon-deploy-agent plugin PATCH version when the plugin
# changes during a Claude Code session.
#
# Wired as a PostToolUse(Edit|Write|MultiEdit) hook (see ../settings.json).
# Behavior is idempotent per commit-round so it fires ONCE per change-set,
# not once per edit:
#   - bumps only when plugin/ (excluding the version file itself) differs
#     from git HEAD — so unrelated edits, and pure reverts, do nothing;
#   - skips if the version was already bumped since the last commit (current
#     version != HEAD's), so repeated edits in the same round don't re-bump.
# It writes plugin.json directly (not via the Edit tool), so it never
# re-triggers PostToolUse, and the diff scope excludes plugin.json, so editing
# the version alone never triggers a bump.
#
# Humans still decide minor/major bumps by hand; this only guarantees the
# version always MOVES when the plugin's content changes, so no two distinct
# plugin states ever share a version.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"   # .claude/hooks -> .claude -> repo root
PJSON="$REPO_ROOT/plugin/.claude-plugin/plugin.json"
VERSION_FILE_REL="plugin/.claude-plugin/plugin.json"

command -v jq >/dev/null 2>&1 || exit 0          # no jq → no-op (don't break the session)
[ -f "$PJSON" ] || exit 0
cd "$REPO_ROOT" || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

# 1. Did plugin/ content (excluding the version file) actually change vs HEAD?
if git diff --quiet HEAD -- plugin ":(exclude)$VERSION_FILE_REL" 2>/dev/null; then
  exit 0
fi

# 2. Already bumped this round? (working-tree version differs from HEAD's)
CUR="$(jq -r '.version' "$PJSON" 2>/dev/null || echo "")"
[ -n "$CUR" ] || exit 0
HEAD_VER="$(git show "HEAD:$VERSION_FILE_REL" 2>/dev/null | jq -r '.version' 2>/dev/null || echo "")"
if [ -n "$HEAD_VER" ] && [ "$HEAD_VER" != "$CUR" ]; then
  exit 0
fi

# 3. Bump the patch component (semver-ish: a.b.c -> a.b.(c+1); fallback: append .1).
NEW="$(printf '%s' "$CUR" | awk -F. 'NF==3 && $3 ~ /^[0-9]+$/ {printf "%s.%s.%d",$1,$2,$3+1; next} {printf "%s.1",$0}')"
[ -n "$NEW" ] && [ "$NEW" != "$CUR" ] || exit 0

tmp="$(mktemp)"
jq --arg v "$NEW" '.version=$v' "$PJSON" > "$tmp" && mv "$tmp" "$PJSON"
echo "soleon-deploy-agent: plugin/ changed — bumped version $CUR -> $NEW" >&2
exit 0
