---
name: start-an-agent
description: "Asks the questions that decide how a new Soleon agent must be built (what type it is, where it lives, who reaches it, what it can change or spend, what it must never do) and saves the answers as the agent's profile. Use when: Before building a new Soleon agent, or before a big change to an existing one. Every build skill reads the profile first and sends you here if there isn't one."
---

# Start an agent: the intake questions

Asks the questions that decide how a new Soleon agent must be built (what type it is, where it lives, who reaches it, what it can change or spend, what it must never do) and saves the answers as the agent's profile.

**Use when:** Before building a new Soleon agent, or before a big change to an existing one. Every build skill reads the profile first and sends you here if there isn't one.

## The six types of agent

| Type | Who uses it | Where it lives | Who approves its actions | What goes wrong most |
|---|---|---|---|---|
| Personal assistant | One staff member, in Soleon chat. | Soleon chat; private to its owner. | The owner, in the chat. | Acts on the owner's accounts; nobody else sees what it did. |
| Team tool | Several staff, each in their own private session. | Soleon chat, sometimes a Slack channel. | Whoever is using it at that moment. | Sessions don't share memory, so two people can do the same work twice, pay twice or overwrite each other. |
| Customer-facing | Customers or the public: website chat, in-app chat, email, WhatsApp. | A public channel, bound per environment. | The customer is the one in the conversation, so an approval prompt asks them. | Promises, personal data, tone, handoffs to staff, visitors trying to trick it. |
| Outbound | Prospects who never asked to hear from it. | An email or outreach channel (lemlist, a mailbox). | Nobody is watching each send unless approval is designed in. | Sending from the company domain, spam lists, consent, duplicate outreach, money. |
| Scheduled / background | No one in the moment: it runs on a timer or an event. | Automations; output to a channel or a system. | Nobody at run time. | Time zones, runs that fire once per user, failures nobody sees, runs that post to real people. |
| Part of a team of agents | Other agents, through shared databases or tools. | Several agents, each with its own settings. | Each agent's own rules. | One agent's change breaks another's input; shared pieces change for all of them. |

## Steps

1. Say which Soleon system the plugin is connected to (the server URL in the plugin settings): real production agents live on the production system
2. list_agents(app_env="prod") and list_agents(app_env="dev") — is there already an agent for this job, or one to copy? Its channels field shows where each already answers
3. list_ideas() — is there already a ticket?
4. Admins only: list_custom_mcp_servers(app_env) and list_knowledge_bases(app_env="dev") — pieces it could reuse, and would then share
5. Show what you found, propose the agent type from the table, then ask the questions one group at a time in plain words; record "not sure yet" rather than guessing
6. Save the answers as agent-profile.md in the working folder
7. Show the person the profile and only the risks that apply to its type
8. Next: a new agent from a local .claude/agents file → the plugin's deploy-agent skill; changes to an existing agent → the edit skills, which read this profile first

## The questions

Ask one group at a time, in plain words. Record "not sure yet" rather than guessing.

**The job**

- What job does it do, in one sentence, and who asked for it?
- Is there already an agent or ticket for this job, or one close enough to extend?
- Is it a copy of another agent, or will it be copied?
- (Propose the type from the table and ask the person to confirm it.) Which type is it: personal assistant, team tool, customer-facing, outbound, scheduled, or part of a team of agents?
- What language, country and currency does it serve, and where is the one true source for its prices and facts?
- Does it need to hand work to another agent, and through what?

**Where it lives and who reaches it**

- Where will people talk to it (Soleon chat, Slack, website or in-app chat, email), and who exactly are they?
- Will people outside the company write to it?
- Does another agent already answer on that channel? Which messages on it are this agent's?
- What will it know about the person on that channel (name, email)?
- Which environment should people reach first, and when does it move to production?
- Who should be able to see it, use it and change it in Soleon?

**What it can do**

- Will several people use it at once? What shared systems does it write to?
- What will it read, write, send, buy or delete, and as whose account or from which email address?
- For each write, send or purchase: who approves it, given who is in the conversation?
- Is there a test copy of each system for dev and staging?
- Which tool servers and knowledge bases will it share with other agents?

**What it says and sees**

- What personal or sensitive data will it see, and what must it never say or reveal?
- What may it promise, and what must it always pass to a person?
- Which rules must hold in every conversation, whatever is asked?
- Should it remember earlier conversations with the same person?

**Time and people**

- Does anything depend on time, date, time zone, business hours or holidays? Does it run on a schedule?
- When it can't help, who takes over, how, and during which hours?

