# soleon-builder

The Soleon agent build loop: everything `soleon-observer` can read, plus the
scopes to create, update, deploy, promote and delete agents, bind channels, and
author custom MCP servers and knowledge bases. It carries the `deploy-agent`
skill.

> **Renamed from `soleon-deploy-agent`.** See *Upgrading* below — there is no
> alias, and your old access-token setting is obsolete.

## Scopes this bundle requests

Sixteen — the eight `soleon-observer` reads, plus:

```
soleon-mcp/agent.write   soleon-mcp/agent.deploy  soleon-mcp/agent.delete
soleon-mcp/channel.write soleon-mcp/mcp.write     soleon-mcp/kb.write
soleon-mcp/business.write soleon-mcp/wiki.write
```

Installing a broader bundle grants **no** additional access. Scope is a ceiling on
what the token may consent to, never a role — Soleon authorizes every request
against your real permissions, so you see and reach only the tools you are already
entitled to.

The admin-only families (`channel.read`, `mcp.read`, `eval.run`, `discovery.*`) are
deliberately absent; they live in `soleon-admin`.

## What the deploy-agent skill does

Turn a local Claude Code agent definition (`.claude/agents/<name>.md`) into a
**validated, deploy-ready agent-infra record** — without re-describing your
agent in a web form. The skill distills your agent's identity (soul + a
proposed `config`) into an Allium spec, asks only about genuine gaps the file
can't answer (and confirms the proposed config in plain language), and emits
the exact `POST /agents` request body plus the predicted DynamoDB CONFIG row.

**v0.2 — what's now covered:** the request body carries the full `configFields`
the No-channel create path accepts — `soul`, a distilled-then-confirmed `config`
(model mapped from your alias, `evals` derived from your hard constraints,
`guardrails` inferred from your safety language, prompt caching), and bundled
`skills`. Every emitted field is validated offline against the live server
rules (offline-green ⇒ live-200).

**Deploy is the final, confirmed step.** Distillation, elicitation, and
validation all run **offline** — the skill builds and validates the JSON on
your machine without touching the network, so the prototype never pays the
token cost of loading a complex remote server's full tool catalog just to
interview you. Only after you review the assets and explicitly confirm does the
skill deploy them, by calling the `private_deploy_agent` tool on the bundled
`soleon-agent-toolkit` MCP server (a stateless HTTP server you sign in to over
OAuth — see *Authentication* below). Nothing is sent until you say so.

**What this plugin does NOT do:** it does not deploy anything *without your
explicit confirmation*, and it does not create agents during distillation or
elicitation — those phases are offline and produce only local JSON.
`config.schedules` and `config.tools` are deferred (no honest identity signal /
a dependency the plugin avoids); visibility is `private` only in this slice.

## Authentication

Sign-in is **OAuth 2.1** — there is no token to paste and nothing to rotate. The
first request to the server opens the flow in your browser (or run
`claude mcp login` explicitly); Claude Code then holds a short-lived access token
and refreshes it for you.

The plugin pins the sixteen scopes listed above, so the token it obtains is capped
at the builder surface no matter what the server would otherwise offer.

The install prompt offers a **Soleon MCP server URL**, defaulting to the dev system
(`https://mcp-dev.oppizi.com/mcp`) — which the plugin is configured for out of the
box, so nothing else needs setting up to use it.

This connects to an Oppizi-operated Soleon system and requires an account there.
There is no public sign-up — accounts are created by an administrator, so if you
have not been given one, the sign-in page cannot be completed.

### Connecting to a different Soleon system

The server URL and the OAuth **client ID** are env-coupled — each Soleon system
registers its own client — so changing one without the other is a guaranteed
sign-in failure. The plugin ships dev's client ID as a literal, and the `/plugin`
reconfigure path cannot override it (Claude Code substitutes `${user_config.…}` in
the server URL but *not* inside the OAuth block). To work against another
environment, add the server directly and pass both:

```bash
ENV=staging   # the environment you want

claude mcp add-json soleon-builder-$ENV "$(cat <<JSON
{
  "type": "http",
  "url": "$(aws ssm get-parameter --name /agent-infra/mcp/$ENV/oauth-base-url \
             --query Parameter.Value --output text)/mcp",
  "oauth": {
    "clientId": "$(aws ssm get-parameter --name /agent-infra/mcp/$ENV/cli-client-id \
                     --query Parameter.Value --output text)",
    "scopes": "soleon-mcp/agent.read soleon-mcp/agent.write soleon-mcp/agent.deploy soleon-mcp/agent.delete soleon-mcp/channel.write soleon-mcp/mcp.write soleon-mcp/kb.read soleon-mcp/kb.write soleon-mcp/observability.read soleon-mcp/eval.read soleon-mcp/usage.read soleon-mcp/governance.read soleon-mcp/business.read soleon-mcp/business.write soleon-mcp/wiki.read soleon-mcp/wiki.write"
  }
}
JSON
)"

claude mcp login soleon-builder-$ENV
```

**Use `add-json`, not `claude mcp add`.** `claude mcp add` cannot set OAuth
scopes — its `--scope` flag is the *config* scope (local/user/project), something
else entirely — so a server added that way requests whatever the server advertises
as its default: the **read-only observer set**. You would get 8 scopes instead of
this bundle's 16, silently losing every write and deploy capability this plugin
exists for, while everything still looked correctly installed.

This registers a **second, separate** MCP server alongside the plugin's own, which
stays pointed at the default system. Disable the plugin (or just use the new
server) so you are not signed in to two systems at once.

