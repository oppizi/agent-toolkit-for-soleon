# Soleon plugins

Bring Soleon into your coding agent. This repository publishes
the `soleon` plugin: a connection to the Soleon server plus a set of skills for
building, testing and running Soleon agents without leaving your editor.

- **The connection** is a standard MCP server (`soleon`, OAuth sign-in). Its tools
  are discovered live, so a new Soleon tool appears without a plugin update. Full
  reference: [TOOLS.md](TOOLS.md).
- **The skills** are `SKILL.md` folders under [`plugins/soleon/skills/`](plugins/soleon/skills/).

Today the repository is packaged for **Claude Code**: the install files it ships
are Claude Code's (`.claude-plugin/`, `plugins/soleon/.mcp.json`). The MCP server
works with any MCP client, and the skills use the common `SKILL.md` layout, but
packaging for another harness (its own install file, and versions of the skills
that write Claude Code subagents) is not here yet.

## Install (Claude Code)

From GitHub:

```
/plugin marketplace add soleon-ai/soleon-plugins
/plugin install soleon@agent-toolkit-for-soleon
```

Or from a local clone:

```
git clone https://github.com/soleon-ai/soleon-plugins.git
/plugin marketplace add ./soleon-plugins
/plugin install soleon@agent-toolkit-for-soleon
```

`agent-toolkit-for-soleon` is the marketplace's name. It is the repository's
earlier name, kept on purpose: it is part of every installed plugin's id
(`soleon@agent-toolkit-for-soleon`), so changing it would make everyone reinstall
and sign in again.

Sign-in is OAuth: the first request opens the flow in your browser (or run
`claude mcp login`). There is no token to paste.

The plugin takes an optional **Soleon MCP server URL**, defaulting to the dev
system (`https://mcp-dev.oppizi.com/mcp`). Use the stage-less custom-domain form;
a URL carrying an API-Gateway stage path breaks OAuth discovery.

### Prerequisites

