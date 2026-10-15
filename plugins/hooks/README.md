# Hooks catalogue

The source of truth for every hook any bundle ships. **Edit here, never in a
bundle** — `plugins/<bundle>/hooks/<name>/` holds a generated copy.

One entry today: `local-emulation/` — pull-agent's save, refresh, turn and
message-budget hooks, shipped by `builder` and `admin`. Its scripts live in the
`bin/` catalogue (`plugins/bin/`), not next to the hook, because the skills and
the stdio servers call the same scripts through `${CLAUDE_PLUGIN_ROOT}/bin/`.

```
plugins/hooks/<name>/hooks.json      required — the hook config
plugins/hooks/<name>/<script>        optional — anything hooks.json invokes
```

## Composing

```bash
python3 harness/sync_bundles.py --add    builder hooks my-hook
python3 harness/sync_bundles.py --remove builder hooks my-hook
```

## Runtime notes — why this kind is different

Hooks are the one component that cannot be composed by copying alone. **Only the
single path `hooks/hooks.json` is auto-discovered — there is no `hooks/*.json`
scan.** A file dropped at `hooks/<name>/hooks.json` does nothing until it is
listed in that bundle's `plugin.json`:

```json
"hooks": ["./hooks/<name>/hooks.json"]
```

The generator writes that array from each bundle's subscriptions. It owns only the
entries pointing at a catalogue entry; anything else in the array is preserved
where a maintainer put it, and a bundle-private `hooks/hooks.json` at the top level
is still auto-loaded alongside.

Reference scripts through the placeholder, which resolves in hook commands and
survives the copy because the destination path is stable:

```json
{ "type": "command", "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/<name>/check.sh\"" }
```

Note this is a *shipped plugin* hook — distinct from `.claude/hooks/`, which is
repo-development tooling for this checkout and never ships.
