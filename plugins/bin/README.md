# Bin catalogue

The source of truth for every helper script any bundle ships. **Edit here, never
in a bundle** — `plugins/<bundle>/bin/<name>.py` holds a generated copy that the
next sync overwrites.

```
plugins/bin/<name>.py        one stdlib-only, Python 3.9-compatible script
```

Today these are the local agent emulation scripts (AHP-889): the two stdio MCP
servers a pulled agent runs on, the hooks' entry points, and the modules they
share. `pull-agent`, `run-local-eval` and `hooks/local-emulation` need all of
them, so a bundle that subscribes to one subscribes to the set — a test asserts it.

## Composing

```bash
python3 harness/sync_bundles.py --add    builder bin soleon_mcp_client
python3 harness/sync_bundles.py --remove builder bin soleon_mcp_client
```

## Runtime notes

Nothing scans `bin/`. Skills and hooks call a script through
`${CLAUDE_PLUGIN_ROOT}/bin/<name>.py`, and the scripts import one another from
that same directory, so the path inside the bundle is the whole contract.

Only `.py` files are catalogue entries. Anything else in a bundle's `bin/` — the
vendored `allium-darwin-arm64` engine in `builder` — is bundle-private and the
generator leaves it alone.
