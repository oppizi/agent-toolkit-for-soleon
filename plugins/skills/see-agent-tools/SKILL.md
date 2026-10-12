---
name: see-agent-tools
description: "Lists the tools a Soleon agent has, what each tool server contains, and the platform-wide settings for a tool. Use when: 'Can the agent do X?', or before changing tools."
---

# See an agent's tools and tool servers

Lists the tools a Soleon agent has, what each tool server contains, and the platform-wide settings for a tool.

**Use when:** "Can the agent do X?", or before changing tools.

## Before you start

- **Web and file tools are on unless someone turns them off.** Agents get system tools for the web and a file workspace. The web lets it read any page (including pages that try to instruct it); the file workspace can expose its own settings. *Check:* Does it need to browse the web or keep files? If not, switch them off.
- **A shared tool server changes every agent that uses it.** Several agents can use the same custom tool server or Tool Library integration. Editing, publishing or promoting it changes all of them. *Check:* Which other agents use this tool server, and should they change too?
- **Platform-wide tool settings change every agent.** A Tool Library tool's settings (approval, descriptions, which argument is filled from the channel) apply to every agent that uses it. *Check:* (Admin only) Is this a change for one agent (use its own switches) or for all?
- **Tool servers go up before the agent.** An agent promoted to production that uses a tool server not yet on production calls a server that isn't there. *Check:* Promote every tool server the agent uses first, then check it exists there.
- **An agent with the file tool can read its own tests.** The agent's settings file sits in its file workspace, and it holds every test conversation and the judge's scoring rules. An agent with the file tool can read them. *Check:* Switch the file tool off unless the job needs it; never put the ideal answer in a test's input.

## Steps

1. get_agent_document(slug, app_env, include="effective") — read only effective.tools. Ids: custom_<server>_read/_write → custom tool server <server>; mcp_<id>_… → Tool Library integration <id>; kb_<kb-slug>_read → knowledge base; sys_* → built-in web and file tools. {"enabled": false} under upstreamOverrides means that tool is off for this agent
2. list_custom_mcp_servers(app_env) — tool names and platform settings for every custom server; usually enough
3. get_custom_mcp_server(slug=<server>, app_env) — only if you need a tool's inputs; about 80 KB with code
4. get_tool_policy(integration_id, app_env) — integration_id is "custom_mcp:<server>", a catalogue id ("gmail", "strapi") or "kb:<kb-slug>", never the agent's tool id; "updatedAt": null usually means the id was wrong

## Check it worked

Read-only. Check the server exists in the environment you care about; a test there can quietly fall back to dev.

## Never on your own

Paste get_custom_mcp_server output: tool code can contain an Authorization header.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent_document`, `list_custom_mcp_servers`, `get_custom_mcp_server`, `get_tool_policy`
