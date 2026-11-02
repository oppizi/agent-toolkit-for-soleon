---
name: tool-switches
description: "Turns individual tools on or off in an agent's draft and sets whether each needs a person's approval. Use when: Pruning tools, or a tool must ask first."
---

# Switch single tools on or off, and set approval

Turns individual tools on or off in an agent's draft and sets whether each needs a person's approval.

**Use when:** Pruning tools, or a tool must ask first.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **Reading is safe; writing, sending, spending and deleting are not.** Tool servers come in a read half and a write half. Write approval is on by default when you attach a server; turning it off lets the agent act without asking. *Check:* What will it write, send, buy or delete, and which of those need a person's OK every time?
- **On a customer channel, "approval" asks the customer.** An approval prompt goes to whoever is in the conversation. In website chat or email that is the customer or prospect, not a colleague. *Check:* On this channel, who is in the conversation when it wants to act, and who should really approve?
- **Web and file tools are on unless someone turns them off.** Agents get system tools for the web and a file workspace. The web lets it read any page (including pages that try to instruct it); the file workspace can expose its own settings. *Check:* Does it need to browse the web or keep files? If not, switch them off.
- **Platform-wide tool settings change every agent.** A Tool Library tool's settings (approval, descriptions, which argument is filled from the channel) apply to every agent that uses it. *Check:* (Admin only) Is this a change for one agent (use its own switches) or for all?
- **Tools act as, and check, whoever is chatting.** Some tools run as the person in the conversation: a Slack tool posts as them, and a tool connected per person checks that person's access. *Check:* For each tool: whose account does it act as on this channel?
- Also check, if the profile says they apply: An Off switch isn't proof a tool can't run; Money needs a fresh yes every time; An agent with the file tool can read its own tests.

## Steps

1. get_agent_draft(slug, app_env="dev")
2. set_mcp_server_tools(slug, app_env="dev", server_id, kind, enable, disable) — many at once
3. set_agent_tool(slug, app_env="dev", tool_id, approval, enabled) — one half's switches; it can create a single half, so attach servers with attach-tool-server
4. set_agent_upstream_override(slug, app_env="dev", tool_id, upstream_tool, approval, enabled, type) — one tool inside a server
5. get_agent_draft(slug, app_env="dev") — read back

## Check it worked

Each switch reads back as set.

## Never on your own

Turn approval off on a tool that writes without asking. Rely on an Off switch alone for a tool that must never run: attach only servers whose every tool is acceptable.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `get_agent_draft`, `set_mcp_server_tools`, `set_agent_tool`, `set_agent_upstream_override`
