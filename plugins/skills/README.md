# Skills catalogue

The source of truth for every skill any bundle ships. **Edit here, never in a
bundle** — `plugins/<bundle>/skills/<name>/` holds a generated copy that the next
sync overwrites.

```
plugins/skills/<name>/SKILL.md        (+ assets/, references/, …)
```

An entry needs a `SKILL.md` with frontmatter whose `name` matches its directory
name — Claude Code resolves a skill by its directory, so a mismatch installs
cleanly and then never triggers.

## Composing

A bundle subscribes by having a directory of the same name. Add and remove
subscriptions with the generator, never by hand:

```bash
python3 harness/sync_bundles.py --add    builder skills my-skill
python3 harness/sync_bundles.py --remove builder skills my-skill
python3 harness/sync_bundles.py --check          # drift guard, run before committing
```

## Runtime notes

Skills are auto-discovered from `<pluginRoot>/skills/`, and the `skills` manifest
key is *additive*, so nothing needs declaring in `plugin.json`.

A skill's assets travel with it. Anything a skill reads that is **not** skill
content — `contract.json`, `bin/` — stays bundle-owned and is resolved at runtime
through `${CLAUDE_PLUGIN_ROOT}`, which points at the installed bundle root.
