---
name: pause-agent
description: "Switches an agent off in one environment (users see a message you set) or back on. Use when: An incident: the agent must stop answering now."
---

# Pause or resume an agent

Switches an agent off in one environment (users see a message you set) or back on.

**Use when:** An incident: the agent must stop answering now.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **"Instant" cuts off conversations in progress.** Deploys, rollbacks and pauses can be passive (running chats finish) or instant (running chats stop). *Check:* Passive unless the agent must stop now.

## Steps

1. set_agent_active(slug, active=false, app_env, inactive_message) — takes effect at once and stops running conversations
2. get_agent(slug, app_env) — read back
3. To resume: run release-check, then set_agent_active(slug, active=true, app_env)

## Check it worked

The active state reads back.

## Never on your own

Pause or resume without asking: it takes effect at once.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `set_agent_active`, `get_agent`
