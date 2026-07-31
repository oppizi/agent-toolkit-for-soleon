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

The server URL and the OAuth **client ID** are env-coupled — each Soleon system
registers its own client — so changing one without the other is a guaranteed
sign-in failure. The plugin ships dev's client ID as a literal, and a plugin
setting cannot override it (Claude Code substitutes `${user_config.…}` in the
server URL but *not* inside the OAuth block). To work against another environment,
add the server directly and pass both:

```bash
ENV=staging   # the environment you want

claude mcp add-json soleon-observer-$ENV "$(cat <<JSON
{
  "type": "http",
  "url": "$(aws ssm get-parameter --name /agent-infra/mcp/$ENV/oauth-base-url \
             --query Parameter.Value --output text)/mcp",
  "oauth": {
    "clientId": "$(aws ssm get-parameter --name /agent-infra/mcp/$ENV/cli-client-id \
                     --query Parameter.Value --output text)",
    "scopes": "soleon-mcp/agent.read soleon-mcp/kb.read soleon-mcp/observability.read soleon-mcp/eval.read soleon-mcp/usage.read soleon-mcp/governance.read soleon-mcp/business.read soleon-mcp/wiki.read"
  }
}
JSON
)"

claude mcp login soleon-observer-$ENV
```

**Use `add-json`, not `claude mcp add`.** `claude mcp add` cannot set OAuth
scopes — its `--scope` flag is the *config* scope (local/user/project), something
else entirely — so a server added that way requests whatever the server advertises
as its default. That default is currently this same read-only set, so `add` happens
to work for **this** bundle today — but nothing pins it, and it would drift silently
if the server's default ever changed. `add-json` states the pin explicitly.

This registers a **second, separate** MCP server alongside the plugin's own, which
stays pointed at the default system. Disable the plugin (or just use the new
server) so you are not signed in to two systems at once.

Both `aws ssm` lookups need access to the AWS account running that Soleon system —
ask whoever operates it if you don't have it. They return the stage-less
custom-domain URL on purpose: a URL carrying an API-Gateway stage path (the
`…execute-api.us-east-1.amazonaws.com/prod/mcp` shape earlier versions documented)
breaks OAuth discovery and the sign-in fails on the first request.
### Access

This connects to an Oppizi-operated Soleon system and requires an account there.
There is no public sign-up — accounts are created by an administrator, so if you
have not been given one, the sign-in page cannot be completed.

### Troubleshooting sign-in

- **"Incompatible auth server: does not support dynamic client registration"** —
  you are on plugin version **0.3.0 or earlier**, which shipped without an OAuth
  client ID. Update to **0.3.1 or later**. No server-side change can fix it: a
  plugin with no client ID never reaches the authorization server at all.
- **"Unrecognised MCP client" / `unauthorized_client`** — the client ID being sent
  is not registered in the environment you are pointing at. The error page names
  that environment's current client ID; re-add the server with `add-json` as
  shown above, using that ID.

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
