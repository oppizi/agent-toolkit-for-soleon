---
name: browse-knowledge
description: "Lists knowledge bases and reads their pages. Use when: Checking what an agent can know, or where a wrong answer came from."
---

# Find and read a knowledge base

Lists knowledge bases and reads their pages.

**Use when:** Checking what an agent can know, or where a wrong answer came from.

## Steps

1. list_knowledge_bases(app_env="dev") — knowledge bases are listed on dev only (production and staging return [] even for ones production agents use); note the kbId (hex), not the slug
2. get_knowledge_base(kb_id=<kbId>) — no app_env on this or the next two
3. list_kb_pages(kb_id, with_fact_counts=true) — each page's path is the SK value after "PAGE#"; the title is not always the path
4. get_kb_page(kb_id, path=<path>, include_facts=true)

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

Tools: `list_knowledge_bases`, `get_knowledge_base`, `list_kb_pages`, `get_kb_page`
