# Soleon Agent Toolkit — Tool Reference

The `soleon-agent-toolkit` MCP server exposes the Soleon platform's admin capabilities
as governed MCP tools. Tools are implemented server-side (agent-infra `lambda/mcp_server/`)
and discovered live by any connected MCP client — installing this plugin gives you every
tool the server publishes, automatically.

**Authentication:** every tool requires a Soleon Cognito bearer token, verified by the
server. Access follows platform roles (platform admins see everything; members see the
agents they hold a role on).

## Current tools (85)

### Agents

| Tool | What it does |
|---|---|
| `list_agents` | List the agents you can see in an application environment (admins see every agent; members the agents they hold a role on). |
| `get_agent` | One agent's configuration summary by slug, plus runtime identifiers (runtimeArn, endpointId, appId, secretName). |
| `get_agent_config` | The agent's **deployed** soul (SOUL.md), config.json, and schedules from the deployed S3 artifact. |
| `private_deploy_agent` | Create a private, owner-only agent from a validated request body the deploy-agent plugin produced (dev tier only). |
| `get_agent_draft` | Fetch your per-user edit draft for an agent, plus the `draftEtag` concurrency token. |
| `update_agent_draft` | Save your edit draft **without deploying** (dev tier), with `expected_updated_at` conflict protection. |
| `delete_agent_draft` | Discard your edit draft — yours only; the deployed config is untouched. |
| `deploy_agent_draft` | Deploy your staged draft to the dev tier through the full deploy pipeline; clears the draft on success. |
| `sync_draft_test_chat` | Materialize your draft into the test-chat sandbox and reset its session, so the next test message runs it. |
| `promote_agent` | Promote an agent's config one tier (dev→staging or staging→prod) — sequential-only, version-bound. |
| `update_agent_profile` | Update presentation metadata — avatar icon/color, description, visibility — with no deploy (dev tier, If-Match CAS). |
| `delete_agent` | Delete an agent (cascades to promoted tiers; removes Slack app, secrets, config, registry rows). Idempotent. |
| `list_agent_collaborators` | The Cognito role grants on an agent: sub, email, role, grantedBy, grantedAt. |

### Channels

| Tool | What it does |
|---|---|
| `list_channel_catalogue` | The channel-type catalogue (slack, talkjs, …) with credential schemas and webhook URL templates. |
| `list_channel_instances` | The platform's channel instances with probe status (credential values never returned). |
| `get_channel_instance` | One channel instance by its `ci_…` id: name, type, probe status, workspace/app identity. |
| `bind_agent_channel` | Bind an agent to a channel instance (deploy_mode passive or instant). |
| `unbind_agent_channel` | Unbind an agent from a channel instance (deploy_mode passive or instant). |

### Custom MCPs

| Tool | What it does |
|---|---|
| `list_custom_mcp_servers` | The user-defined custom MCP servers in an environment: name, status, version, draft presence, tool counts. |
| `get_custom_mcp_server` | One server by slug: pointer metadata, draft overlay, published tool definitions, per-env overrides. |
| `list_custom_mcp_connections` | The data-source connections in an environment (credentials never returned — auth type only). |
| `create_custom_mcp_server` | Create a new empty, inactive tool server (slug is the permanent identity). |
| `clone_custom_mcp_server` | Duplicate a server into a new unpublished one — tools AND governance travel with the clone. |
| `update_custom_mcp_draft` | Save changes to a server's draft **without publishing** (complement to `publish_custom_mcp`). |
| `generate_tool_code` | Generate a tool's Python `execute()` body from a natural-language prompt (modes: regenerate / update). |
| `test_custom_mcp_tool` | Execute one tool against the live test runtime (draft tools included); write-capable tools require `confirmed_writes=true`. |
| `diagnose_custom_mcp_failure` | Diagnose a failing tool test — pass the errored `test_custom_mcp_tool` response body verbatim. |
| `publish_custom_mcp` | Publish a server's draft as the next version (interface gate, GitHub commit, cache bump). |
| `promote_custom_mcp` | Promote a server one tier (dev→staging or staging→prod) — sequential-only, version-bound. |
| `set_custom_mcp_lifecycle` | Activate or deactivate a server in one environment (deactivation deletes nothing; deletion stays UI-only). |

### Knowledge

