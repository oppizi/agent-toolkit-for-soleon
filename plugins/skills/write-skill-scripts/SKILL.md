---
name: write-skill-scripts
description: Use when writing, changing or debugging a Python script that a Soleon agent runs from one of its skills (run_code_file, write_and_run_code), especially one that calls paid tools, moves many rows into a Sheet or file, takes inputs the agent types, or has failed with "Tool call budget reached", "request_conflict", "scope_violation", "exceeds current policy maximum", "Result exceeds the 64 KiB limit", a timeout, or a flooded context.
---

# Write a script for an agent's skill

A Soleon agent can run a Python script that lives in one of its skills
(`<skill>/scripts/<name>.py`) with `run_code_file`. Done well, the script moves the data and
spends the money, and the agent only fills in a form and reads a short answer.

**Core principle: the agent's context is for decisions, not data.** The script checks its inputs
before it spends anything, writes results to their destination itself, stays inside its own
limits, and hands back only counts, cost, links or an error that says what to fix.

## Before you write anything

1. Call `get_sandbox_script_rules` (Soleon MCP). It returns the script shape, the exact
   `tools` / `execution` / `llm` signatures, the packages installed, an example script and the
   `run_code_file` request that runs it. The sandbox is offline: nothing else is available.
2. List the agent's tools (`see-agent-tools` skill) and **copy the exact tool names**. A
   misspelt name in `tool_scope` is refused as "Tool is unavailable, forbidden, or
   duplicated", with no suggestion.
3. Start from this skill's `assets/script_template.py`; test with its `assets/run_locally.py`
   (`assets/example_fakes.py` and `assets/example_form.json` show the inputs it takes).

Platform facts with their source lines: `references/platform-facts.md`.

## The five rules

### 1. Data never passes through the model

- The script writes rows to the destination itself (a Sheet, a database tool, a CRM) and
  returns only counts, cost, links and problems. **Never return the rows**, not even "so the
  agent can double-check": put the checks in the script and return their result. Where the
  destination can be read back (a row count), check that, not the script's own counter.
- A return value over 64 KiB fails the whole run. A return value under that still lands in
  the agent's context on every run.
- Saving a CSV with `execution.save` is not a way round it. Each saved CSV comes back to the
  model as a summary of up to about 24 KB of rows plus 24 KB of column counts. Save a file only
  when the person needs the file.
- `output_scope.deliver` defaults to true: every saved file gets attached to the chat. Set
  `"deliver": false` unless the person asked for the file as an attachment.
- A resume `state` comes back through the model too. Keep it a cursor (a page number, an id,
  a Sheet id), never the rows collected so far.

### 2. Validate first

- `check_inputs(inputs)` is the first thing the script runs. It returns **every** problem at
  once, each naming the field and the fix: `"region 'Califonia' is not allowed (did you mean
  'California'?); use one of: ..."`.
- On any problem, return `{"error": "nothing was run; fix this and run once more: ..."}`
  **before the first tool call**: free and paid calls alike. A bad run must cost nothing.
- Check against fixed allowed lists in the script, not by calling a tool to see what comes back.
  Unknown keys are errors too (a misspelt `due_befor` silently means "no filter").
- Never silently correct or fuzzy-match a value into a run. Suggest; the agent re-runs with the
  fixed value.
