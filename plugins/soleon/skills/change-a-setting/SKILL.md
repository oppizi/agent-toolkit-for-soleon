---
name: change-a-setting
description: "Changes only the named fields in your draft on dev and leaves the rest alone. Use when: Draft settings without their own skill: loop fields in bulk, computeMode, access. Not for description, display name, icon or open registration (use agent-profile: update_agent_profile changes the dev agent at once, no draft). Not for tools and approvals, model, guardrails or schedules: use their own skills."
---

# Change one draft setting

Changes only the named fields in your draft on dev and leaves the rest alone.

**Use when:** Draft settings without their own skill: loop fields in bulk, computeMode, access. Not for description, display name, icon or open registration (use agent-profile: update_agent_profile changes the dev agent at once, no draft). Not for tools and approvals, model, guardrails or schedules: use their own skills.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **Who can see and open the agent in Soleon.** An agent is public (every signed-in member can open it) or private (owner, admins and people granted a role). Registration can be opened so end users sign themselves up. Roles: admin, collaborator, user, viewer. *Check:* Who should be able to see it, use it and change it?
- **A whole-document save can flip a private agent to public.** Saving the whole agent document without the access field sets the agent to public. Fields left out of a full save are dropped. *Check:* (Build rule, not a question) Change only the fields you mean to change.

## Steps

1. get_agent_draft(slug, app_env="dev") — keep draftEtag only if hasDraft is true; never pass baselineEtag as expected_updated_at
2. patch_agent_draft(slug, app_env="dev", changes, expected_updated_at=<draftEtag, if any>) — flat document; null deletes a key; lists replace whole. 409 DRAFT_CONFLICT: someone changed the same field; re-read, show the person, re-apply
3. get_agent_draft(slug, app_env="dev") — read the field back
4. update_agent_draft(slug, app_env="dev", agent, baseline_etag=<baselineEtag>, expected_updated_at=<draftEtag, if any>) — only for a deliberate whole-document rewrite, with the person's OK; without baseline_etag the deploy can silently undo others' published changes

## Check it worked

The field reads back as sent; diff_agent_draft shows nothing else moved.

## Never on your own

Send null or leave out access (the agent becomes public), change access without asking, nest fields under config (they're dropped silently), or trust a success reply without reading back.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent_draft`, `patch_agent_draft`, `update_agent_draft`
