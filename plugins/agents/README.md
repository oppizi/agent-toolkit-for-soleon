# Agents catalogue

The source of truth for every subagent any bundle ships. **Edit here, never in a
bundle** — `plugins/<bundle>/agents/<name>.md` holds a generated copy.

Empty for now. The convention is fixed so the first agent does not have to
rediscover it.

```
plugins/agents/<name>.md        one flat markdown file per agent
```

## Composing

```bash
python3 harness/sync_bundles.py --add    builder agents my-agent
python3 harness/sync_bundles.py --remove builder agents my-agent
```

## Runtime notes — two traps

**Keep entries flat.** `agents/` is scanned *recursively*, and a subdirectory
becomes part of the agent's scoped id: `agents/review/security.md` in
`soleon-builder` registers as `soleon-builder:review:security`. Flat files keep
the id `soleon-<bundle>:<name>`.

**Never set the `agents` key in `plugin.json`.** Unlike `skills`, which is
additive, `agents` *replaces* the default scan — setting it silently hides every
agent not listed. The generator deliberately writes nothing to the manifest for
this kind.

Plugin agents ignore `hooks`, `mcpServers`, and `permissionMode` frontmatter; use
`.claude/agents/` if an agent needs that level of control.
