---
name: read-skills
description: "Lists the skills a Soleon agent has, with on/off state, and shows their text. Use when: Before editing one of the agent's own skills (not Claude Code skills), or to see why the agent does something."
---

# List and read an agent's skills

Lists the skills a Soleon agent has, with on/off state, and shows their text.

**Use when:** Before editing one of the agent's own skills (not Claude Code skills), or to see why the agent does something.

## Steps

1. get_agent_skills(slug, app_env, content_mode="names") — the deployed skills, no text
2. get_agent_skills(slug, app_env, content_mode="full") — every skill's full text at once (about 40 KB for 7 skills); read only the one you need. It shows the deployed copy, not your draft

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

Tools: `get_agent_skills`
