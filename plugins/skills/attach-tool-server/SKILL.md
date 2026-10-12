---
name: attach-tool-server
description: "Attaches a Tool Library integration or a custom tool server to an agent's draft on dev, with only the tools it needs. Use when: An agent needs to read or act in another system."
---

# Give an agent a tool server

Attaches a Tool Library integration or a custom tool server to an agent's draft on dev, with only the tools it needs.

**Use when:** An agent needs to read or act in another system.

## Before you start

- Read the agent's profile (`agent-profile.md`, from the `start-an-agent` skill). No profile: run `start-an-agent` first.

- **Every user gets a private session with no shared memory.** Each person who opens the agent gets their own chat history and files. The agent doesn't know what other people did with it. *Check:* Will several people use it at the same time? What does it create, change or buy in shared systems?
- **Reading is safe; writing, sending, spending and deleting are not.** Tool servers come in a read half and a write half. Write approval is on by default when you attach a server; turning it off lets the agent act without asking. *Check:* What will it write, send, buy or delete, and which of those need a person's OK every time?
- **On a customer channel, "approval" asks the customer.** An approval prompt goes to whoever is in the conversation. In website chat or email that is the customer or prospect, not a colleague. *Check:* On this channel, who is in the conversation when it wants to act, and who should really approve?
- **"Dev" doesn't mean fake data.** Connections (API keys, databases, CRM logins) are shared by dev, staging and production. A dev agent with the CRM connection writes to the real CRM. *Check:* Which systems does it touch, and is there a test copy for dev and staging?
- **Sending from the company domain can get it blacklisted.** Outbound email from the main company domain, or look-alike domains in the same account, puts the whole company's email reputation at risk. *Check:* Which address and domain does it send from, and how many emails a day?
- Also check, if the profile says they apply: Content it reads can carry instructions; Tools act as, and check, whoever is chatting; An Off switch isn't proof a tool can't run; Money needs a fresh yes every time; A tool can hand the agent data its audience mustn't see; A custom tool server needs both halves attached.

## Steps

1. attach_mcp_server(slug, app_env="dev", server_id, kind="mcp" | "custom", subagent=true, write_approval=true) — adds both halves (read and write); a custom server with only one half attached exposes no tools and no error
2. set_mcp_server_tools(slug, app_env="dev", server_id, kind, disable=[tools it doesn't need])
3. get_agent_draft(slug, app_env="dev") — read back both halves and the switches

## Check it worked

Both halves and the enabled tools read back, with approval on for writes.

## Never on your own

Switch approval off for tools that send, spend money or change customer records without asking.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `attach_mcp_server`, `set_mcp_server_tools`, `get_agent_draft`