- Check every reply a tool sends back, writes included, by **confirming success**, never by
  looking for error words: errors come in endless shapes ("HttpError 403", "Tool execution
  failed", `{"success": false}`, `{"updatedRows": 0}`).
  - A read counts only when the fields the script needs are there with values: `rows` is a
    list, each record has its `id`, the `email` is not empty. A reply that says it failed
    (`success: false`, `ok: false`) is an error even when it carries an empty list.
  - A write counts only when its reply confirms it: the row count written equals the rows
    sent, or the link or id you need is in it. Anything else is a failed write.
  - Missing fields are an error to return, never an empty list or a blank cell. A misnamed field
    must not become "done, 0 found".
  - An empty result is a claim too. Accept "0 found" or "no contact" only from a reply whose
    keys are exactly those of a real good reply (copy them from a sample). Any other shape
    (`{"status": "error", "rows": []}`) is a failure.
  - Error messages name the tool, the keys and the counts, never values from the data: a reply
    quoted back can carry a customer's details into the model.
  - Fake each in the local test: renamed fields at both levels, failure replies in several
    shapes (including ones with no "error" word), an empty value, and a write that doesn't
    confirm.
- The agent's SKILL.md says: on `error`, fix only that field and run once more. **Never raise a
  limit** to get past an error.

### 3. Never let the platform cut a run

- Going past a tool's `max_calls`, or `limits.max_tool_calls`, **ends the run on the spot**.
  The script can't catch it, the run is marked blocked and its return value is lost: the paid
  calls already made are spent for nothing.
- `limits.max_tool_calls` can't exceed the environment's ceiling (platform maximum 1000; a new
  environment's policy allows 100). Over it, the run is refused with "exceeds current policy
  maximum N". Use that N. Each tool's `max_calls` must be ≤ `limits.max_tool_calls`.
- So the script keeps **its own** limit below **every** cap (each tool's `max_calls` and the
  total), and counts every retry. At its limit,
  or near the time budget, it writes what it has and returns `done: false` with `state`. The
  agent runs it again with that state.
- Stop paying when a tool keeps failing: after a few failures in a row, return an error (with
  `state`) instead of spending the rest of the budget on an outage. Don't retry a definite
  answer such as "not found".
- Don't pay twice. Look each distinct thing up once (dedupe before paying), write each part
  before moving the cursor, and keep parts small: a resumed run repeats every paid call made
  since the last written part. If a write fails, return the error **with the state from before
  that part**, so running again redoes just that part.
- A run gets about 300 seconds of wall clock, and up to 90 of those can be a cold start. Keep a
  time budget (the template uses 150 s) and leave room to deliver.
- Tools run at most 4 at a time by default. Bound any concurrency to the platform's number.
- An ordinary tool error *is* catchable: retry it, counting the retry against your own limit.
- Raising `max_calls` is not a fix for a cut run. The fix is a lower own limit and a resume.

### 4. One copy-exact run form

The agent's SKILL.md holds one fixed form, and the agent fills only the business inputs.
Tool names, `tool_scope`, `output_scope` and `limits` are never typed by the agent (mistyped
tool names get refused).

```json
{"request_id": "<the current UTC time, e.g. 2026-10-16T14:05:00Z>/<short-name>",
 "file": "<skill>/scripts/<name>.py", "files": [],
 "inputs": {"<business input>": "<...>", "file_name": "<descriptive name>"},
 "tool_scope": [{"tool": "<exact tool name>", "max_calls": 100, "constraints": {}}],
 "output_scope": {"workspace_prefix": "reports/<skill>", "max_files": 0, "deliver": false},
 "llm_scope": null, "limits": {"max_tool_calls": 100}}
```

- `request_id` is `<UTC time now>/<name>`. A **new** time on every run: the same id with the
  same request replays the earlier result without running, and with a changed request it is
  refused ("request_conflict"). Times older than 30 days or ahead of now are refused.
- Every `tool_scope` entry needs `"constraints": {}`, even when empty.
- Under the form, the SKILL.md says what to do with each result: `error` (fix that field, run
  once more), `done: false` (run again at once with the same form plus `"state"`, without
  asking), `done: true` (reply with the link and counts; never paste rows). `error` wins over
  everything else in the result; an `error` that carries `state` (a failed write) is run once
  more with that state, then reported if it fails again.

### 5. The script lives in the skill and runs by its path

- The script is a file in the skill's files, run with `run_code_file` and `file:
  "<skill>/scripts/<name>.py"`. The agent never pastes or rewrites it into
  `write_and_run_code`: a copy can drop the checks, costs tokens every time, and leaves no
  single tested version.
- To try a change without redeploying, pass `edits: [{"old": "...", "new": "..."}]` to
  `run_code_file`. Each `old` must appear exactly once. Then put the fix in the skill file.
- After you deploy the skill (`edit-skill`, then `deploy-to-dev`), run `sync_draft_test_chat`
  and start a new test conversation. Otherwise the test chat still runs the old script.

## Test in this order

1. **Locally, with fakes that keep the platform's rules.** Run
   `python3 <this skill>/assets/run_locally.py <script> <form.json> <fakes.py>`. It checks the form, then
   runs the script the way the sandbox does. The fakes refuse unlisted tools, end the run at a
   cap, fail a return over 64 KiB, and flag a return value that carries rows. Make the fakes
   behave like the real tools you know about (paging, errors, the destination's own rules).
   Test at least: bad input (zero calls), the biggest real job (resumes, never hits the cap),
   tool errors, a reply missing a field, and the empty case. Add one test that reads the form
   from the agent's SKILL.md, passes it to `check_form`, and fails if its `max_calls` and the
   script's own limits disagree: the two drift apart when only one is edited.
2. **One free real run** on dev (`call_agent_tool` with `run_code_file`): a bad input that
   must return the error, then the smallest real job.
3. **One chat test** (`test-draft-in-chat`) after `sync_draft_test_chat`, to see the agent fill
   the form and report the result.

Spend more only after these pass, and only with the person's yes. For a paid job, give the
script a free mode that counts the work and prices it, so the agent can quote the cost first.

## Small jobs

A small answer that *is* the point (three counts and a few ids) goes straight back in the
return value, under a few KB. Don't save a file for it. The other rules still hold: validate
first, copy-exact form, own limit.

## Common mistakes

| Thought | Reality |
|---|---|
| "Return the rows too, so the agent can check them" | Rows flood the context and can fail the run at 64 KiB. Check in the script and return the result. |
| "Save a backup CSV, it's harmless" | It is attached to the chat (deliver defaults to true) and its summary (~48 KB) enters the context. |
| "If the cap is hit, the script stops cleanly and still builds the Sheet" | The cap ends the run; nothing after it runs and the return is lost. Stop yourself first. |
| "Just raise max_calls" | The ceiling is often 100, and a bigger cap only moves the cut. Lower the own limit and resume. |
| "Call the tool and see if the region exists" | That's a call before validation. Check against a fixed list first. |
| "Fuzzy-match the typo and carry on" | A guessed value spends money on the wrong job. Suggest; let the agent re-run. |
| "Reuse the request_id for the resume" | The same id replays the old result. Use a new time on every run. |
| "Paste the script into write_and_run_code to skip a redeploy" | Use `edits` on `run_code_file` to try it, then update the skill. |
| "It passed my local test" | Only if the fakes keep the platform's rules. Then one free real run, then one chat test. |
| "print() the summary" | The result is only what the script returns: `return {...}` as the last line, or `async def main(inputs)` returning it. |

## Google Sheets example

`references/google-sheets.md`: why the first part of a list is an import and later parts
are written into exact rows, leading zeros in zips, text-only cells, and the 1,000-row grid of
a new Sheet.
