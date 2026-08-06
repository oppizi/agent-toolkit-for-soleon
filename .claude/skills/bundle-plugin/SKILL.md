---
name: bundle-plugin
description: Compose a Soleon plugin bundle from the shared capability catalogue — show what a bundle currently subscribes to, propose skills/agents/hooks to add or remove, and apply the change through harness/sync_bundles.py. Use when adding a capability to soleon-observer/builder/admin, sharing one between bundles, or investigating why a bundle is out of sync with the catalogue.
---

# /bundle-plugin — compose a bundle from the catalogue

Repo-development skill for `agent-toolkit-for-soleon`. It does not ship.

`plugins/{skills,agents,hooks}/` is the catalogue: the single source of truth for
every capability. `plugins/{observer,builder,admin}/` are the bundles, which carry
**real copies**. `harness/sync_bundles.py` is the only thing that writes those
copies.

You are the head; the script is the core. **Never edit a file under
`plugins/<bundle>/`** — the next sync overwrites it. Every mutation goes through
the script.

## State machine

### Step 1 — Orient

Run, and read the output before saying anything:

```bash
python3 harness/sync_bundles.py --check
```

If it reports drift, **stop and surface it first**. Drift means someone edited a
bundle copy instead of the catalogue, and their change is about to be destroyed.
Show the drifted paths, name the catalogue file that is authoritative, and ask
whether to (a) discard the bundle-side edit by syncing, or (b) port it into the
catalogue first. Do not proceed to Step 2 until that is settled.

### Step 2 — Report the current composition

Build the picture from the filesystem, not from memory:

```bash
ls plugins/skills plugins/agents plugins/hooks          # the catalogue
ls plugins/*/skills plugins/*/agents plugins/*/hooks    # what each bundle has
```

Present it as a table: for each kind, which catalogue entries each bundle
subscribes to, and which entries no bundle uses. Mention any entry inside a bundle
with **no** catalogue match — that is a bundle-private capability, it is legitimate,
and the script leaves it alone.

Remember the bundles nest by intent: observer ⊂ builder ⊂ admin. A capability in
builder but not admin is usually a mistake worth flagging; the reverse usually is
not.

### Step 3 — Confirm the target and propose

Confirm which bundle the user means before proposing anything. Then propose adds
and removes as a concrete list, each with a one-line reason. Where a capability
implies write access the bundle's OAuth scopes do not carry, say so — `observer`
requests no write consent at all, so a skill that creates or deploys does not
belong there.

**Wait for explicit acceptance.** Do not apply anything in the same turn you
propose it, and do not bundle an unrequested extra into an accepted change.

If the user wants a capability that is not in the catalogue yet, that is an
authoring task, not a composition one: create `plugins/<kind>/<name>/` (see the
README in that directory for the required shape and the per-kind runtime traps),
then come back to Step 3.

### Step 4 — Apply

One command per accepted change:

```bash
python3 harness/sync_bundles.py --add    <bundle> <kind> <name>
python3 harness/sync_bundles.py --remove <bundle> <kind> <name>
```

`<kind>` is `skills`, `agents`, or `hooks`. The script copies the content, and for
hooks it also regenerates that bundle's `plugin.json` `hooks` array — only
`hooks/hooks.json` is auto-discovered at runtime, so an unlisted hook file is
inert.

It then bumps the patch version of every bundle it changed, by invoking
`.claude/hooks/bump-plugin-version.sh`. A capability *added* or *removed* is
usually a minor bump, not a patch — offer to set it by hand.

### Step 5 — Verify and report

```bash
python3 harness/sync_bundles.py --check
git status --porcelain
```

Report what changed: the copied paths, any `plugin.json` `hooks` rewrite, and the
version bumps. If a bundle gained or lost a skill, its README and the root
`README.md` bundle table now describe it wrongly — say so and offer to fix them.
`harness/tests/test_packaging.py` carries an expected-skills map per bundle; a
composition change must update it or the suite fails.

Finish with the suite:

```bash
~/.asdf/installs/python/3.14.2/bin/python3 -m pytest harness/tests -o addopts=""
```

## Failure branches

- **`--check` fails after an apply** — the script is not idempotent for that entry;
  this is a bug in `sync_bundles.py`, not something to fix by editing files. Report
  it rather than working around it.
- **`--add` says "not a bundle"** — bundles are identified by
  `.claude-plugin/plugin.json`, so the name is wrong or the manifest is missing.
- **`--add` says the entry is not in the catalogue** — it prints what is available.
  The entry may exist but lack its required marker file (`SKILL.md`, `hooks.json`).
- **The version bump warns that the hook is missing** — `.claude/hooks/` moved.
  Bump by hand and fix the path in `sync_bundles.py`; do not ignore it.
