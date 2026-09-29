---
name: failures-check
description: "Lists an agent's recent errors and opens the conversation behind one. Use when: Something broke, or a regular health check."
---

# Check failures

Lists an agent's recent errors and opens the conversation behind one.

**Use when:** Something broke, or a regular health check.

## Before you start

- **Scheduled runs happen with nobody watching.** Automations run on a timer, may run once per user, and can post to real channels. Letting users create their own schedules adds more. *Check:* Does it run on a schedule? Who sees its output and its failures?
- **A failed action can look like success.** A tool that returns "not sent" still counts as a successful call, so nothing shows under failures. *Check:* How would anyone know the action failed?
- **"Not ready" and empty records don't always mean broken.** On staging and production an agent can show as not ready while it works normally. A conversation that hit a runtime error leaves a record with only its header and no turns. *Check:* Check real conversations and failures before calling it down; treat a header-only record as a failed conversation.

## Steps

1. list_failures(agent_slug, app_env, limit) — newest first, no date filter. type "guardrail" means a content filter blocked a message (working as set); "system" is a real error
2. get_trace_session(session_id) — a failed conversation may have no turns, or one turn with status error whose prompt is the error text; the customer's message isn't kept. These don't appear in list_trace_sessions

## Check it worked

Read-only. No results means healthy only if you're a platform admin: others see only their own failures.

## Never on your own

Paste trace output: it can carry a callback token.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `list_failures`, `get_trace_session`
