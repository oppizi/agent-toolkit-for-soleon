# Agent Toolkit for Soleon

This toolkit converts a local agent identity into a validated, deploy-ready
configuration. Its language is in transition: **v0** speaks the Soleon /
agent-infra platform's vocabulary; **v1** adopts the omnigent Agent YAML DSL to
become harness-agnostic, oriented toward a future **Meta Harness Agent** — an
agent that can target any execution runtime, not just Soleon. The two sections
below are kept separate on purpose; cross-references mark where a v0 term and a
v1 term name the same axis.

## Language

### Soleon platform (v0 — current)

**Identity file**:
A local Markdown file (`.claude/agents/<name>.md`) describing one agent — its
purpose, behavior, and constraints. The input the toolkit converts.
_Avoid_: agent definition, persona file

**Soul**:
The agent's governing system prompt, carried byte-exact with no summarization.
The v0 name for the axis the v1 DSL splits into `prompt` / `instructions`.

**Allium spec**:
The intermediate specification an Identity file is distilled into, written in
the Allium spec language, before validation and emission.

**Contract**:
The frozen platform-validation rules (allowed keys, frameworks, model aliases,
slug pattern, caps) an Allium spec is checked against. Generated from
agent-infra source, never hand-edited.

**Framework**:
The Soleon execution runtime that runs a deployed agent (`maverick`,
`nanobot`, `openclaw`). The v0, Soleon-specific instance of the axis the v1
DSL generalizes as **Harness**.

**App env**:
The target deployment environment (e.g. `dev`) a converted agent is built for.

**Distillation**:
The phase that extracts an Allium spec from an Identity file. Governed by
"extract, don't interview": nothing the Identity file already answers may
become a question.

**Elicitation**:
The phase that asks the user only for genuine gaps the Identity file leaves
unanswered. Never re-asks anything Distillation already found.

**Model alias**:
A Claude Code frontmatter `model:` value (`fable`, `opus`, `sonnet`, `haiku`,
`inherit`, `default`) that is **not** a Bedrock catalog id. It registers
cleanly but silently breaks the agent at runtime, so it is dropped, never
deployed.

### Agent DSL (v1 — adopted from omnigent)

**Harness**:
The execution framework that runs an Agent (`claude-sdk`, `openai-agents`,
`codex`, …). The v1 generalization of the v0 Soleon **Framework** — same axis,
broader scope.

**Agent**:
An autonomous entity defined by a prompt, an Executor, and a set of Tools. The
unit the toolkit produces and deploys.

**Sub-agent**:
An Agent invoked by a parent Agent. When exposed as a callable capability it is
an **Agent-as-tool**; when it takes over the conversation it is a **Handoff**.

**Executor**:
The bundle of Harness, model, and authentication settings that determines how
an Agent runs.

**Tool**:
An external capability an Agent can call. Comes in three kinds: **MCP tool**,
**Function tool**, **Agent-as-tool**.

**MCP tool**:
A Tool backed by a remote or local MCP server.

**Function tool**:
A Tool backed by a callable function with a typed parameter schema.

**Agent-as-tool**:
A Sub-agent exposed to a parent Agent as a callable Tool (control returns to
the parent when it finishes).

**Handoff**:
The transfer of control from one Agent to another, as distinct from calling it
as an Agent-as-tool (control does not return).

**Policy**:
A runtime guardrail that constrains an Agent's requests, responses, tool calls,
or tool results.

**Sandbox**:
The security-isolation layer bounding an Agent's access to the operating system
(writable paths, network access).

**Params**:
Typed, user-supplied parameters made available to an Agent's Tools.

---

The v1 Agent DSL vocabulary is adopted from the **omnigent** project's Agent
YAML specification (Apache License 2.0). See `NOTICE` for attribution.