Both `aws ssm` lookups need access to the AWS account running that Soleon system —
ask whoever operates it if you don't have it. They return the stage-less
custom-domain URL on purpose: a URL carrying an API-Gateway stage path (the
`…execute-api.us-east-1.amazonaws.com/prod/mcp` shape earlier versions documented)
breaks OAuth discovery and the sign-in fails on the first request.
## Upgrading from soleon-deploy-agent

This plugin **was** `soleon-deploy-agent`. The rename is hard: there is no alias, so
an existing install will not update itself. Uninstall the old plugin and install
this one:

```
/plugin uninstall soleon-deploy-agent@agent-toolkit-for-soleon
/plugin install soleon-builder@agent-toolkit-for-soleon
```

The `deploy-agent` skill, the contract, and the bundled engine are unchanged. Your
old **Soleon access token** setting is obsolete — authentication is now the OAuth
flow above, so that long-lived JWT is no longer read from your keychain and can be
deleted.

If you only ever *read* from Soleon, consider `soleon-observer` instead — it
consents to no writes at all.

## Install

From the GitHub marketplace:

```
/plugin marketplace add oppizi/agent-toolkit-for-soleon
/plugin install soleon-builder@agent-toolkit-for-soleon
```

Or from a local checkout of [oppizi/agent-toolkit-for-soleon](https://github.com/oppizi/agent-toolkit-for-soleon):

```
/plugin marketplace add ./agent-toolkit-for-soleon
/plugin install soleon-builder@agent-toolkit-for-soleon
```

(The only contents that matter at runtime are this directory's
`contract.json`, `bin/`, and `skills/`.)

## Use

```
/deploy-agent .claude/agents/my-agent.md
```

To also bundle local skills, name their directories:

```
/deploy-agent .claude/agents/my-agent.md --skill .claude/skills/cite-sources
```

The skill will: run a 2-second selfcheck → distill your identity file (soul +
a proposed config) into an Allium spec → ask you only what the file cannot
answer (the framework) and confirm the proposed config in plain language (the
model mapping, guardrails, evals, prompt caching) plus a display name for each
bundled skill → validate → write three files beside the spec:

- `<slug>.request_body.json` — the `POST /agents` body (top-level
  `slug/displayName/framework/appEnv` + `dynamoFields` + `configFields`)
- `<slug>.ddb_projection.json` — the predicted create-time CONFIG row
- `<slug>.report.json` — audit trail: dropped model aliases, extracted
  constraints, decode decisions, resolved engine version

Then it shows you exactly what will be deployed and, **only on your explicit
confirmation**, deploys the validated `request_body.json` by calling
`private_deploy_agent` on the `soleon-agent-toolkit` MCP server. Decline and the
JSON files simply stay on disk — nothing is sent.

Direct converter invocation (no LLM, spec already in hand):

```
python3 skills/deploy-agent/assets/allium_to_json.py spec.allium --app-env dev --out-dir out/ \
  --skill .claude/skills/cite-sources
```

## Supported platforms (bundled engine)

| Platform | Bundled binary | Status |
|---|---|---|
| darwin-arm64 | `bin/allium-darwin-arm64` | ✅ shipped (built from juxt/allium-tools v3.2.4, provenance in `LICENSES/`) |
| darwin-x86_64 / linux-arm64 / linux-x86_64 | — | ❌ CI cross-build gap — PATH fallback applies |

No bundled binary for your platform? Install the pinned engine version:

```
cargo install --git https://github.com/juxt/allium-tools --tag v3.2.4 allium-cli
```

A different `allium` version on PATH is **refused by default** (this plugin's
output contract is verified against 3.2.4 exactly). Set
`ALLIUM_ENGINE_UNPINNED=1` to accept the mismatch at your own risk.

## Escape hatches

| Override | How | Default |
|---|---|---|
| Deploy target app env | skill argument / `--app-env` converter flag | `dev` |
| Bedrock model id | your frontmatter `model:` alias is mapped to a catalog id and **confirmed** at the elicit; aliases are never emitted raw | mapped from alias, or omitted (platform default) |
| Config (model/evals/guardrails/prompt caching) | distilled from the identity, confirmed in plain language; nothing you must learn the schema for | proposed conservatively from honest signals only |
| Skills | name local skill dirs with `--skill <path>` (repeatable); display name confirmed per skill | none bundled |
| Framework | stated in the file or invocation → no question asked; otherwise one elicit turn | always asked (never silently defaulted) |
| Engine version pin | `ALLIUM_ENGINE_UNPINNED=1` | pinned to `contract.json.engine_version` |
| Visibility | not overridable in this slice — `private` only | `private` |

> **Upgrading from v0.1:** the bundled `contract.json` is now version 2. A
> stale v1 contract fails the selfcheck loudly (by design) — reinstall the
> plugin so the converter and contract ship from the same bundle.

## Health check

```
python3 skills/deploy-agent/assets/engine.py --selfcheck
```

Verifies the contract loads, the engine binary resolves, and versions match —
in ~2 seconds, before any LLM work is spent.

## Troubleshooting

Every error message carries the problem, the cause, and the fix. The two most
common:

- **EngineNotFound** — no bundled binary for your platform and no `allium` on
  PATH → run the `cargo install` command above.
- **ContractError** — `contract.json` missing/corrupt → reinstall the plugin
  from its source (the contract ships with the bundle; it is not user-editable).

Sign-in specifically:

- **"Incompatible auth server: does not support dynamic client registration"** —
  you are on plugin version **0.3.0 or earlier**, which shipped without an OAuth
  client ID. Update to **0.3.1 or later**. No server-side change can fix it: a
  plugin with no client ID never reaches the authorization server at all.
- **"Unrecognised MCP client" / `unauthorized_client`** — the client ID being sent
  is not registered in the environment you are pointing at. The error page names
  that environment's current client ID; re-add the server with `add-json` as shown
  under *Connecting to a different Soleon system*, using that ID.
