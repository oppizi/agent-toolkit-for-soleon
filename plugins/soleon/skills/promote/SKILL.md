---
name: promote
description: "Moves what's on dev to staging, or staging to production (release, go live), tool servers first. Use when: A change is proven on the environment below and should go to staging or production."
---

# Promote to staging or production

Moves what's on dev to staging, or staging to production (release, go live), tool servers first.

**Use when:** A change is proven on the environment below and should go to staging or production.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **Channels are bound per environment, and binding is going live.** Dev, staging and production each have their own channel bindings. Promoting an agent doesn't move its channels. Binding on production is the moment real people reach it. *Check:* Which environment should people reach first, and on which channel in each?
- **A shared tool server changes every agent that uses it.** Several agents can use the same custom tool server or Tool Library integration. Editing, publishing or promoting it changes all of them. *Check:* Which other agents use this tool server, and should they change too?
- **Deploy puts it on dev; production is two more steps.** Draft → deploy to dev → promote to staging → promote to production. Each environment has its own version, channels and settings, and a dev agent can already be bound to a real channel, so a dev deploy can reach real people. *Check:* Which environment will real people use, and what must be true before it gets there?
- **Tool servers go up before the agent.** An agent promoted to production that uses a tool server not yet on production calls a server that isn't there. *Check:* Promote every tool server the agent uses first, then check it exists there.
- **Each production move is its own yes.** An OK for one production release doesn't cover the next, and a test "on production" can quietly run elsewhere. *Check:* Ask before every production move, then check it on production itself.
- **A tool test "on production" isn't proof it's there.** Testing a custom tool with the production setting runs even when the tool server isn't on production yet. Only reading the server on production proves it. *Check:* Prove a release by reading the tool server on production, not by testing it there.

## Steps

1. Say which Soleon system you're connected to; production agents live on the production system. Going to production: run release-check first and ask the person once, covering tool servers and agent
2. get_agent(slug, app_env=<from>) and get_agent(slug, app_env=<to>) — both current versions
3. get_agent_document(slug, app_env=<from>, include="published") — every custom_<server>_read/_write id in tools names a tool server the agent uses
4. get_custom_mcp_server(<server>, app_env=<from>) and (…, app_env=<to>) — server.currentVersion on each; skip servers already level. Not a platform admin: ask one to confirm each server on the target
5. promote_custom_mcp(<server>, from_env, to_env, from_version=<source currentVersion>, to_version=<target currentVersion, or leave out if it isn't there yet>)
6. promote_agent(slug, from_env, to_env, from_version, to_version=<target's current version, or leave out the first time>). PROMOTE_CONFLICT: re-read both versions and show what changed; never retry blindly
7. get_agent(slug, app_env=<to>) and get_custom_mcp_server(<server>, app_env=<to>) — read back; the server read on the target is the only proof it's there

## Check it worked

The target's version matches the source, and each tool server reads back on the target. Channels don't travel: bind them per environment. Knowledge bases have nothing to promote (their content is already live everywhere). Undeployed dev drafts don't travel.

## Never on your own

Promote to production without asking.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent`, `get_agent_document`, `get_custom_mcp_server`, `promote_custom_mcp`, `promote_agent`
