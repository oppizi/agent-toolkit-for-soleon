# soleon-observer

Read-only access to a Soleon system from Claude Code. Inspect agents and their
configuration, read traces and failures, review usage and eval scores, and browse
the ideas board and agent wiki — **without consenting to a single write.**

This is the least-privilege way to connect Claude Code to Soleon. If all you do is
ask questions about what your agents are doing, this is the bundle you want.

## Scopes this bundle requests

Eight, all reads:

```
soleon-mcp/agent.read          soleon-mcp/kb.read
soleon-mcp/observability.read  soleon-mcp/eval.read
soleon-mcp/usage.read          soleon-mcp/governance.read
soleon-mcp/business.read       soleon-mcp/wiki.read
```

The plugin pins these, so the access token it obtains can never be used to write,
deploy, or delete anything — even by accident, and even if you ask it to.

## What this plugin does NOT do

It ships **no skills and no commands** — it is a connection and a scope pin. It
does NOT write, deploy, promote, or delete anything, and it cannot: no write scope
is on the token.

It also does **not** grant you access. Installing a bundle widens what you *consent*
to, never what you are *permitted* to do — Soleon authorizes every request against
your actual permissions, so you will only ever see and reach the tools you are
already entitled to. Installing `soleon-admin` would not make you an admin.

## Install

```
/plugin marketplace add oppizi/agent-toolkit-for-soleon
/plugin install soleon-observer@agent-toolkit-for-soleon
```

Then sign in — the first request to the server triggers the OAuth flow in your
browser, or run `claude mcp login` explicitly. There is no token to paste.

On install you may set a **Soleon MCP server URL**. It defaults to the dev system
(`https://mcp-dev.oppizi.com/mcp`), which the plugin is configured for out of the
box — nothing else to set up.

## Connecting to a different Soleon system

Set the **Soleon MCP server URL** to that system's `/mcp` endpoint — that is the
only value involved. The plugin carries nothing else environment-specific, so
there is no second setting to keep in step and no CLI override to paste.

Earlier versions needed one: they shipped a literal OAuth client ID that a
plugin setting could not override, so pointing elsewhere meant registering a
second server by hand. The server now accepts the client identity Claude
publishes for itself, so the plugin is environment-neutral.

### Access

This connects to an Oppizi-operated Soleon system and requires an account there.
There is no public sign-up — accounts are created by an administrator, so if you
have not been given one, the sign-in page cannot be completed.

### Troubleshooting sign-in

- **"Incompatible auth server: does not support dynamic client registration"** —
  the Soleon system you are pointing at has not been updated for the current
  sign-in flow. Check the **Soleon MCP server URL** setting, and ask whoever
  operates that system to update it.
- **"Unrecognised MCP client" / `unauthorized_client`** — the server did not
  accept the client identity presented. If you added this server by hand with an
  explicit client ID, remove it and use the plugin's own server instead.

## Upgrading from soleon-deploy-agent

`soleon-deploy-agent` has been replaced by three role bundles and there is **no
alias**. If you had it installed, uninstall it and install the bundle that matches
what you do — `soleon-observer` to read, `soleon-builder` to build agents, or
`soleon-admin` for the platform-admin surface.

Its **Soleon access token** setting is obsolete. Authentication is now the OAuth
flow, so the long-lived JWT that used to sit in your keychain is gone; you can
delete it.

## Moving between bundles

Bundles nest: observer ⊂ builder ⊂ admin. Switching means uninstalling one and
installing another, which triggers a fresh sign-in — a deliberate re-consent moment,
since you are changing what the token may do. Within a bundle nothing needs
re-authorizing: permission grants and revokes take effect on your next request.
