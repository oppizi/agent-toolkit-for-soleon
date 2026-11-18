---
name: browse-knowledge
description: "Searches a knowledge base, lists knowledge bases and reads their pages. Use when: Looking something up in a knowledge base (how a system or code base works, why it was built that way), checking what an agent can know, or finding where a wrong answer came from."
---

# Search and read a knowledge base

Searches a knowledge base the way its agents do, then reads the pages that answer.

**Use when:** Looking something up in a knowledge base (how a system or code base works, why it was built that way), checking what an agent can know, or finding where a wrong answer came from.

## Steps

1. Find the base: list_knowledge_bases(app_env="dev") — knowledge bases are listed on dev only (production and staging return [] even for ones production agents use); note the kbId (hex), not the slug. Skip this when you already know the kbId.
2. search_knowledge_base(kb_id, query="<the question, in words>") — the same search agents run: keyword and meaning-based matching, then a relevance grade per hit (`relevance`) and the knowledge-graph facts that answer it. `answered: false` means no page directly answers the question: say so rather than stretching a near miss. Default limit 8 (max 20); rerank=false skips the grading (cheaper, rougher order). Each search writes one trace under you and bills its two model calls to you.
3. get_kb_page(kb_id, path=<hit path>) for the hits that look like the answer — read before you quote. include_facts=true adds the page's graph facts.
4. To browse instead of search: get_knowledge_base(kb_id), then list_kb_pages(kb_id, with_fact_counts=true) — each page's path is the SK value after "PAGE#"; the title is not always the path.

## Check it worked

Read-only. A knowledge base is built from its sources and can lag them: before acting on what a page says about code, confirm it in the code (file and line), and say which you checked.

## Never on your own

Nothing to guard; read-only. If a page is wrong, say so and name the page; fixing it (put_kb_page, or the source it came from) is the person's call.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`; search_knowledge_base's `app_env` only says where its trace is filed.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `search_knowledge_base`, `list_knowledge_bases`, `get_knowledge_base`, `list_kb_pages`, `get_kb_page`
