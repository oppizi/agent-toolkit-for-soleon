---
name: connect-channel
description: "Binds or unbinds an agent to Slack, website chat, email or another channel, in one environment. Use when: An agent should (or should stop) answering real people somewhere."
---

# Connect an agent to a channel, or disconnect it

Binds or unbinds an agent to Slack, website chat, email or another channel, in one environment.

**Use when:** An agent should (or should stop) answering real people somewhere.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **The channel decides the audience.** Soleon chat reaches signed-in staff. A Slack channel reaches everyone in it. Website or in-app chat reaches customers and the public. Email and lemlist reach prospects. *Check:* Where will people talk to it, and who exactly are they?
- **Channels are bound per environment, and binding is going live.** Dev, staging and production each have their own channel bindings. Promoting an agent doesn't move its channels. Binding on production is the moment real people reach it. *Check:* Which environment should people reach first, and on which channel in each?
- **On a customer channel, "approval" asks the customer.** An approval prompt goes to whoever is in the conversation. In website chat or email that is the customer or prospect, not a colleague. *Check:* On this channel, who is in the conversation when it wants to act, and who should really approve?
- **Sending from the company domain can get it blacklisted.** Outbound email from the main company domain, or look-alike domains in the same account, puts the whole company's email reputation at risk. *Check:* Which address and domain does it send from, and how many emails a day?
- **A private agent refuses everyone outside the company.** Binding a channel doesn't give outsiders access. A private agent turns away every sender who isn't a Soleon user, without an error they can see. *Check:* Will people outside the company write to it? Then it can't stay private on that environment.
- Also check, if the profile says they apply: Two agents on one chat compete; Some channels need a filter, or they route nothing; On email, whatever the agent writes is sent; Each production move is its own yes; Tools that look up the customer only work through a channel.

## Steps

1. Run release-check first when real people will reach it
2. Admins: get_channel_instance(instance_id) — which connection point it is, and its health
3. bind_agent_channel(slug, instance_id, app_env, deploy_mode) / unbind_agent_channel(slug, instance_id, app_env)
4. get_agent(slug, app_env) — the channel reads back on that environment

## Check it worked

The channel reads back on that environment's agent.

## Never on your own

Bind or unbind without asking: real people start or stop reaching the agent. Channels don't carry across environments.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_channel_instance`, `bind_agent_channel`, `unbind_agent_channel`, `get_agent`
