---
name: edit-skill
description: "Creates or replaces one skill in a Soleon agent's draft on dev (the agent's own skills, not Claude Code skills). Use when: The agent needs know-how for one kind of task, or one of its skills is wrong."
---

# Add or rewrite one of the agent's skills

Creates or replaces one skill in a Soleon agent's draft on dev (the agent's own skills, not Claude Code skills).

**Use when:** The agent needs know-how for one kind of task, or one of its skills is wrong.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **Must-follow rules belong in the identity, not a skill.** Skills are summarised and loaded when the agent thinks it needs them. A rule that must always hold can be missed if it only lives in a skill. *Check:* Which rules must hold in every conversation, whatever is asked?
- **When a skill and the identity disagree, the skill wins.** Instructions in a skill are followed over the identity when they conflict. *Check:* After an edit, search the other place for the same topic.
- **Bigger instructions cost more and work worse.** Up to 50 skills; long identities and many skills make every conversation cost more and rules easier to miss. *Check:* Keep one job per skill; remove before you add.
- Also check, if the profile says they apply: A fix in one agent doesn't reach its copies.

## Steps

1. get_agent_document(slug, app_env="dev", include="effective") — start from effective.skills[] (your draft over the live one), keeping the skill's id and files
2. put_agent_skill(slug, app_env="dev", skill={id, name, description, content, enabled, files}) — always send the existing id and files: the skill is replaced whole; limit 50
3. get_agent_draft(slug, app_env="dev") → diff_agent_draft(slug, app_env="dev") — read back
4. validate_agent_draft(slug, app_env="dev")

## Check it worked

The skill text reads back exactly; the count stays within 50; validation passes.

## Never on your own

Send part of a skill (it replaces the whole one) or leave out its id (you get a duplicate). Deploy.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent_document`, `put_agent_skill`, `get_agent_draft`, `diff_agent_draft`, `validate_agent_draft`
