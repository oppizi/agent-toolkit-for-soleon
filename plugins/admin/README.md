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

## What this plugin does NOT do

It ships **no skills and no commands** — it is a connection and a scope pin. The
agent build loop lives in `soleon-builder`.

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

claude mcp add-json soleon-admin-$ENV "$(cat <<JSON
{
  "type": "http",
  "url": "$(aws ssm get-parameter --name /agent-infra/mcp/$ENV/oauth-base-url \
             --query Parameter.Value --output text)/mcp",
  "oauth": {
    "clientId": "$(aws ssm get-parameter --name /agent-infra/mcp/$ENV/cli-client-id \
                     --query Parameter.Value --output text)",
    "scopes": "soleon-mcp/agent.read soleon-mcp/agent.write soleon-mcp/agent.deploy soleon-mcp/agent.delete soleon-mcp/channel.read soleon-mcp/channel.write soleon-mcp/mcp.read soleon-mcp/mcp.write soleon-mcp/kb.read soleon-mcp/kb.write soleon-mcp/observability.read soleon-mcp/eval.read soleon-mcp/eval.run soleon-mcp/usage.read soleon-mcp/governance.read soleon-mcp/business.read soleon-mcp/business.write soleon-mcp/wiki.read soleon-mcp/wiki.write soleon-mcp/discovery.read soleon-mcp/discovery.write"
  }
}
JSON
)"

claude mcp login soleon-admin-$ENV
```

**Use `add-json`, not `claude mcp add`.** `claude mcp add` cannot set OAuth
scopes — its `--scope` flag is the *config* scope (local/user/project), something
else entirely — so a server added that way requests whatever the server advertises
as its default: the **read-only observer set**. You would get 8 scopes instead of
this bundle's 21, silently losing every write and deploy capability this plugin
exists for, while everything still looked correctly installed.

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
alias**. Uninstall it and install the bundle matching what you do. Note that the
`deploy-agent` skill now lives in **`soleon-builder`**, not here — if that skill is
what you came for, install `soleon-builder` (or both).

Its **Soleon access token** setting is obsolete. Authentication is now the OAuth
flow, so the long-lived JWT that used to sit in your keychain is gone; you can
delete it.

## Moving between bundles

Bundles nest: observer ⊂ builder ⊂ admin. Switching means uninstalling one and
installing another, which triggers a fresh sign-in — a deliberate re-consent moment,
since you are changing what the token may do. Within a bundle nothing needs
re-authorizing: permission grants and revokes take effect on your next request.
