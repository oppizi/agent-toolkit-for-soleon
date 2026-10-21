# Platform facts behind the rules

Each fact was read in the platform source (agent-infra `origin/dev` at 78e08a7cc, 16 Oct 2026).
The same lines are on `staging` and `main` unless noted. `ce/` is
`containers/shared/code_execution/`. If a fact here and `get_sandbox_script_rules` disagree,
the live tool wins: tell the toolkit maintainers.

## How a script runs

| Fact | Source |
|---|---|
| `run_code_file` takes `file` (`<skill>/<path>.py` or a saved `scr_…` id) and optional `edits`; the file's exact bytes run, changed only by the edits | `ce/manifest.py:1109-1130` |
| Each edit is `{"old", "new"}`, `old` must appear exactly once, at most 20 edits | `ce/saved_scripts.py:37`, `:55-73` |
| The tool tells the agent to read the skill's SKILL.md for the inputs, files, tool_scope, llm_scope, output_scope and limits, and declare them | `containers/shared/mcp_server.py:4913-4925` |
| A plain script returns its result with a top-level `return` (last line); a script that only defines `async def main(inputs)` has `main` awaited once | `ce/runner.py:39-85`, `ce/instructions.py:46-51` |
| `tools`, `llm` and `execution` are globals in the script | `ce/runner.py:96-97` |
| A line after a top-level `return`, or a bare last-line expression, is refused before running | `ce/manifest.py:1484-1503` |
| `get_sandbox_script_rules` returns the script shape, SDK signatures, an example script and request, the rules and the packages | `lambda/mcp_server/core/interface_adapters/sandbox_script_rules.py:19-28` |

## Data and the model's context

| Fact | Source |
|---|---|
| The return value must be JSON of at most 64 KiB; over that the run fails ("Result exceeds the 64 KiB limit") | `ce/runner.py:102-103`, `ce/service.py:499-503` |
| `output_scope.deliver` defaults to true: published files are attached to the chat; `false` skips delivery | `ce/schema.json:240-242`, `ce/service.py:333-334`, `ce/delivery.py:9-39` |
| A saved CSV comes back to the model as a summary: up to 128 rows or 24,000 bytes of rows (cells cut at 80 characters) plus up to 24,000 bytes of column counts; columns with over 128 values, or a value over 80 characters, get no counts | `ce/csv_validation.py:62-94`, `ce/handlers.py:22-24` |
| A tool result over about 1 MiB is refused to the script as `result_too_large` | `ce/dispatch.py:179` |

## Limits

| Fact | Source |
|---|---|
| Every `tool_scope` entry requires `tool`, `constraints` and `max_calls` (1–1000) | `ce/schema.json:146-198` |
| A tool name that isn't the agent's, or is listed twice, is refused: "Tool is unavailable, forbidden, or duplicated" (`scope_violation`) | `ce/manifest.py:1558-1559` |
| A tool's `max_calls` above `limits.max_tool_calls` is refused: "Per-tool grant exceeds run call budget" | `ce/manifest.py:1260-1261` |
| `limits.max_tool_calls` is capped by the environment's policy ceiling: "Requested limits.max_tool_calls=N exceeds current policy maximum M" | `ce/manifest.py:1527-1532` |
| Platform maximum 1000; a new environment's policy: default 20, ceiling 100 | `ce/schema.json:648-679` |
| Past a cap: "Tool call budget reached: … the script tried call N+1" | `ce/ledger.py:263-272` |
| That error is not handed to the script: dispatch closes, the run re-raises it and is marked `blocked`; the return value is lost; files saved before it stay | `ce/runtime.py:144-146`, `:454-455`, `ce/service.py:486-491`, `:508` |
| An ordinary tool failure reaches the script as a catchable `ExecutionError` with `.code` and `.message` | `ce/dispatch.py:164-171`, `ce/guest_sdk.py:21`, `:55` |
| Tools run at most 4 at once by default ("Parallel tool call limit reached") | `ce/schema.json:668`, `ce/ledger.py:274-282`, `ce/manifest.py:1543-1545` |
| Wall clock 300 s by default (policy ceiling 900); the agent is told up to 90 s of it can be a cold start | `ce/schema.json:664`, `:676`, `ce/watchdog.py:86-87`, `ce/instructions.py:284-285` |
| `execution.checkpoint(value)` keeps one JSON value (≤64 KB) with the run; it comes back to the model as `partial_result` if the run fails. No SDK call reads it back | `ce/checkpoints.py:14`, `:105-112`, `ce/retained.py:135`, `ce/guest_sdk.py:343-344` |
| Approval for approval-gated tools in `tool_scope` is asked once, before the run | `ce/service.py:186-192`, `ce/catalogue.py:64` |

## request_id

| Fact | Source |
|---|---|
| Pattern `YYYY-MM-DDTHH:MM:SSZ/<name>` (`<name>`: letters, digits, `_ . : -`), at most 200 characters | `ce/schema.json:20-25`, `ce/store.py:48-57` |
| Its time must be within the last 30 days and at most 300 s ahead, or "request_conflict" | `ce/store.py:58-71`, `ce/manifest.py:1456-1470` |
| The same id with the same request returns the stored result (no new run); with a different code or scope: "Request ID already belongs to different code or scope" | `ce/store.py:85-95`, `:153-155` |
| On dev only (not yet staging or main), a missing id is minted as `<time>/auto-<hash>` | `ce/request_ids.py:1-38` |

## Deploying and testing

| Fact | Source |
|---|---|
| `sync_draft_test_chat` puts the draft (SKILL.md bodies and the skill's files) into the test chat and resets its session, so the next message runs it | `lambda/mcp_server/core/interface_adapters/admin_forward_handlers.py:2200-2205`, `lambda/ui_admin_agents/index.py:11166-11173` |

Proven in use (lead-finder, case T-208, 15–16 Oct 2026): leaving out `"constraints": {}` was
refused; a run cut by the cap lost 9 paid searches; skipping `sync_draft_test_chat` after a
deploy ran the old script.
