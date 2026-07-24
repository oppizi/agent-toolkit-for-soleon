# Adopt the omnigent Agent YAML DSL as the v1 vocabulary

The v0 prototype is hardwired to one deploy target (Soleon / agent-infra),
speaking that platform's vocabulary (`framework`, `soul`, `contract`,
`appEnv`). For v1 we want the toolkit to become harness-agnostic — a future
**Meta Harness Agent** that can target any execution runtime. We chose to
adopt the existing [omnigent](https://github.com/omnigent-ai/omnigent) Agent
YAML DSL (Apache License 2.0) as the v1 vocabulary rather than invent our own
DSL or stay Soleon-only, because it already models the harness-agnostic
concepts we need (`harness`, `executor`, `tools`, `policies`, `sandbox`,
sub-agents/handoffs) and lets us later reference the upstream project directly.
For now we adopt **only the DSL vocabulary** — no omnigent source code — and
keep the v0 Soleon terms and v1 DSL terms as two clearly labelled vocabularies
during the transition (see `CONTEXT.md`).
