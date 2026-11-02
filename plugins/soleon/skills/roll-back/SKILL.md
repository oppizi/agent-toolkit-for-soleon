---
name: roll-back
description: "Puts an earlier version back live on dev as a new version, then (with the person's OK) promotes it up to wherever the bad version is. Use when: A deploy or release made things worse."
---

# Roll back to an earlier version

Puts an earlier version back live on dev as a new version, then (with the person's OK) promotes it up to wherever the bad version is.

**Use when:** A deploy or release made things worse.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **"Instant" cuts off conversations in progress.** Deploys, rollbacks and pauses can be passive (running chats finish) or instant (running chats stop). *Check:* Passive unless the agent must stop now.
- **Rolling back one agent doesn't roll back what it shares.** A rollback restores the agent's own settings on dev (then it must be promoted). Shared tool servers and knowledge stay as they are. *Check:* Know each piece's version per environment before a release, so you can undo it.

## Steps

1. get_agent(slug, app_env) for dev, staging and prod — where the bad version runs
2. list_versions(kind="agent", slug, app_env="dev") — pick the version to return to (it's large: read only version, versionId, committedAt, commitMessage)
3. get_version_diff(kind="agent", slug, to_ref=<versionId>, from_ref="dev", app_env="dev") — show the person what reverts
4. revert_agent_version(slug, version_id, app_env="dev", deploy_mode="passive") — the tool defaults to "instant", which stops running conversations: pass passive unless it must stop now
5. To fix staging or production: promote (dev → staging → production), with the person's OK at each step
6. get_agent(slug, app_env) — read back

## Check it worked

The new version matches the chosen one on each environment you rolled back.

## Never on your own

Roll back without asking: it goes live. Shared tool servers and knowledge bases are not rolled back with the agent.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent`, `list_versions`, `get_version_diff`, `revert_agent_version`
