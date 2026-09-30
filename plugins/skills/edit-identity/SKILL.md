---
name: edit-identity
description: "Replaces, adds, renames or removes one section of an agent's identity in your draft on dev, then shows the change. Use when: Any wording change to how a Soleon agent behaves."
---

# Edit the identity, one section at a time

Replaces, adds, renames or removes one section of an agent's identity in your draft on dev, then shows the change.

**Use when:** Any wording change to how a Soleon agent behaves.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **Must-follow rules belong in the identity, not a skill.** Skills are summarised and loaded when the agent thinks it needs them. A rule that must always hold can be missed if it only lives in a skill. *Check:* Which rules must hold in every conversation, whatever is asked?
- **When a skill and the identity disagree, the skill wins.** Instructions in a skill are followed over the identity when they conflict. *Check:* After an edit, search the other place for the same topic.
- **What it says, the company must honour.** Prices, dates, response times, availability and legal claims made by the agent are commitments. *Check:* What may it promise, and what must it always pass to a person?
- **A fix in one agent doesn't reach its copies.** Agents cloned from one another (for example one per country) keep their own copy of every rule and tool. A fix in one stays there. *Check:* Is this agent a copy of another, or copied by others?
- Also check, if the profile says they apply: Anything shared needs a "check first" step; Two sources that disagree make the agent unpredictable; Someone must be there when it hands over; Bigger instructions cost more and work worse; On email, whatever the agent writes is sent; The channel may not tell the agent who it's talking to; Long-term memory can act on old conversations; A copied agent carries the original's facts; Tool descriptions are instructions too.

## Steps

1. get_agent_draft(slug, app_env="dev") — note hasDraft; no draft is fine, the first edit creates one
2. get_agent_soul_sections(slug, app_env="dev") — exact heading names, levels and sizes (works without a draft)
3. Show the person the current section text and your new text, and get their OK
4. edit_agent_soul(slug, app_env="dev", mode="replace_section", section=<exact heading>, content) — a section includes every deeper heading under it: never target a top-level (#) heading unless you mean to replace everything beneath it. Other modes: insert_after (content starts with its own heading), rename_section, delete_section; replace_all only for a full rewrite
5. diff_agent_draft(slug, app_env="dev") — confirm the change
6. validate_agent_draft(slug, app_env="dev") — catches anything the deploy would refuse

## Known limits

replace_section drops the blank line under the heading and squeezes double blank lines across the whole identity; expect that in the diff.

## Check it worked

get_agent_soul_sections and diff_agent_draft show the new text. Errors: SECTION_NOT_FOUND → pick a title from the list it returns; SECTION_AMBIGUOUS → two headings share the name, rename one first; EMPTY_SOUL → you were about to delete everything.

## Never on your own

Delete a section or rewrite the whole identity without showing the person the before and after first. Deploy (that's its own skill).


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent_draft`, `get_agent_soul_sections`, `edit_agent_soul`, `diff_agent_draft`, `validate_agent_draft`
