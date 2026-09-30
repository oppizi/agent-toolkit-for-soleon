---
name: cost-check
description: "Shows an agent's dollar cost by model and day, and how much was test traffic. Use when: 'What does this agent cost?'"
---

# Check cost

Shows an agent's dollar cost by model and day, and how much was test traffic.

**Use when:** "What does this agent cost?"

## Before you start

- **The model and the budgets set the bill.** The model, token budgets per message and per user per day (0 means no limit), thinking effort and prompt caching drive cost. *Check:* How many conversations a day, and what cost per month is acceptable?

## Steps

1. get_cost_summary(agent_slug, app_env, days=7) — always pass agent_slug (without it, dev returns about 1 MB of test-agent rows). testCostUSD is test traffic; byModel can list models besides the agent's own (side calls are billed to the agent); pricingIncompleteRows > 0 means the total is low; truncated=true means a partial sum

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

Tools: `get_cost_summary`
