---
name: run-an-eval
description: "Starts an eval run on your draft or the deployed version, alone or side by side, and waits for the scores. Costs money. Use when: Before and after a change, to test a behaviour. To read results without running anything, use read-eval-results."
---

# Run evals

Starts an eval run on your draft or the deployed version, alone or side by side, and waits for the scores. Costs money.

**Use when:** Before and after a change, to test a behaviour. To read results without running anything, use read-eval-results.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **"Dev" doesn't mean fake data.** Connections (API keys, databases, CRM logins) are shared by dev, staging and production. A dev agent with the CRM connection writes to the real CRM. *Check:* Which systems does it touch, and is there a test copy for dev and staging?
- **Testing a write tool really writes.** Testing a tool that writes, and evals that call tools, run against the real system unless it points at a test copy. *Check:* Which tools must be switched off or pointed at test data during testing?
- **Evals cost money.** Every eval run calls the model and a judge. Running the whole list on every small change adds up. *Check:* One failing check per fix; the full list once before production.
- **Evals in one run can leak into each other.** Evals in a run share memory, so one eval can see another's answers; wording like "don't look anything up" also stops the agent reading its skills. *Check:* Keep ideal answers out of eval inputs; run a doubtful eval alone.
- **Tools that look up the customer only work through a channel.** Tools that fetch a customer's own data find the customer from the channel they came in on (a test chat on dev, another on staging, the real one on production). Without a bound channel there is no customer. *Check:* Does it read the customer's own data? Then test it through the test channel for that environment.
- Also check, if the profile says they apply: Eval scores are not pass or fail, and they wobble; Some things can't be tested before release; An agent with the file tool can read its own tests.

## Steps

1. get_agent_document(slug, app_env="dev", include="effective" for a draft side A, "published" for deployed) — the ids are evals.standardEvals[].id
2. run_eval(slug, app_env="dev", selected_eval_ids, side_a_version_ref={kind: "draft"} for undeployed edits or {kind: "deployed"}, mode="single") — or mode="side_by_side" with side_b_version_ref={kind: "deployed"} (costs twice as much). Right after a deploy there is no draft: use deployed
3. get_eval_run(slug, run_id, app_env="dev") — repeat while running; read only each eval's name, outcome and score
4. 409 EVAL_RUN_IN_FLIGHT (maybe someone else's run): poll that runId until it finishes, then start yours
5. If a score looks wrong: an agent whose tools look up the customer needs its test channel; an agent with the file tool may read its own tests

## Check it worked

The run reads complete, with a score per eval (no pass mark).

## Never on your own

Run the whole list each round: one failing check per fix, the full suite once before production. One run per agent at a time, 50 evals at most. Paste get_eval_run output (it carries a token).


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent_document`, `run_eval`, `get_eval_run`
