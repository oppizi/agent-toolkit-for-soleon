---
name: test-draft-in-chat
description: "Loads your draft into Soleon's test chat with a fresh session. Use when: After an edit, before deploying."
---

# Try your draft in the test chat

Loads your draft into Soleon's test chat with a fresh session.

**Use when:** After an edit, before deploying.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **"Dev" doesn't mean fake data.** Connections (API keys, databases, CRM logins) are shared by dev, staging and production. A dev agent with the CRM connection writes to the real CRM. *Check:* Which systems does it touch, and is there a test copy for dev and staging?
- **Testing a write tool really writes.** Testing a tool that writes, and evals that call tools, run against the real system unless it points at a test copy. *Check:* Which tools must be switched off or pointed at test data during testing?
- **Some things can't be tested before release.** Tests can't reach everything: arguments filled from the real channel are empty in evals and test chat, real sends don't happen, and after-hours branches don't run at noon. *Check:* What is the first real occasion it will be used, and who checks the real conversations then?
- **Tools that look up the customer only work through a channel.** Tools that fetch a customer's own data find the customer from the channel they came in on (a test chat on dev, another on staging, the real one on production). Without a bound channel there is no customer. *Check:* Does it read the customer's own data? Then test it through the test channel for that environment.

## Steps

1. sync_draft_test_chat(slug, app_env="dev")
2. The person chats in Soleon's test chat; no tool sends a test message
3. Then read the conversation with read-a-conversation

## Check it worked

The reply says synced.

## Never on your own

Nothing to guard; test only. Tools still run for real in the test chat.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `sync_draft_test_chat`
