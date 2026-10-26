---
name: deploy-to-dev
description: "Checks your draft of an existing Soleon agent, shows the change and puts it live on dev. Not for creating an agent from a local .claude/agents file (use deploy-agent). Use when: Your draft edits are ready to try for real on dev."
---

# Deploy your draft to dev

Checks your draft of an existing Soleon agent, shows the change and puts it live on dev. Not for creating an agent from a local .claude/agents file (use deploy-agent).

**Use when:** Your draft edits are ready to try for real on dev.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **Drafts belong to one person.** Your edits sit in your own draft. A colleague can't see them, and theirs aren't in yours. Whoever deploys puts their own draft live. *Check:* Who else is changing this agent right now?
- **"Dev" doesn't mean fake data.** Connections (API keys, databases, CRM logins) are shared by dev, staging and production. A dev agent with the CRM connection writes to the real CRM. *Check:* Which systems does it touch, and is there a test copy for dev and staging?
- **Deploy puts it on dev; production is two more steps.** Draft → deploy to dev → promote to staging → promote to production. Each environment has its own version, channels and settings, and a dev agent can already be bound to a real channel, so a dev deploy can reach real people. *Check:* Which environment will real people use, and what must be true before it gets there?
- **"Instant" cuts off conversations in progress.** Deploys, rollbacks and pauses can be passive (running chats finish) or instant (running chats stop). *Check:* Passive unless the agent must stop now.

## Steps

1. get_agent(slug, app_env="dev") — note its channels: if dev is bound to a real channel, real people get this change
2. validate_agent_draft(slug, app_env="dev") — no draft means nothing to deploy: stop (don't follow its hint to rebuild from get_agent_config)
3. diff_agent_draft(slug, app_env="dev") — show the person what goes live; stop if access changed and they didn't ask
4. deploy_agent_draft(slug, app_env="dev", deploy_mode="passive") — "instant" stops running conversations. 409 DRAFT_REMOVES_FIELDS: show removedKeys and re-stage; 409 DRAFT_STALE: someone deployed since your draft began, stop and ask; 409 VERSION_MISMATCH: re-read and retry once
5. get_agent(slug, app_env="dev") — repeat until deployStatus.status is idle, error is null and the version went up

## Check it worked

The dev version went up with no deploy error, and the change shows in get_agent_document(include="published").

## Never on your own

Pass confirm_removals=true without asking (it deletes content from the agent). Deploy when dev is bound to real people without saying so.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent`, `validate_agent_draft`, `diff_agent_draft`, `deploy_agent_draft`
