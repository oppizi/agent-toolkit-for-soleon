---
name: find-an-agent
description: "Lists the Soleon agents you can see and shows one agent's setup: identity, skills, tools, knowledge, guardrails, evals and model. Use when: 'Which agents do we have?', 'what does agent X do?', 'how is X set up?'"
---

# Find an agent and see its setup

Lists the Soleon agents you can see and shows one agent's setup: identity, skills, tools, knowledge, guardrails, evals and model.

**Use when:** "Which agents do we have?", "what does agent X do?", "how is X set up?"

## Before you start

- **Some read tools return secrets and personal data.** The deployed-config read, eval runs and version history return an agent's internal connection token; conversation records hold customers' words. Interview requests also carry the invite link, which isn't a way in on its own (single-use, bound to the invited email, entry needs a code) but belongs to the invited person. *Check:* Summarise; never paste raw output.
- **"Not ready" and empty records don't always mean broken.** On staging and production an agent can show as not ready while it works normally. A conversation that hit a runtime error leaves a record with only its header and no turns. *Check:* Check real conversations and failures before calling it down; treat a header-only record as a failed conversation.

## Steps

1. Say which Soleon system you're connected to; production agents live on the production system
2. list_agents(app_env) — always pass app_env; it defaults to dev
3. get_agent(slug, app_env) — summary, version, deploy status, channels
4. get_agent_document(slug, app_env, include="published") — the whole setup, 70–150 KB even for a small agent: read only soul, model, tools, skills[].name and evals. Knowledge bases appear in tools as kb_<kb-slug>_read

## Check it worked

Read-only.

## Never on your own

Paste the whole document back to the person; summarise it.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `list_agents`, `get_agent`, `get_agent_document`
