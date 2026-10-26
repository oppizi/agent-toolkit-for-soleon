---
name: read-identity
description: "Shows the identity (main instructions) an agent runs on in one environment, or your draft of it. Use when: Before an edit, or to answer 'what does its identity say about X?'"
---

# Read an agent's identity

Shows the identity (main instructions) an agent runs on in one environment, or your draft of it.

**Use when:** Before an edit, or to answer "what does its identity say about X?"

## Before you start

- **Some read tools return secrets and personal data.** The deployed-config read, eval runs and version history return an agent's internal connection token; conversation records hold customers' words. Interview requests also carry the invite link, which isn't a way in on its own (single-use, bound to the invited email, entry needs a code) but belongs to the invited person. *Check:* Summarise; never paste raw output.

## Steps

1. get_agent_document(slug, app_env, include="effective") — read only effective.soul (live plus your draft edits); the reply is 70–150 KB and the identity alone can be 40 KB
2. get_agent_document(…, include="published") — to see exactly what is live, without your draft

## Check it worked

Read-only.

## Never on your own

Use get_agent_config for this: it returns the agent's connection token.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent_document`