| Tool | What it does |
|---|---|
| `list_knowledge_bases` | The knowledge bases in an environment (metadata only). |
| `get_knowledge_base` | One knowledge base's metadata by kb id. |
| `create_knowledge_base` | Create a new empty KB scaffold (the slug names the agent-facing `kb_{slug}_*` tools). |
| `list_kb_pages` | A KB's wiki pages: path, title, kind, last modified, contributing sources (optional knowledge-graph fact counts). |
| `get_kb_page` | Read one wiki page: markdown body + etag (optionally the page's knowledge-graph facts). |
| `put_kb_page` | Write one wiki page (full replacement) with `expected_etag` stale-write protection. |
| `add_kb_source` | Add a source (url / text / mcp / file) to a KB and queue its ingestion. |
| `get_kb_source` | Read one source's config row — plus, behind flags, the raw content and the extraction log feed. |
| `manage_kb_source` | Patch a source's config or schedule and/or re-run its ingestion (action-free — the fields you pass decide). |
| `manage_kb_watch` | Manage polled S3 bucket-watch sources: list / create / update / run_now (deletion stays UI-only). |
| `kb_lint` | Drive the KB quality loop: run a lint pass, poll its state, and accept / reject / edit findings and recommendations. |
| `kb_versions` | Read a KB's change history; with `ts` + `path`, the three-way content view for one page (read-only — restore is UI-only). |

### Observability & evals

| Tool | What it does |
|---|---|
| `list_trace_sessions` | List trace sessions (newest first, cursor-paginated), filterable by agent slug or user namespace. |
| `get_trace_session` | One trace session end-to-end: META plus its turns and steps. |
| `list_failures` | Recent failure events for an environment, optionally filtered by agent slug. |
| `get_usage_summary` | Aggregate usage over the last N days: sessions, tokens, durations, per-agent/channel splits, daily buckets. |
| `list_scored_sessions` | An agent's LLM-judge eval scores grouped by session (newest first, cursor-paginated). |
| `get_session_eval_detail` | The eval score rows for one trace session, grouped by (level, item). |
| `get_eval_scoreboard` | An agent's aggregate eval scoreboard over the last N days: headline score, counts, issue tags, judge budget. |
| `run_eval` | Start an eval run — mode single or side_by_side — against draft / deployed / history version refs. |
| `get_eval_run` | One eval run's full detail by runId; poll while status is `running`. |
| `list_eval_runs` | An agent's eval runs, newest first, with side tallies and the trend block. |
| `get_agent_skills` | One agent's deployed skills: id, enabled state, package files (optionally full SKILL.md content). |
| `list_versions` | The published version lineage of an agent or custom MCP server, plus what each environment currently runs. |
| `get_version_diff` | Compare two versions — refs are versionIds or env selectors (e.g. from `prod` to `dev`); summary or full patches. |

### Governance

| Tool | What it does |
|---|---|
| `list_role_change_audit` | The platform's role-change audit trail (newest first, cursor-paginated). |

### Business Center — Ideas & Pipeline

| Tool | What it does |
|---|---|
| `list_ideas` | The Ideas board: title, department, status, submitter, the 3 agent-scored metrics, votes, comments — score-rank sorted. |
| `get_idea` | One idea in full: content fields, scores with provenance, status, linked agent, votes, comments, reactions. |
| `create_idea` | Submit a new idea (title required; content is the free-text interview brief; scores cannot be supplied). |
| `update_idea` | Edit an idea — content fields for author/admin; status / priority / scores overrides are admin-only. |
| `list_idea_comments` | An idea's discussion comments (oldest-first) with reply threading and vote counts. |
| `add_idea_comment` | Add a comment or threaded reply; the forum locks (409) once the idea moves to preparation. |
| `approve_idea` | Approve an idea — decision only, the idea stays on the board. Admin-only. |
| `move_idea_to_preparation` | Promote an idea into the build pipeline: mints an in-build agent shell seeded with the brief + scores. Admin-only. |
| `get_pipeline` | The agent build-pipeline board: every not-yet-launched agent grouped by the 6 build stages. |
| `set_pipeline_stage` | Set an agent's build-pipeline stage (one of the 6 stages, or terminal `Launched`). Admin-only. |

### Business Center — Agent management & Wiki

| Tool | What it does |
|---|---|
| `list_agent_thread` | An agent's discussion-thread comments (oldest-first) — the thread that takes over when an idea's forum locks. |
| `add_agent_thread_comment` | Append a comment to an agent's discussion thread (flat — no reply nesting). |
| `get_overview_visibility` | The manager-curated Business Center Overview visibility overrides (agent slug → shown \| hidden). |
| `set_overview_visibility` | Set one agent's Overview visibility override (all other overrides survive). |
| `get_wiki` | An agent's wiki: the ordered section layout, every page's metadata, and the nested page tree (no bodies). |
| `get_wiki_page` | One wiki page in full: bodyMarkdown plus title, placement, and provenance. |
| `put_wiki_page` | Create (title + section_id) or edit (page_id) a wiki page; the body is a full replacement. |
| `delete_wiki_page` | Delete a wiki page and all its descendant sub-pages (soft-delete; re-create with `put_wiki_page` if needed). |

### Business Center — Discovery

| Tool | What it does |
|---|---|
| `list_discovery_requests` | Discovery requests (owner-side interview/context collection): person, agent, modality, status, injected context. |
| `get_discovery_request` | One request's full tree in one call: META, collected materials, gaps, interview summary, form. |
| `create_discovery_request` | Ask a person for knowledge an agent needs — modality conversation (AI interview) or form; optional owner steering. |
| `update_discovery_request` | Edit a request's status, interview-chat binding, or injected context/gaps (identity fields are immutable). |
| `manage_discovery_gap` | Manage a request's knowledge gaps: add / update / delete / expand / analyze (async AI gap analysis). |
| `list_discovery_people` | The env-neutral Discovery people directory, with Cognito candidate matching and email duplicate-check. |
| `manage_discovery_person` | Create / update / delete a directory person (an existing email reuses that person; delete removes the discovery profile only). |
| `list_stakeholders` | An agent's discovery stakeholders — directory people bound to the agent as knowledge sources. |
| `manage_stakeholder` | Bind (`add`, idempotent) or unbind (`remove`) a stakeholder on an agent — the person themself is untouched. |
| `list_discovery_materials` | Materials (links, notes, files, photos) for one request or an agent's generic pool — metadata only, no file bytes. |
| `get_material_download_link` | Mint a short-lived (15-minute) presigned download URL for one stored material file. |

## Known gaps / wishlist

- Agent **draft-vs-deployed diff** still requires fetching both (`get_agent_draft` +
  `get_agent_config`) and comparing client-side — `get_version_diff` compares published
  versions and environments, but does not accept a draft ref.
- Several surfaces deliberately stay UI-only for now: KB restore and watch deletion,
  custom-MCP server deletion, Discovery request deletion / material uploads / forms /
  invite minting, and the pipeline's launch flow that flips an in-build shell live.
