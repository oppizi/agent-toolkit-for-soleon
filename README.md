# bet-1 — Agent-Identity → Allium → JSON Pipeline

> **Status: Prototype (v0.2).** This is a proof-of-concept built to answer one
> question, offline, with evidence. It is not production software. It works —
> 9 logged runs validated green, 99/99 tests passing — but it is the
> *foundation we will extend*, not the finished product. Expect sharp edges,
> a single supported platform for the bundled engine, and APIs that will
> change as capabilities grow on top of it.

Convert an **existing local Claude Code agent** (`.claude/agents/<name>.md`)
into a **validated, deploy-ready agent-infra record** — without re-describing
the agent in a web form. The pipeline distills the agent's identity markdown
(soul + a proposed `config`) into an [Allium](https://github.com/juxt/allium-tools)
specification, asks the user only about genuine gaps the file cannot answer (and
confirms the proposed config in plain language), bundles any operator-named
skills, and emits the exact `POST /agents` request body plus the predicted
DynamoDB CONFIG row, validated offline against a frozen schema oracle.

```
.claude/agents/my-agent.md
        │  distill (LLM, extract — don't interview)
        ▼
agent_identity.allium          ← single reviewable artifact
        │  elicit (gaps only, every question cites the file)
        │  allium check (gate on parsed errors)
        ▼
allium_to_json.py              ← stdlib-only converter
        │
        ├── <slug>.request_body.json    (POST /agents envelope)
        ├── <slug>.ddb_projection.json  (predicted CONFIG row)
        └── <slug>.report.json          (audit: dropped aliases, constraints)
```

---

## Why this exists (intent)

Deploying an agent on the platform today means manually re-entering, in the
Soleon UI, an identity that already exists as a markdown file on the
developer's machine. That re-entry is slow, lossy (personality and hard
constraints get paraphrased), and unverifiable.

bet-1 is one of several competing prototype attempts at the first slice of a
`/deploy-agent` skill. Its specific hypotheses:

1. **Feasibility** — an LLM head plus a small deterministic converter can
   produce a byte-correct platform record from a real identity file, with
   zero manual corrections. **Result: confirmed** (8/8 runs, including a
   real, unsanitized 29 KB identity file).
2. **The Allium leg earns its place** — routing through a spec language
   catches defects a direct markdown→JSON conversion misses. **Result: not
   yet supported.** Under a pre-registered scorecard, the no-Allium control
   arm tied the experiment arm 5/5 on every trap class. We committed to that
   decision rule before running, and we report it plainly — see
   the internal experiment proposal (kept out of this repository) for the
   verdict and the qualitative
   advantages the scorecard does not measure.

The honest framing matters: this prototype proves the *workflow* (distill →
elicit → validate → emit) end to end. The spec-language question stays open
until a future iteration scores the artifacts Allium uniquely produces.

## What's in the box

```
agent-toolkit-for-soleon/
├── .claude-plugin/          marketplace manifest (GitHub installs resolve here;
│                            lists all three bundles)
├── plugins/                 ← THE adoptable artifacts, one per role bundle
│   ├── scope_bundles.json   generated scope pins + sha256, vendored from the platform
│   ├── observer/            read-only bundle (scope pin only, no skills)
│   ├── builder/             the agent build loop — the only bundle shipping Python
│   │   ├── .claude-plugin/  manifest
│   │   ├── README.md        install, usage, supported platforms, escape hatches
│   │   ├── bin/             vendored allium engine (v3.2.4, provenance in LICENSES/)
│   │   ├── contract.json    platform validation contract, generated from source
│   │   ├── LICENSES/        MIT notice + binary provenance chain
│   │   └── skills/          the shipped skills
│   │       ├── deploy-agent/  SKILL.md state machine + converter assets
│   │       └── write-evals/   designing evals whose score reflects the behaviour
│   └── admin/               full platform surface (scope pin only, no skills)
├── preflight/               frozen correctness oracle (schema fixture + contract doc)
├── harness/                 offline validator, judge rubric, tests — never ships
├── samples/                 4 authored identity files (+ skill fixtures under
│                            samples/skills/; a 5th real sample stayed internal)
├── transcripts/             pre-registered elicit answers + per-run transcripts
├── runs/ + runs.jsonl       generated specs, outputs, judge verdicts, 9 logged runs
└── (PLAN.md / PROPOSAL.md)  internal review + results docs — deliberately NOT
                             committed to this repository
```

