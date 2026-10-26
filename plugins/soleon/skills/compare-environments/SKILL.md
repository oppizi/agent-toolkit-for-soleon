---
name: compare-environments
description: "Shows which version each environment runs, whether production is up to date, and what changed between two versions. Use when: 'Is production up to date?', 'what changed since the last release?', before a promotion."
---

# See which version runs on dev, staging and production

Shows which version each environment runs, whether production is up to date, and what changed between two versions.

**Use when:** "Is production up to date?", "what changed since the last release?", before a promotion.

## Before you start

- **Deploy puts it on dev; production is two more steps.** Draft → deploy to dev → promote to staging → promote to production. Each environment has its own version, channels and settings, and a dev agent can already be bound to a real channel, so a dev deploy can reach real people. *Check:* Which environment will real people use, and what must be true before it gets there?

## Steps

1. get_agent(slug, app_env) for dev, staging and prod — the version each one runs
2. get_version_diff(kind="agent", slug, to_ref="dev", from_ref="prod", detail="summary", app_env="dev") — "resolve to the same version" means production is up to date
3. For the text: get_version_diff(…, detail="patches", file_filter="<slug>/SOUL.md") — the slug prefix is required; plain "SOUL.md" silently returns no files
4. list_versions(kind="agent", slug, app_env) — only when you need the history: it is very large on dev

## Check it worked

Read-only.

## Never on your own

Paste or save list_versions output: every version's saved state carries the agent's connection token.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent`, `get_version_diff`, `list_versions`
