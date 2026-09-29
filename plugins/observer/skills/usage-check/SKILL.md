---
name: usage-check
description: "Counts an agent's conversations and tokens, by day. Use when: 'Is anyone using it?', 'did traffic drop?' For money, use cost-check."
---

# Check usage

Counts an agent's conversations and tokens, by day.

**Use when:** "Is anyone using it?", "did traffic drop?" For money, use cost-check.

## Steps

1. get_usage_summary(agent_slug, app_env, days=7) — sessions and tokens; duration fields read 0 (not recorded); byChannel lists one row per anonymous visitor, not per channel type; conversations that errored before starting aren't counted

## Check it worked

Read-only. Token totals won't match cost-check (different window and counting).

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

Tools: `get_usage_summary`
