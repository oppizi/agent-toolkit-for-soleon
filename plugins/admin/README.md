# soleon-admin

The full Soleon surface for platform administrators. Everything `soleon-builder`
covers, plus the five families whose tools are platform-admin-only: channel instance
reads, custom-MCP instance reads, on-demand eval runs, and the discovery workspace.

Install this if you are a Soleon platform admin. If you build agents but do not
administer the platform, `soleon-builder` is the better fit — it consents to less.

## Scopes this bundle requests

All 21 — the complete taxonomy:

```
soleon-mcp/agent.read      soleon-mcp/agent.write     soleon-mcp/agent.deploy
soleon-mcp/agent.delete    soleon-mcp/channel.read    soleon-mcp/channel.write
soleon-mcp/mcp.read        soleon-mcp/mcp.write       soleon-mcp/kb.read
soleon-mcp/kb.write        soleon-mcp/observability.read
soleon-mcp/eval.read       soleon-mcp/eval.run        soleon-mcp/usage.read
soleon-mcp/governance.read soleon-mcp/business.read   soleon-mcp/business.write
soleon-mcp/wiki.read       soleon-mcp/wiki.write      soleon-mcp/discovery.read
soleon-mcp/discovery.write
```

The five beyond `soleon-builder` — `channel.read`, `mcp.read`, `eval.run`,
`discovery.read`, `discovery.write` — cover only tools Soleon already restricts to
platform admins. That is exactly why they are here and not in a lower bundle.

## Skills

One: **`/write-evals`** — designing evals whose score reflects the behaviour under
test rather than resemblance to a reference answer. Read it before writing an eval,
and whenever a suite passes but you don't trust it.

It is the same skill `soleon-builder` ships, byte for byte: both bundles are
composed from one shared source, so the two copies can never drift apart.

## What this plugin does NOT do

It ships **no commands**, and none of the agent build loop. `/deploy-agent` — local
identity file → validated `POST /agents` → deploy — lives in `soleon-builder` only.
Beyond `/write-evals`, this bundle is a connection and a scope pin.

Most importantly, it does **not** grant you admin. A bundle widens what you
*consent* to, never what you are *permitted* to do. Soleon authorizes every request
against your real permissions, so a non-admin who installs this sees exactly the
same tools they saw before — the admin-only ones stay hidden. Scope is a ceiling on
the token, not a role.

## Install

```
/plugin marketplace add oppizi/agent-toolkit-for-soleon
/plugin install soleon-admin@agent-toolkit-for-soleon
```

Then sign in — the first request to the server triggers the OAuth flow in your
browser, or run `claude mcp login` explicitly. There is no token to paste.

On install you may set a **Soleon MCP server URL**. It defaults to the dev system
(`https://mcp-dev.oppizi.com/mcp`); point it at another environment to work there.
Use the stage-less custom-domain form — a URL carrying an API-Gateway stage path
breaks OAuth discovery.

## Upgrading from soleon-deploy-agent

`soleon-deploy-agent` has been replaced by three role bundles and there is **no
alias**. Uninstall it and install the bundle matching what you do. Note that the
`deploy-agent` skill now lives in **`soleon-builder`**, not here — if that skill is
what you came for, install `soleon-builder` (or both). `/write-evals` is available
in both.

Its **Soleon access token** setting is obsolete. Authentication is now the OAuth
flow, so the long-lived JWT that used to sit in your keychain is gone; you can
delete it.

## Moving between bundles

Bundles nest: observer ⊂ builder ⊂ admin. Switching means uninstalling one and
installing another, which triggers a fresh sign-in — a deliberate re-consent moment,
since you are changing what the token may do. Within a bundle nothing needs
re-authorizing: permission grants and revokes take effect on your next request.
