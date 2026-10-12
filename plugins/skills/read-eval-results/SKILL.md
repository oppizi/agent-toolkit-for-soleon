---
name: read-eval-results
description: "Shows an agent's recent eval runs, their scores and the trend. Runs nothing. Use when: 'How is the agent scoring?', 'what did the last run say?' To start a new run, use run-an-eval."
---

# Read eval results

Shows an agent's recent eval runs, their scores and the trend. Runs nothing.

**Use when:** "How is the agent scoring?", "what did the last run say?" To start a new run, use run-an-eval.

## Before you start

- **Eval scores are not pass or fail, and they wobble.** Each eval is scored 0 to 100 by a judge with no pass mark. The same version can score very differently on two runs. *Check:* Which 3 to 5 behaviours prove it works, written as yes/no facts?
- **Evals in one run can leak into each other.** Evals in a run share memory, so one eval can see another's answers; wording like "don't look anything up" also stops the agent reading its skills. *Check:* Keep ideal answers out of eval inputs; run a doubtful eval alone.
- **An agent with the file tool can read its own tests.** The agent's settings file sits in its file workspace, and it holds every test conversation and the judge's scoring rules. An agent with the file tool can read them. *Check:* Switch the file tool off unless the job needs it; never put the ideal answer in a test's input.

## Steps

1. list_eval_runs(slug, app_env="dev") — evals run on dev. Each run has status and sideA.avgScore; the trend block's points are 0–1 while rollingAvgScore is 0–100. Runs record "deployed" or "draft", not a version number: match dates with the deploy
2. get_eval_run(slug, run_id, app_env="dev") — per eval: name, outcome, score. About 100–150 KB, mostly a copy of the agent: read only those fields
3. get_eval_scoreboard(slug, app_env, days=28) — scores of live conversations only; empty means no live scoring, not no evals

## Check it worked

Read-only. outcome "score_only" is normal: scores are 0 to 100 with no pass mark, and move between runs. Criteria are weighted points; ignore any advice about pass/fail modes or score thresholds.

## Never on your own

Paste or save get_eval_run output: its copy of the agent carries the connection token.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `list_eval_runs`, `get_eval_run`, `get_eval_scoreboard`
