# Soleon Agent Toolkit — Tool Reference

The `soleon-agent-toolkit` MCP server exposes the Soleon platform's admin capabilities
as governed MCP tools. Tools are implemented server-side (agent-infra `lambda/mcp_server/`)
and discovered live by any connected MCP client — installing this plugin gives you every
tool the server publishes, automatically.

**Authentication:** every tool requires a Soleon Cognito bearer token, verified by the
server. Access follows platform roles (platform admins see everything; members see the
agents they hold a role on).

## Current tools (31)

### Agents

| Tool | What it does |
|---|---|
| `list_agents` | List the agents you can see in an application environment (dev/staging/prod). |
| `get_agent` | One agent's configuration summary + runtime identifiers. |
| `get_agent_config` | The agent's **deployed** soul (SOUL.md), config.json, and schedules from the deployed artifact. |
| `private_deploy_agent` | Create a private, owner-only agent from a validated request body. |
| `get_agent_draft` | Your per-user edit draft (document + concurrency token). |
| `update_agent_draft` | Stage changes in the draft — the ONLY content-edit path (conflict-protected; 409 returns the current draft to reconcile). |
| `deploy_agent_draft` | Deploy exactly what's staged in your draft to dev (no content inputs). *Arrives with #336.* |
| `promote_agent` | Promote one tier (dev→staging→prod), sequential + version-bound. *Arrives with #336.* |
| `delete_agent` | Delete an agent (cascades across promoted tiers). |
| `list_agent_collaborators` | Who holds a role on an agent. |

### Custom MCPs

| Tool | What it does |
|---|---|
| `list_custom_mcp_servers` | The user-defined custom MCP servers in an environment (name, status, version, draft presence). |
| `get_custom_mcp_server` | One server by slug: pointer metadata, draft overlay, published tool definitions. |
| `list_custom_mcp_connections` | The data-source connections (credentials never returned). |
| `update_custom_mcp_draft` | Save tool changes to a server's draft without publishing. |
| `publish_custom_mcp` | Publish a server's draft as the next version via the platform's publish pipeline. |
| `promote_custom_mcp` | Promote a server one tier (sequential + version-bound). *Arrives with #336.* |

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

## The draft-first workflow (business rule, 2026-07-16)

The toolkit never mutates a live agent directly — in any tier, including dev.

1. **Stage** — `update_agent_draft` (agents) / `update_custom_mcp_draft` (tool servers)
2. **Review** — `get_agent_draft` / `get_custom_mcp_server` (draft overlay)
3. **Go live on dev** — `deploy_agent_draft` / `publish_custom_mcp`
4. **Promote tiers** — `promote_agent` / `promote_custom_mcp` (dev → staging → prod,
   sequential-only, version-bound; per-stage platform permissions apply)

The former direct-edit tools (`update_agent_soul`, `update_agent_config`,
`update_agent_metadata`) were **deleted** — the draft flow is the only write path.
No plugin update is needed as tools change — clients discover the toolset live from
the server on every connection.

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
