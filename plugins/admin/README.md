# admin

The full Soleon surface for platform administrators. Everything `builder`
covers, plus the five families whose tools are platform-admin-only: channel instance
reads, custom-MCP instance reads, on-demand eval runs, and the discovery workspace.

Install this if you are a Soleon platform admin. If you build agents but do not
administer the platform, `builder` is the better fit — it consents to less.

## Scopes this bundle requests

All 20 — every active scope family:

```
soleon-mcp/agent.read      soleon-mcp/agent.write     soleon-mcp/agent.deploy
soleon-mcp/agent.delete    soleon-mcp/agent.invoke    soleon-mcp/channel.read
soleon-mcp/channel.write
soleon-mcp/mcp.read        soleon-mcp/mcp.write       soleon-mcp/kb.read
soleon-mcp/kb.write        soleon-mcp/observability.read
soleon-mcp/eval.read       soleon-mcp/eval.run        soleon-mcp/usage.read
soleon-mcp/governance.read soleon-mcp/business.read   soleon-mcp/business.write
soleon-mcp/discovery.read  soleon-mcp/discovery.write
```

The five beyond `builder` — `channel.read`, `mcp.read`, `eval.run`,
`discovery.read`, `discovery.write` — cover only tools Soleon already restricts to
platform admins. That is exactly why they are here and not in a lower bundle.

## Skills

**`/write-evals`** — designing evals whose score reflects the behaviour under
test rather than resemblance to a reference answer. Read it before writing an eval,
and whenever a suite passes but you don't trust it.

**`/write-skill-scripts`** — writing a Python script that an agent runs from one of
its skills (`run_code_file`): check the inputs before anything is spent, keep the data
out of the agent's context, stay under the platform's call limits, give the agent one
copy-exact run form, and test it locally first (`assets/run_locally.py` runs the script
against fakes that keep the platform's rules). Also ships in `builder`.

**Local agent testing** (AHP-889) — the same pair `builder` ships:

```
/pull-agent <slug>
/run-local-eval <slug> [evalId]
```

`/pull-agent` pulls a Soleon agent into Claude Code as a local subagent that
reasons on your machine and runs every external tool on Soleon (through
`call_agent_tool`, with your credentials, the agent's tool policy and its
approval gates). Its SOUL, config, skills and evals land as files under
`.soleon/agents/<slug>/`, and every save is pushed to your Soleon draft — never
live; deploy as usual. `/run-local-eval` scores the agent's evals locally with
the platform's own judge prompt. The full behaviour — traces, the edit loop, the
refresh hook, the message budget, what is not emulated, macOS credentials — is
documented once, in the
[builder README](../builder/README.md#pull-a-soleon-agent-into-claude-code).

These are uses of the `soleon-mcp/agent.invoke` scope this bundle already
requests; Soleon also checks that you may invoke the agent.

All of it is the same content `builder` ships, byte for byte: both bundles are
composed from one shared source, so the copies can never drift apart.

**Install `admin` or `builder`, not both.** Both register the local-emulation
hooks, so with both installed every save is pushed to your draft twice, and
the duplicate push can come back as a conflict that stops the session.

## What this plugin does NOT do

`/deploy-agent` — local identity file → validated `POST /agents` → deploy —
lives in `builder` only (it needs the contract and the vendored engine).

Most importantly, it does **not** grant you admin. A bundle widens what you
*consent* to, never what you are *permitted* to do. Soleon authorizes every request
against your real permissions, so a non-admin who installs this sees exactly the
same tools they saw before — the admin-only ones stay hidden. Scope is a ceiling on
the token, not a role.

## Starter skills

27 one-job skills for everyday Soleon work: reading and changing agents, checking usage, cost and failures, and releasing safely. Every skill that changes an agent reads the agent's profile first (from `start-an-agent`) and runs its "before you start" checks; `release-check` runs before anything reaches real people.

- `start-an-agent` — Start an agent: the intake questions
- `find-an-agent` — Find an agent and see its setup
- `compare-environments` — See which version runs on dev, staging and production
- `read-identity` — Read an agent's identity
- `edit-identity` — Edit the identity, one section at a time
- `review-draft` — See what your draft would change
- `change-a-setting` — Change one draft setting
- `test-draft-in-chat` — Try your draft in the test chat
- `read-skills` — List and read an agent's skills
- `edit-skill` — Add or rewrite one of the agent's skills
- `browse-knowledge` — Find and read a knowledge base
- `see-agent-tools` — See an agent's tools and tool servers
- `attach-tool-server` — Give an agent a tool server
- `tool-switches` — Switch single tools on or off, and set approval
- `read-eval-results` — Read eval results
- `write-an-eval` — Write or remove one eval
- `run-an-eval` — Run evals
- `usage-check` — Check usage
- `cost-check` — Check cost
- `failures-check` — Check failures
- `read-a-conversation` — Find and read one conversation
- `deploy-to-dev` — Deploy your draft to dev
- `release-check` — Before it reaches real people: the release check
- `promote` — Promote to staging or production
- `connect-channel` — Connect an agent to a channel, or disconnect it
- `roll-back` — Roll back to an earlier version
- `pause-agent` — Pause or resume an agent

## Install

```
/plugin marketplace add oppizi/agent-toolkit-for-soleon
/plugin install admin@agent-toolkit-for-soleon
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
`deploy-agent` skill now lives in **`builder`**, not here — if that skill is
what you came for, install `builder` instead. `/write-evals`, `/pull-agent` and
`/run-local-eval` are available in both.

Its **Soleon access token** setting is obsolete. Authentication is now the OAuth
flow, so the long-lived JWT that used to sit in your keychain is gone; you can
delete it.

## Moving between bundles

Bundles nest: observer ⊂ builder ⊂ admin. Switching means uninstalling one and
installing another, which triggers a fresh sign-in — a deliberate re-consent moment,
since you are changing what the token may do. Within a bundle nothing needs
re-authorizing: permission grants and revokes take effect on your next request.