**Cost and proof**

- How many conversations a day, and what monthly cost is acceptable?
- Which 3 to 5 behaviours prove it works, and when is its first real use?

## The profile (save as agent-profile.md)

```markdown
# Agent profile: <display name> (<slug>)
Date: <YYYY-MM-DD> · Confirmed by: <person>

## The job
- What job does it do, in one sentence, and who asked for it? → 
- Is there already an agent or ticket for this job, or one close enough to extend? → 
- Is it a copy of another agent, or will it be copied? → 
- (Propose the type from the table and ask the person to confirm it.) Which type is it: personal assistant, team tool, customer-facing, outbound, scheduled, or part of a team of agents? → 
- What language, country and currency does it serve, and where is the one true source for its prices and facts? → 
- Does it need to hand work to another agent, and through what? → 

## Where it lives and who reaches it
- Where will people talk to it (Soleon chat, Slack, website or in-app chat, email), and who exactly are they? → 
- Will people outside the company write to it? → 
- Does another agent already answer on that channel? Which messages on it are this agent's? → 
- What will it know about the person on that channel (name, email)? → 
- Which environment should people reach first, and when does it move to production? → 
- Who should be able to see it, use it and change it in Soleon? → 

## What it can do
- Will several people use it at once? What shared systems does it write to? → 
- What will it read, write, send, buy or delete, and as whose account or from which email address? → 
- For each write, send or purchase: who approves it, given who is in the conversation? → 
- Is there a test copy of each system for dev and staging? → 
- Which tool servers and knowledge bases will it share with other agents? → 

## What it says and sees
- What personal or sensitive data will it see, and what must it never say or reveal? → 
- What may it promise, and what must it always pass to a person? → 
- Which rules must hold in every conversation, whatever is asked? → 
- Should it remember earlier conversations with the same person? → 

## Time and people
- Does anything depend on time, date, time zone, business hours or holidays? Does it run on a schedule? → 
- When it can't help, who takes over, how, and during which hours? → 

## Cost and proof
- How many conversations a day, and what monthly cost is acceptable? → 
- Which 3 to 5 behaviours prove it works, and when is its first real use? → 

## Risks this raises
- <each nuance below that applies to this type, one line each>
```

## Risks to raise, by answer

Show the person only the ones that apply to their answers and the agent's type:

- The type of agent decides almost everything else; Check for an existing agent first; The channel decides the audience; Who can see and open the agent in Soleon; Every user gets a private session with no shared memory; Reading is safe; writing, sending, spending and deleting are not; Sending from the company domain can get it blacklisted; Guardrails are off until someone switches them on; What it says, the company must honour; The agent has no time zone of its own; Someone must be there when it hands over; The model and the budgets set the bill; A private agent refuses everyone outside the company; The channel may not tell the agent who it's talking to; Money needs a fresh yes every time; Long-term memory can act on old conversations; A fix in one agent doesn't reach its copies; A copied agent carries the original's facts; Soleon "subagents" are helpers inside one agent.

## Check it worked

The profile has an answer (or "not sure yet") for every question, and the person has read it back.

## Never on your own

Guess an answer, or start building before the person has confirmed the profile. Once the agent exists, posting the profile on its wiki (get_wiki(agent) → put_wiki_page(agent, title="Agent profile", section_id)) is visible to colleagues: ask first.


## Always
- Say which Soleon system the plugin is connected to (its server URL setting). Real production agents live on the production system; "prod" on the dev system is not production.
- Pass `app_env` wherever a tool takes it (`dev`, `staging`, `prod`); most default quietly to `dev`. Drafts exist on dev only. Knowledge-base reads take no `app_env`.
- Read before you write, and read back after. A success reply doesn't prove the change landed.
- Some changes are live at once: knowledge-base content, channels, pausing, rolling back, activating tool servers, platform-wide tool settings.
- Ask the person first before anything that reaches production, reaches people (posts, invites, channels), spends money, or deletes. Run `release-check` before anything reaches real people.
- Large results get saved to a file: read only the fields you need, and delete the file when done. Never paste or save output that carries a token: agent config, eval runs, version history, tool-server code, conversation records.
- Many reads are platform-admin only. If a tool refuses you, ask an admin; and an empty list of failures or conversations means "none" only for admins.
- Name an agent by its display name and slug, with a link to it in Soleon.

Tools: `list_agents`, `list_ideas`, `list_custom_mcp_servers`, `list_knowledge_bases`