- Claude Code
- Python 3.9+ (standard library only, no pip installs)
- For `deploy-agent` only: macOS on Apple Silicon for the bundled engine; other
  platforms need one `cargo install` (see
  [Supported platforms](plugins/soleon/README.md#supported-platforms-bundled-engine))

### One plugin, every role

There is one plugin, `soleon`, for everyone. It asks consent for every Soleon
scope family, and Soleon decides what you can actually do from your real
permissions: the server lists only the tools you may use and re-checks your
permissions on every call. Installing it does not make you an admin; the
platform-admin tools stay hidden from a member, and a call to one made by hand
with the same token is refused.

> **Upgrading from `observer` / `builder` / `admin`?** They are replaced by
> `soleon`, with no alias. Uninstall the old plugin, install `soleon`, and sign in
> again. Tools are now named `mcp__plugin_soleon_soleon__<tool>`, so update any
> permission rules that named `mcp__plugin_<role>_soleon__…`, and re-run
> `/pull-agent <slug>` for agents you pulled with the old plugin. (Older names —
> `soleon-observer` / `soleon-builder` / `soleon-admin`, `soleon-deploy-agent` —
> upgrade the same way; the last one's **Soleon access token** setting is obsolete
> and can be deleted from your keychain.)

## What the skills do

| Area | Skills |
|---|---|
| Start and build an agent | `start-an-agent`, `create-agent`, `deploy-agent` |
| Find and read an agent | `find-an-agent`, `read-identity`, `read-skills`, `see-agent-tools`, `review-draft` |
| Change a draft on dev | `edit-identity`, `edit-skill`, `change-a-setting`, `tool-switches`, `attach-tool-server`, `connect-channel` |
| Test it | `test-draft-in-chat`, `write-an-eval`, `write-evals`, `run-an-eval`, `read-eval-results` |
| Run it locally | `pull-agent`, `run-local-eval`, `write-skill-scripts` |
| Knowledge bases | `browse-knowledge`, `build-knowledge-base`, `pull-knowledge-base` |
| Release | `deploy-to-dev`, `promote`, `compare-environments`, `release-check`, `roll-back`, `pause-agent` |
| Watch it | `read-a-conversation`, `failures-check`, `usage-check`, `cost-check` |

A few in more detail:

- **`/create-agent`** interviews you about a new agent, creates it on dev, attaches
  the integrations and knowledge bases it needs with your approval on every
  change, writes its instructions and evals, validates it, and deploys only on
  your yes.
- **`/deploy-agent .claude/agents/my-agent.md`** converts a local Claude Code agent
  file into a validated Soleon agent through an
  [Allium](https://github.com/juxt/allium-tools) specification, asking only about
  what the file cannot answer, and deploys it on your explicit confirmation.
- **`/pull-agent <slug>`** pulls your dev draft of a Soleon agent into the project
  as a local subagent. The local session does the thinking; every tool runs on
  Soleon with your credentials and the agent's tool policy. The agent's files land
  under `.soleon/agents/<slug>/`, and every save is pushed to your draft. Each local
  run shows up in Soleon's Traces under "Draft Agents" (channel `Local`).
  `/run-local-eval <slug>` scores the local agent with the platform's own judge
  prompt: scores only, never a pass/fail verdict.
- **`/pull-knowledge-base <kb>`** copies a knowledge base's text sources into
  `.soleon/knowledge-bases/<slug>/` and pushes one edited source back only on your
  word. **`/build-knowledge-base`** builds a knowledge base with Soleon's ingestion
  while every model call that decides its pages runs on your machine.
- **`/write-evals`** designs evals whose score reflects the behaviour under test,
  including how the platform's judge computes a score.

Full usage, escape hatches and troubleshooting:
[`plugins/soleon/README.md`](plugins/soleon/README.md).

## Repository layout

```
soleon-plugins/
├── .claude-plugin/          marketplace manifest (lists the one plugin)
├── plugins/
│   ├── scope_pin.json       generated scope pin + sha256, vendored from the platform
│   └── soleon/              the plugin that ships
│       ├── .claude-plugin/  manifest
│       ├── .mcp.json        the soleon MCP server + its OAuth scope pin
│       ├── README.md        install, usage, supported platforms, escape hatches
│       ├── bin/             vendored allium engine (provenance in LICENSES/), the
│       │                    local-emulation stdio MCP servers, hooks and shared
│       │                    client (stdlib Python)
│       ├── hooks/hooks.json saves under .soleon/agents/ → Soleon draft; each local
│       │                    agent run → one trace turn
│       ├── contract.json    platform validation contract, generated from source
│       ├── LICENSES/        MIT notice + binary provenance chain
│       └── skills/          the skills listed above
├── preflight/               frozen correctness oracle (schema fixture + contract doc)
├── harness/                 validator, tests and generators; never ships
├── samples/, transcripts/,  experiment records from the deploy-agent prototype
│   runs/, runs.jsonl        (identity files, elicit answers, logged runs); the
│                            plugin may not reference them
├── .agents/, skills-lock.json  three skills from mattpocock/skills installed for
│                            working on this repo, and their lock file
└── docs/                    the public docs page (GitHub Pages)
```

Anything the plugin needs at runtime lives in `plugins/`; everything else is
harness. The packaging tests enforce that boundary: one of them copies
`plugins/soleon/` into a bare directory and runs a conversion end to end.

## Running the tests

```bash
python3 -m pytest harness/tests -o addopts=""
```

Any Python 3.10+ with pytest works. The contract-drift test also needs a checkout
of the Soleon platform repository.

## Contributing

1. Keep the ship boundary: runtime code goes in `plugins/`, everything else in
   `harness/`.
2. Add a skill under `plugins/soleon/skills/` and to `EXPECTED_SKILLS` in
   `harness/tests/test_packaging.py`.
3. After changing platform code, regenerate the vendored artifacts:
   `python3 harness/sync_contract.py` and `python3 harness/sync_scope_pin.py` (the
   drift tests fail otherwise).
4. Every test green before review. A new failure mode gets a negative test, not a
   workaround.

A pull request into `main` needs one approval. CI runs on every push and pull
request to `main`: the full suite on macOS arm64 (where the bundled engine
resolves), the engine-independent guards on Linux against Python 3.10 and 3.13,
CodeQL, and a conventional-commit check on pull request titles. See
`.github/workflows/`.

## License

[MIT](LICENSE) © 2026 Oppizi.
The bundled `allium` engine is built from
[juxt/allium-tools](https://github.com/juxt/allium-tools) (MIT); its verbatim
license and the binary's provenance chain (source tag, commit, sha256) ship
in [`plugins/soleon/LICENSES/allium-tools-MIT.txt`](plugins/soleon/LICENSES/allium-tools-MIT.txt).

## Acknowledgments

Third-party open source projects this software bundles, adapts, or is informed
by are attributed per their respective license in the [`NOTICE`](NOTICE) file:

- **MIT** — spec engine [juxt/allium-tools](https://github.com/juxt/allium-tools) (bundled as the `allium` binary)
- **MIT** — installed skills from [mattpocock/skills](https://github.com/mattpocock/skills) (`git-guardrails-claude-code`, `grill-with-docs`, `to-prd`)
- **Apache-2.0** — packaging pattern adapted from [aws/agent-toolkit-for-aws](https://github.com/aws/agent-toolkit-for-aws)
