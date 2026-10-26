---
name: read-a-conversation
description: "Finds an agent's conversations by time and shows every turn and tool call in one. Use when: A complaint about a specific chat, or checking a test."
---

# Find and read one conversation

Finds an agent's conversations by time and shows every turn and tool call in one.

**Use when:** A complaint about a specific chat, or checking a test.

## Before you start

- **Some read tools return secrets and personal data.** The deployed-config read, eval runs and version history return an agent's internal connection token; conversation records hold customers' words. Interview requests also carry the invite link, which isn't a way in on its own (single-use, bound to the invited email, entry needs a code) but belongs to the invited person. *Check:* Summarise; never paste raw output.
- **"Not ready" and empty records don't always mean broken.** On staging and production an agent can show as not ready while it works normally. A conversation that hit a runtime error leaves a record with only its header and no turns. *Check:* Check real conversations and failures before calling it down; treat a header-only record as a failed conversation.

## Steps

1. list_trace_sessions(agent_slug, app_env, since_iso, limit) — each has a one-line desc; eval traffic is under user_namespace="__eval__"; errored conversations aren't listed here (use failures-check)
2. get_trace_session(session_id) — even a one-question chat can be 500 KB+ (it holds the full prompt sent to the model); it will be saved to a file: read turns[].turn.status and steps[].label/status with a script. Read tool calls from steps of type tool; turn.toolsUsed is often empty

## Check it worked

Read-only. No results means none only if you're a platform admin: others see only their own conversations.

## Never on your own

Paste customer messages or trace output (it can carry a callback token); delete the saved file when done.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `list_trace_sessions`, `get_trace_session`
