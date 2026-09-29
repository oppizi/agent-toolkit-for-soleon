---
name: release-check
description: "Reads everything that decides what real people will experience and walks the person through it in plain words before a production release, a channel binding, an un-pause or a Launch. Use when: Right before promoting to production, binding a channel, un-pausing, or moving a ticket to Launch."
---

# Before it reaches real people: the release check

Reads everything that decides what real people will experience and walks the person through it in plain words before a production release, a channel binding, an un-pause or a Launch.

**Use when:** Right before promoting to production, binding a channel, un-pausing, or moving a ticket to Launch.

## Before you start

The summary covers six lines. Under each, raise only the risks that match the agent's type in the profile:

- **Who will reach it:** channels, private or public, outsiders.
- **What it can do without asking:** tools with approval off, web and file tools.
- **What it must never do, and what stops it:** guardrails, identity rules, approvals.
- **The most it can cost:** model, budgets, caching, live scoring.
- **Who takes over when it can't help:** named person or team, hours, contact details.
- **How to undo it:** the version to return to on each environment; shared tool servers and knowledge bases don't roll back.

Risks to check against the profile: The type of agent decides almost everything else; The channel decides the audience; Channels are bound per environment, and binding is going live; Who can see and open the agent in Soleon; Every user gets a private session with no shared memory; Reading is safe; writing, sending, spending and deleting are not; On a customer channel, "approval" asks the customer; Sending from the company domain can get it blacklisted; Web and file tools are on unless someone turns them off; Guardrails are off until someone switches them on; What it says, the company must honour; Scheduled runs happen with nobody watching; Someone must be there when it hands over; Some things can't be tested before release; Deploy puts it on dev; production is two more steps; Moving a ticket to Launch can make an agent live; Rolling back one agent doesn't roll back what it shares; The model and the budgets set the bill; A private agent refuses everyone outside the company; Two agents on one chat compete; Some channels need a filter, or they route nothing; Tools act as, and check, whoever is chatting; An Off switch isn't proof a tool can't run; A tool can hand the agent data its audience mustn't see; Long-term memory can act on old conversations; Each production move is its own yes; An agent with the file tool can read its own tests; A tool test "on production" isn't proof it's there; Tools that look up the customer only work through a channel.

## Steps

1. Read agent-profile.md (or run start-an-agent first). Say which Soleon system you're connected to
2. get_agent(slug, app_env=<target>) — private or public, registration, channels; runtimeReady false with status idle is normal
3. get_agent_document(slug, app_env=<target>, include="effective") — guardrails, tools and their approval switches, web and file tools, model, promptCaching, automations (schedules)
4. get_custom_mcp_server(<server from custom_<server>_read ids>, app_env=<target>) — check overrides.<target> for the database or connection it really uses; get_tool_policy(integration_id="custom_mcp:<server>", app_env=<target>). Neither shows other agents using the server: search list_agents / get_agent_document for it. Not an admin: ask one
5. list_eval_runs(slug, app_env="dev") — latest results; runs don't record a version number, so compare dates with the promotion
6. get_agent(slug, app_env) for each environment — the version to return to if it goes wrong
7. Show a one-screen summary: who will reach it; what it can do without asking; what it must never do and what stops it; the most it can cost; who takes over when it can't help; how to undo it. The person confirms each line

## Check it worked

Every line of the summary matches the profile, or the difference is agreed with the person.

## Never on your own

Release anything itself. It only reads and reports; the release skill runs after the person says yes.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent`, `get_agent_document`, `get_custom_mcp_server`, `get_tool_policy`, `list_eval_runs`
