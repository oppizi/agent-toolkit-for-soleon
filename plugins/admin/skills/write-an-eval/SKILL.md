---
name: write-an-eval
description: "Adds, rewrites or deletes one test conversation in an agent's draft on dev. Use when: A behaviour needs a test before or after a change."
---

# Write or remove one eval

Adds, rewrites or deletes one test conversation in an agent's draft on dev.

**Use when:** A behaviour needs a test before or after a change.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **Anything shared needs a "check first" step.** Before creating, sending or spending, the agent must re-read the shared system to see if someone already did it, and record who did what. *Check:* What must it check before it creates, sends or pays for something?
- **The agent has no time zone of its own.** Agents have no time-zone setting; schedules default to New York time. Hours, holidays and "today" need care. *Check:* Does anything depend on the time, date, time zone, business hours or holidays?
- **Eval scores are not pass or fail, and they wobble.** Each eval is scored 0 to 100 by a judge with no pass mark. The same version can score very differently on two runs. *Check:* Which 3 to 5 behaviours prove it works, written as yes/no facts?
- **On email, whatever the agent writes is sent.** On an email channel the agent's final text is the email, sent at once. The only way to stay silent is to write nothing. *Check:* Tell the agent exactly when to write nothing.
- **Long-term memory can act on old conversations.** The agent can carry notes from earlier conversations with the same person, and a session restarts after about an hour idle. Unless the identity says memory is background only, it may act on it. *Check:* Should it remember earlier conversations with the same person, and may it act on them?
- Also check, if the profile says they apply: Evals in one run can leak into each other; An agent with the file tool can read its own tests.

## Steps

1. get_agent_document(slug, app_env="dev", include="effective") — existing evals in evals.standardEvals[]
2. put_standard_eval(slug, app_env="dev", eval={name, inputs ending with a user turn, evaluationCriteria, enabled}) — criteria are yes/no facts the judge can check; leave expectedOutput empty unless resemblance to a reference answer is the point
3. delete_standard_eval(slug, app_env="dev", eval_id)
4. get_agent_draft(slug, app_env="dev") — read back

## Check it worked

The eval reads back as written.

## Never on your own

Delete an eval without asking. Put the ideal answer in an eval's inputs (the agent can see it).


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent_document`, `put_standard_eval`, `delete_standard_eval`, `get_agent_draft`
