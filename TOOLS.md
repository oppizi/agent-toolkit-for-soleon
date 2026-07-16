# Soleon Agent Toolkit — Tool Reference

The `soleon-agent-toolkit` MCP server exposes the Soleon platform's admin capabilities
as governed MCP tools. Tools are implemented server-side (agent-infra `lambda/mcp_server/`)
and discovered live by any connected MCP client — installing this plugin gives you every
tool the server publishes, automatically.

**Authentication:** every tool requires a Soleon Cognito bearer token, verified by the
server. Access follows platform roles (platform admins see everything; members see the
agents they hold a role on).

## Current tools (28)

### Agents

| Tool | What it does |
|---|---|
| `list_agents` | List the agents you can see in an application environment (dev/staging/prod). |
| `get_agent` | One agent's configuration summary + runtime identifiers. |
| `get_agent_config` | The agent's **deployed** soul (SOUL.md), config.json, and schedules from the deployed artifact. |
| `private_deploy_agent` | Create a private, owner-only agent from a validated request body. |
| `update_agent_soul` | Update an agent's soul through the platform's validators (live mutation). |
| `update_agent_config` | Update soul/config/skills with optimistic concurrency (live mutation, dev tier only). |
| `update_agent_metadata` | Update display metadata (live mutation). |
| `delete_agent` | Delete an agent (cascades across promoted tiers). |
| `list_agent_collaborators` | Who holds a role on an agent. |

### Custom MCPs

| Tool | What it does |
|---|---|
| `list_custom_mcp_servers` | The user-defined custom MCP servers in an environment (name, status, version, draft presence). |
| `get_custom_mcp_server` | One server by slug: pointer metadata, draft overlay, published tool definitions. |
| `list_custom_mcp_connections` | The data-source connections (credentials never returned). |
| `publish_custom_mcp` | Publish a server's draft as the next version via the platform's publish pipeline. |

### Channels

| Tool | What it does |
|---|---|
| `list_channel_catalogue` | The channel types the platform supports. |
| `list_channel_instances` | Channel instances (Slack workspaces, TalkJS apps, …). |
| `get_channel_instance` | One instance by its `ci_…` id. |
| `bind_agent_channel` | Bind an agent to a channel instance. |
| `unbind_agent_channel` | Unbind an agent from a channel instance. |

### Knowledge

| Tool | What it does |
|---|---|
| `list_knowledge_bases` | The knowledge bases in an environment. |
| `get_knowledge_base` | One knowledge base's details. |

### Observability & evals

| Tool | What it does |
|---|---|
| `get_eval_scoreboard` | An agent's eval scoreboard. |
| `list_scored_sessions` | Sessions scored by the online eval pipeline. |
| `get_session_eval_detail` | Per-criterion detail for one scored session. |
| `list_trace_sessions` | Trace sessions for an agent. |
| `get_trace_session` | One trace session end-to-end. |
| `list_failures` | Platform failure records. |
| `get_usage_summary` | Usage/cost summary. |

### Governance

| Tool | What it does |
|---|---|
| `list_role_change_audit` | The role-change audit trail. |

## Pending tools — awaiting publish in Soleon

These complete the **draft-first workflow** (see change in agent-infra: branch
`mcp-draft-tools`): edit safely in a draft, review the diff, publish deliberately —
instead of mutating live agents or falling back to direct admin-API calls.

| Tool | What it will do | Status |
|---|---|---|
| `update_custom_mcp_draft` | Save changes to a custom MCP server's draft **without publishing** (complement to `publish_custom_mcp`). | built + tested, awaiting PR → dev deploy |
| `get_agent_draft` | Fetch the caller's edit draft for an agent (per-user), with the concurrency token for safe updates. | built + tested, awaiting PR → dev deploy |
| `update_agent_draft` | Save an agent edit draft **without deploying** (complement to `update_agent_config`), with conflict protection. | built + tested, awaiting PR → dev deploy |

No plugin update is needed when these ship — MCP clients discover tools live from the
server on every connection.

## Known gaps / wishlist

- `list_custom_mcp_connections` returns empty in all environments even when connections
  exist, and `get_custom_mcp_server` reports base (dev) connection ids without applying
  per-environment overrides — a prod audit can wrongly show prod pointing at dev
  connections. Raised with the agent-infra team.
- Agent draft-vs-deployed **diff** currently requires fetching both
  (`get_agent_draft` + `get_agent_config`) and comparing client-side.
- The plugin's server URL is fixed in `plugin/.mcp.json` — making the target
  environment (dev vs prod system) configurable at install time would remove a
  hand-edit.