The boundary is enforced by tests: each bundle under `plugins/` ships alone (an
isolation smoke test copies `plugins/builder/` — the only bundle with executable
assets — to a bare temp directory and runs the conversion end to end); everything
else is experiment telemetry.

## Getting started

### Documentation

- **[TOOLS.md](TOOLS.md)** — full reference for every MCP tool the Soleon server
  exposes (85 live, grouped by area), plus known gaps. Tools are discovered live
  from the server, so new ones appear in the plugin automatically — no plugin
  update needed.
- **[MCP landing page](docs/index.html)** — a public, no-auth docs page for the
  server (serve via GitHub Pages: Settings → Pages → deploy from `main`, `/docs`).

### Prerequisites

- Claude Code (the plugin's skill is executed by it)
- Python 3.9+ (standard library only — no pip installs)
- macOS on Apple Silicon for the bundled engine (`soleon-builder` only); other
  platforms need one `cargo install` (see
  [Supported platforms](plugins/builder/README.md#supported-platforms-bundled-engine))

### Choose a bundle

The repo ships three plugins, one per role. They nest — observer ⊂ builder ⊂
admin — and differ only in the OAuth scopes they request:

| Plugin | Scopes | For |
|---|---|---|
| `soleon-observer` | 8, all reads | Reading agents, traces, failures, usage, evals, ideas, wiki. No write consent at all. |
| `soleon-builder` | 16 | The agent build loop: drafts, deploys, promotions, channel binds, custom MCPs, knowledge bases. Ships the `deploy-agent` and `write-evals` skills. |
| `soleon-admin` | 21 (all) | Platform admins — adds channel/custom-MCP instance reads, eval runs, and discovery. |

**Pick the narrowest one that covers your work.** A broader bundle grants no extra
access: scope is a ceiling on what the token may consent to, never a role. Soleon
authorizes every request against your real permissions, so installing
`soleon-admin` does not make you an admin — it only widens what an access token
could, in principle, be used for.

### Install

From the GitHub marketplace (recommended):

```
/plugin marketplace add oppizi/agent-toolkit-for-soleon
/plugin install soleon-observer@agent-toolkit-for-soleon
```

Or from a local clone:

```
git clone https://github.com/oppizi/agent-toolkit-for-soleon.git
/plugin marketplace add ./agent-toolkit-for-soleon
/plugin install soleon-observer@agent-toolkit-for-soleon
```

Substitute `soleon-builder` or `soleon-admin` as needed. Sign-in is OAuth — the
first request opens the flow in your browser (or run `claude mcp login`). There is
no token to paste.

Each plugin takes an optional **Soleon MCP server URL**, defaulting to the dev
system (`https://mcp-dev.oppizi.com/mcp`). Use the stage-less custom-domain form;
a URL carrying an API-Gateway stage path breaks OAuth discovery.

> **Upgrading from `soleon-deploy-agent`?** It has been renamed to
> `soleon-builder`, with no alias — uninstall the old plugin and install the
> bundle that matches your work. Its **Soleon access token** setting is obsolete
> now that sign-in is OAuth; you can delete it from your keychain.

### Health check (2 seconds, before anything else)

```bash
python3 plugins/builder/skills/deploy-agent/assets/engine.py --selfcheck
```

### Use

```
/deploy-agent .claude/agents/my-agent.md
```

You'll typically answer **one question** (which platform framework — the one
thing a Claude Code agent file never states), then receive three JSON files
and a plain-English summary. **Nothing is deployed**: v0 stops at validated
JSON by design — the live `POST /agents` call is a later phase.

Direct converter invocation (no LLM, spec already in hand):

```bash
python3 plugins/builder/skills/deploy-agent/assets/allium_to_json.py spec.allium --app-env dev --out-dir out/
```

Once an agent is deployed, the other half of the build loop is knowing whether it
actually behaves:

```
/write-evals
```

Designs evals for a deployed agent — how to choose *what* to test (derive from
decisions made and ways the agent can be confidently wrong, not from plausible
user inputs), and how the platform's LLM judge computes a score. That second part
matters more than it sounds: populating `expectedOutput` silently makes half the
score measure *resemblance to your reference answer* rather than correctness,
which is the usual reason a suite goes green while testing very little. Read it
before writing an eval, and whenever a suite passes but you don't trust it.

Full usage, escape hatches, and troubleshooting: [`plugins/builder/README.md`](plugins/builder/README.md).

## Running the tests

```bash
~/.asdf/installs/python/3.14.2/bin/python3 -m pytest harness/tests -o addopts=""
# 99 passed
```

(Any Python ≥3.10 with pytest works; `-o addopts=""` bypasses the parent
repo's xdist requirement. The suite needs the agent-infra repo checkout for
the contract-drift test only.)

The suite is deliberately paranoid: the validator is proven falsifiable by
~25 mutation tests, the soul-fidelity judge is calibrated against three
deliberately corrupted souls it must reject, and the packaging boundary is
verified behaviorally, not by convention.

## What v0.2 deliberately does NOT do

Managing expectations — these are design boundaries, not oversights:

- **No live deployment.** Output is validated JSON for the platform's
  `POST /agents` endpoint; the round-trip happens in a later phase.
- **One bundled engine platform** (darwin-arm64). Others fall back to a
  version-pinned PATH install with a copy-paste recipe.
- **Visibility is `private` only.** Public/restricted agents are out of scope.
- **`config.schedules` and `config.tools` deferred.** Schedules need faithful
  cron validation (a third-party dep that would break the stdlib-only
  guarantee); Claude Code's `tools:` are built-ins, not platform MCP refs — no
  honest identity signal. Everything else in `config` (model, evals,
  guardrails, prompt caching) is distilled + confirmed.
- **Knowledge bases and MCP wiring** are future capability.
- **The validation contract is a build-time snapshot** of platform source.
  At real distribution scale it must become server-published (named in
  the internal proposal as the product path).

## Roadmap (what we extend on top of this)

v0 is the foundation. The seams for growth are already in place:

| Next | Builds on |
|---|---|
| Live `POST /agents` round-trip + deploy status polling | the emitted request body (validated against the real envelope rules) |
| CI cross-build of the engine for linux/x86_64 + linux/arm64 + darwin-x86_64 | the resolver's existing full-platform matrix (`engine.py`) |
| Server-published validation contract with version handshake | `contract.json` + its drift test |
| Knowledge bases + `config.tools` (MCP) + `config.schedules` distillation | the v0.2 config-distill-then-confirm seam (`config_proposal`) |
| Governance-consumption scoring (the open Allium question) | the per-run constraint inventory + distilled evals in `<slug>.report.json` |
| Engine swap (wasm or pure-Python) if the platform matrix bites | the `CliEngine` seam — a new implementation, not surgery |

## Contributing

This is an internal prototype in a bet worktree, so the loop is lightweight:

1. Ask the Soleon team for the internal plan/decision audit trail before
   re-litigating a design choice — supersede explicitly, never silently.
2. Keep the ship boundary: anything the plugin needs at runtime goes in
   `plugins/`; anything else is harness. The packaging tests enforce this.
3. Regenerate the contract after touching platform validation code:
   `python3 harness/sync_contract.py` (the drift test fails loudly otherwise).
4. All 99 tests green before handing off. New failure modes get a negative
   test, not a workaround.

## License

[MIT](LICENSE) © 2026 Oppizi.
The bundled `allium` engine is built from
[juxt/allium-tools](https://github.com/juxt/allium-tools) (MIT); its verbatim
license and the binary's provenance chain (source tag, commit, sha256) ship
in [`plugins/builder/LICENSES/allium-tools-MIT.txt`](plugins/builder/LICENSES/allium-tools-MIT.txt).

## Acknowledgments

Third-party open source projects this software bundles, adapts, or is informed
by are attributed per their respective license in the [`NOTICE`](NOTICE) file:

- **MIT** — spec engine [juxt/allium-tools](https://github.com/juxt/allium-tools) (bundled as the `allium` binary)
- **MIT** — installed skills from [mattpocock/skills](https://github.com/mattpocock/skills) (`git-guardrails-claude-code`, `grill-with-docs`, `to-prd`)
- **Apache-2.0** — packaging pattern adapted from [aws/agent-toolkit-for-aws](https://github.com/aws/agent-toolkit-for-aws)
