# MCP coverage — Wave 3 Business Center

Tracking doc for the tool-surface changes landing in Wave 3 (agent-infra `AHP-434`).
This file exists so the documentation in `TOOLS.md` is updated deliberately as each
tool ships, rather than drifting — which is precisely what happened before it: the
reference advertised **85** tools and four that had been deleted, while the server
published **81**.

## Why this work exists

Two audits of the agent-infra repo, both adversarially verified:

1. **A delta audit** of what Wave 3 shipped. It added 16 new Ideas routes and **zero**
   MCP tools, while deleting four — so the agent-facing surface shrank as the product grew.
2. **A whole-surface census** of the Business Center. Roughly **26 tools cover about a
   third** of the area's routes. Reads are broadly served; almost every write past
   "create an idea and comment on it" was UI-only. Two categories were at **zero**:
   Tasks and Pipeline.

The practical consequence: an agent can *read* the Business Center thoroughly and
*change* almost nothing.

## Documentation rule for this batch

Every ticket that adds or changes a tool updates **`TOOLS.md` in the same PR** — the
tool's row, its section, and the count in `## Current tools (N)`. A tool that ships
without its documentation row is not done. This doc is the checklist; `TOOLS.md` is
the published reference.

Note the count in `TOOLS.md` must match the server's `ALL_TOOLS_COUNT`
(agent-infra `tests/e2e/test_mcp_discovery_filtering.py`). Those two numbers
disagreeing is the drift this doc exists to prevent.

## Two corrections already made in this PR

- `## Current tools` corrected **85 → 81**.
- The four retired tools (`move_idea_to_preparation`, `get_pipeline`,
  `set_pipeline_stage`, `approve_idea`) moved out of the live tables into a
  **Recently removed** section, rather than deleted outright — a client that remembers
  them needs to know the capability moved rather than vanished.

## Planned tool changes

### Regressions Wave 3 caused — highest priority

| Ticket | Change | Docs impact |
|---|---|---|
| `AHP-491` | `create_idea` documents a contract the API rejects — it says `content` is optional while the route hard-requires `dataSources` and `appConnections` | Rewrite the `create_idea` description. The LLM-visible text is the defect. |
| `AHP-482` | Agent write-back keys drifted from the form (`solution` vs `approach`; no `expected_impact`) | Container-side tool, not in this reference — no `TOOLS.md` change expected. |

### Core capability gaps

| Ticket | Change | Docs impact |
|---|---|---|
| `AHP-492` | Stage-transition tool, `specApproved` on `update_idea`, stale OAuth consent copy | **New tool row.** Also removes the "Recently removed" caveat on `set_pipeline_stage` / `move_idea_to_preparation` once the capability returns. |
| `AHP-493` | Spec read/write | New tool rows (2). |
| `AHP-494` | Tasks CRUD | New section — Tasks is currently absent from this reference entirely. |
| `AHP-495` | Party-score writes + scorer roster | New tool rows. Note reads already leak via `list_ideas` / `get_idea`, so the docs should be explicit about which half exists. |

### Remaining Business Center gaps

| Ticket | Change | Docs impact |
|---|---|---|
| `AHP-504` | Ideas forum: votes, reactions, comment edit/delete/vote/react | New tool rows. |
| `AHP-505` | Idea delete + link/unlink | New tool rows. Document the detach asymmetry: the shell is deleted only when this idea minted it and it never launched. |
| `AHP-506` | Priority board read + re-rank | New tool rows. Must document that tiers are **user-extensible** — not fixed at `p0`–`p4`. |
| `AHP-507` | Ideas settings read/write | New tool rows. Worth documenting the `ideasAgentMissing` self-heal case. |
| `AHP-508` | Discovery material writes (6 routes) | New tool rows. Document the request-scope vs pool-scope distinction. |
| `AHP-509` | Form authoring, interviewer pool, request delete | New tool rows. Document that a `form`-modality request needs questions authored before it is usable. |
| `AHP-510` | Subject library + coverage analysis | New tool rows. Note the current asymmetry: gap analysis exists, coverage analysis does not. |
| `AHP-511` | Agent Discussion edit/delete/react | New tool rows. |

## Areas deliberately NOT covered

This batch is **Business Center only**, by owner decision. A platform-wide census —
Agents, Channels, Evals, Knowledge, Tools — is deferred to Wave 4. Gaps outside the
Business Center are therefore expected and are not tracked here.

## Known limitation of this doc

The census measured route coverage, which is a proxy for capability coverage. A route
with a tool is not automatically *well* covered — several tools enforce field
allowlists narrower than the route accepts (`update_idea` lifts exactly five fields).
Those are recorded as partial coverage in the tickets, not as gaps, and this doc does
not attempt to enumerate every one.
