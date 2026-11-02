---
name: review-draft
description: "Shows your pending (undeployed) edits to an agent against what's live on dev, and whether a deploy would accept them. Use when: 'What have I changed?', picking work back up, or before any deploy. Not for comparing environments (use compare-environments)."
---

# See what your draft would change

Shows your pending (undeployed) edits to an agent against what's live on dev, and whether a deploy would accept them.

**Use when:** "What have I changed?", picking work back up, or before any deploy. Not for comparing environments (use compare-environments).

## Before you start

- **Drafts belong to one person.** Your edits sit in your own draft. A colleague can't see them, and theirs aren't in yours. Whoever deploys puts their own draft live. *Check:* Who else is changing this agent right now?

## Steps

1. get_agent_document(slug, app_env="dev", include="draft") — if hasDraft is false you have no pending edits: stop (don't follow a 404's hint to rebuild a draft; it tells you to write). Drafts exist on dev only and belong to one person
2. diff_agent_draft(slug, app_env="dev") — field by field; "" → null on description or icon fields and reordered tools are noise
3. validate_agent_draft(slug, app_env="dev") — valid, errors, and removedKeys: content the deploy would refuse to drop

## Check it worked

Read-only.

## Never on your own

Nothing to guard; read-only.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent_document`, `diff_agent_draft`, `validate_agent_draft`
