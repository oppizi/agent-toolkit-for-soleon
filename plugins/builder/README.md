# soleon-builder

The Soleon agent build loop: everything `soleon-observer` can read, plus the
scopes to create, update, deploy, promote and delete agents, bind channels, and
author custom MCP servers and knowledge bases. It carries the `deploy-agent`
skill (local identity → Soleon) and, since 0.4.0, its reverse arrow: the
`pull-agent` and `run-local-eval` skills (Soleon agent → local Claude Code
subagent, edited as files whose every save lands on your draft).

> **Renamed from `soleon-deploy-agent`.** See *Upgrading* below — there is no
> alias, and your old access-token setting is obsolete.

## Scopes this bundle requests

Seventeen — the eight `soleon-observer` reads, plus:

```
soleon-mcp/agent.write   soleon-mcp/agent.deploy  soleon-mcp/agent.delete
soleon-mcp/agent.invoke  soleon-mcp/channel.write soleon-mcp/mcp.write
soleon-mcp/kb.write      soleon-mcp/business.write soleon-mcp/wiki.write
```

`agent.invoke` is the local-emulation scope: running one of an agent's tools on
the platform with YOUR credentials (`call_agent_tool`), listing them, fetching the
assembled prompt and your workspace snapshot.

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

## Pull a Soleon agent into Claude Code

```
/pull-agent <slug>
```

**What it does.** Reads your dev draft of the agent (else the deployed dev
config — what the platform's edit page opens), the tools its session actually
registers, the system prompt the platform assembles for you, and a zip of your
workspace, then asks ONE question (which Claude model to run locally — default
the closest match to the agent's Bedrock model; Kimi/Nova/GLM agents get a
warning and no default) and writes:

```
~/.claude/agents/<slug>.md               the subagent (USER scope): platform prompt + Local Tool Routing
~/.claude/agents/<slug>--<subagentId>.md one per enabled configured helper (D15)
.soleon/agents/<slug>/
  SOUL.md  config.json  skills/<id>/SKILL.md (+ skill.json, package files)
  evals/<evalId>.json  workspace/ (read-only snapshot)  workflows/<id>/SKILL.md
  tools.json  prompt.json  pull.json  .pull/ (raw responses)
```

Talk to it with the Agent tool (`subagent_type: "<slug>"`). It reasons on the
local model; **every external tool runs on Soleon** through the bundled
`bin/soleon_agent_tools_mcp.py` stdio shim (each tool under its platform name
and schema, executed via `call_agent_tool` with your credentials, the agent's
tool policy and its approval gates — the subagent asks you first, then sends
`approved: true`; the platform refuses without it). Workspace tools run locally
via `bin/soleon_workspace_mcp.py` against the snapshot. Tool calls wait for the
platform to finish — no client-side timeout, as on the platform.

**Traces.** Each run of the subagent is one turn on the platform trace
(Monitoring → Traces, Activity "Draft Agents"; the pull report links the
view): the tool server sends every platform call with the run's
`conversation` + `turn` ids, and the plugin's SubagentStart/SubagentStop
hooks (`hooks/hooks.json` → `bin/soleon_turn_hooks.py`) mint the turn id and,
when the agent finishes, record the prompt, the answer and the locally-run
tool calls from the subagent's transcript (`record_agent_turn`). A record
that cannot be sent is reported as a system message, never a blocked session.

**The edit loop.** Edit `SOUL.md`, `config.json`, `skills/**` or
`evals/*.json` and the plugin's PostToolUse hook (`hooks/hooks.json` →
`bin/soleon_draft_sync.py`) pushes the change to your Soleon draft on every
save (`patch_agent_draft`, guarded by the pulled `draftEtag`) and re-syncs the
platform's test sandbox. A concurrent edit elsewhere is a conflict: the hook
stops the session (exit 2) and asks — reload theirs (`/pull-agent <slug>`) or
overwrite with yours (`pull_agent.py adopt-etag`, then save again). Nothing
goes live; deploy with `deploy_agent_draft`. `config.json` is the nested
config.json a deploy of your draft would ship; the hook maps it back onto the
editor's flat fields with the platform's own table (`bin/soleon_agent_document.py`).

**The other direction is automatic too.** Before every prompt, the plugin's
UserPromptSubmit hook (`hooks/hooks.json` → `bin/soleon_pull_refresh.py`)
asks the platform for each pulled agent's draft version (one
`get_agent_draft` read); when it differs from what the last pull or save
recorded — an edit in the Soleon editor, a save from another session — the
local copy is re-pulled and re-materialized on the spot, so the subagent that
answers that prompt reasons with the current SOUL, config, skills, evals and
system prompt. `workspace/` is never touched (it is session data you may have
edited), a pending save conflict is never overwritten (resolve it first), and
the hook never blocks the prompt: a failed refresh is reported and the local
copy stays as it was. You are told when a refresh happened.

```
/run-local-eval <slug> [evalId]
```

runs each `evals/*.json` against the local subagent, fetches the platform's judge
prompt live (`get_eval_judge_prompt` — never vendored), runs it on the same local
model and writes `evals/results/<ts>-<evalId>.json` with `score` / `subScores` /
`reasoning`. Scores only — the platform never adjudicates pass/fail, and neither
does this.

**What is NOT emulated** (platform-only, listed read-only in `config.json` and
in the pull summary): channels, budgets, schedules, guardrails, online-eval
sampling. The workspace snapshot never pushes back. Helper subagents and
workflows follow the platform's steps (manager: assign → review → next decision
until finish, bounded by the configured rounds; peer: bounded rounds)
approximately, not identically.

**Credentials — Linux vs macOS.** The shim and the hook run outside Claude
Code's MCP connection, so they reuse the OAuth token Claude Code already holds
for `soleon-agent-toolkit`. On Linux it is in `~/.claude/.credentials.json`
(mode 0600, `mcpOAuth` map keyed by server); both read the entry whose
`serverUrl` matches the plugin's server URL and refresh it through the server's
`refresh_token` grant on a 401 (the file is rewritten with its mode kept). On
macOS Claude Code stores it in the Keychain; reading it from the hook (e.g.
`security find-generic-password`) is **untested** — until it is, export
`SOLEON_MCP_TOKEN` in the environment Claude Code runs in. Tokens are never
logged or printed.

## Authentication

Sign-in is **OAuth 2.1** — there is no token to paste and nothing to rotate. The
first request to the server opens the flow in your browser (or run
`claude mcp login` explicitly); Claude Code then holds a short-lived access token
and refreshes it for you.

The plugin pins the sixteen scopes listed above, so the token it obtains is capped
at the builder surface no matter what the server would otherwise offer.

The install prompt offers a **Soleon MCP server URL**, defaulting to the dev system
(`https://mcp-dev.oppizi.com/mcp`). Point it at another environment to work there;
switch any time via `/plugin` → reconfigure → change the URL → `/reload-plugins`,
then sign in again (sessions are per-environment).

Use the stage-less custom-domain form. A URL carrying an API-Gateway stage path —
the `…execute-api.us-east-1.amazonaws.com/prod/mcp` shape earlier versions
documented — breaks OAuth discovery and the sign-in will fail on the first request.

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
`contract.json`, `bin/`, `hooks/`, and `skills/`.)

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
