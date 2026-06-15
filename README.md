# bet-1 — Agent-Identity → Allium → JSON Pipeline

> **Status: Prototype (v0).** This is a proof-of-concept built to answer one
> question, offline, with evidence. It is not production software. It works —
> 8/8 logged runs validated green, 66/66 tests passing — but it is the
> *foundation we will extend*, not the finished product. Expect sharp edges,
> a single supported platform for the bundled engine, and APIs that will
> change as capabilities grow on top of it.

Convert an **existing local Claude Code agent** (`.claude/agents/<name>.md`)
into a **validated, deploy-ready agent-infra record** — without re-describing
the agent in a web form. The pipeline distills the agent's identity markdown
into an [Allium](https://github.com/juxt/allium-tools) specification, asks the
user only about genuine gaps the file cannot answer, and emits the exact
`POST /agents` request body plus the predicted DynamoDB CONFIG row, validated
offline against a frozen schema oracle.

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
├── .claude-plugin/          marketplace manifest (GitHub installs resolve here)
├── plugin/                  ← THE adoptable artifact (self-contained Claude Code plugin)
│   ├── .claude-plugin/      manifest + local marketplace entry
│   ├── README.md            install, usage, supported platforms, escape hatches
│   ├── bin/                 vendored allium engine (v3.2.4, provenance in LICENSES/)
│   ├── contract.json        platform validation contract, generated from source
│   ├── LICENSES/            MIT notice + binary provenance chain
│   └── skills/deploy-agent/ the skill (SKILL.md state machine + converter assets)
├── preflight/               frozen correctness oracle (schema fixture + contract doc)
├── harness/                 offline validator, judge rubric, 66 tests — never ships
├── samples/                 3 authored identity files (a 4th real, unsanitized
│                            sample was used in the experiment and kept internal)
├── transcripts/             pre-registered elicit answers + per-run transcripts
├── runs/ + runs.jsonl       generated specs, outputs, judge verdicts, 8 logged runs
└── (PLAN.md / PROPOSAL.md)  internal review + results docs — deliberately NOT
                             committed to this repository
```

The boundary is enforced by tests: `plugin/` ships alone (an isolation smoke
test copies it to a bare temp directory and runs the conversion end to end);
everything else is experiment telemetry.

## Getting started

### Prerequisites

- Claude Code (the plugin's skill is executed by it)
- Python 3.9+ (standard library only — no pip installs)
- macOS on Apple Silicon for the bundled engine; other platforms need one
  `cargo install` (see [Supported platforms](plugin/README.md#supported-platforms-bundled-engine))

### Install

From the GitHub marketplace (recommended):

```
/plugin marketplace add oppizi/agent-toolkit-for-soleon
/plugin install soleon-deploy-agent@agent-toolkit-for-soleon
```

Or from a local clone:

```
git clone https://github.com/oppizi/agent-toolkit-for-soleon.git
/plugin marketplace add ./agent-toolkit-for-soleon
/plugin install soleon-deploy-agent@agent-toolkit-for-soleon
```

### Health check (2 seconds, before anything else)

```bash
python3 plugin/skills/deploy-agent/assets/engine.py --selfcheck
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
python3 plugin/skills/deploy-agent/assets/allium_to_json.py spec.allium --app-env dev --out-dir out/
```

Full usage, escape hatches, and troubleshooting: [`plugin/README.md`](plugin/README.md).

## Running the tests

```bash
~/.asdf/installs/python/3.14.2/bin/python3 -m pytest harness/tests -o addopts=""
# 66 passed
```

(Any Python ≥3.10 with pytest works; `-o addopts=""` bypasses the parent
repo's xdist requirement. The suite needs the agent-infra repo checkout for
the contract-drift test only.)

The suite is deliberately paranoid: the validator is proven falsifiable by
~25 mutation tests, the soul-fidelity judge is calibrated against three
deliberately corrupted souls it must reject, and the packaging boundary is
verified behaviorally, not by convention.

## What v0 deliberately does NOT do

Managing expectations — these are design boundaries, not oversights:

- **No live deployment.** Output is validated JSON for the platform's
  `POST /agents` endpoint; the round-trip happens in a later phase.
- **One bundled engine platform** (darwin-arm64). Others fall back to a
  version-pinned PATH install with a copy-paste recipe.
- **Visibility is `private` only.** Public/restricted agents are out of scope
  for this slice.
- **Verbatim soul carriage.** The agent's system prompt travels byte-exact;
  summarization, knowledge bases, evals, and MCP wiring are future capability.
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
| Structured behavior entities (knowledge, evals, guardrails) in the spec | the invariant/constraint carriage and the additive migration sketched in the internal proposal |
| Governance-consumption scoring (the open Allium question) | the per-run constraint inventory in `<slug>.report.json` |
| Engine swap (wasm or pure-Python) if the platform matrix bites | the `CliEngine` seam — a new implementation, not surgery |

## Contributing

This is an internal prototype in a bet worktree, so the loop is lightweight:

1. Ask the Soleon team for the internal plan/decision audit trail before
   re-litigating a design choice — supersede explicitly, never silently.
2. Keep the ship boundary: anything the plugin needs at runtime goes in
   `plugin/`; anything else is harness. The packaging tests enforce this.
3. Regenerate the contract after touching platform validation code:
   `python3 harness/sync_contract.py` (the drift test fails loudly otherwise).
4. All 66 tests green before handing off. New failure modes get a negative
   test, not a workaround.

## License

[MIT](LICENSE) © 2026 Oppizi.
The bundled `allium` engine is built from
[juxt/allium-tools](https://github.com/juxt/allium-tools) (MIT); its verbatim
license and the binary's provenance chain (source tag, commit, sha256) ship
in [`plugin/LICENSES/allium-tools-MIT.txt`](plugin/LICENSES/allium-tools-MIT.txt).

## Acknowledgments

Third-party open source projects this software bundles, adapts, or is informed
by are attributed per their respective license in the [`NOTICE`](NOTICE) file:

- **MIT** — spec engine [juxt/allium-tools](https://github.com/juxt/allium-tools) (bundled as the `allium` binary)
- **MIT** — installed skills from [mattpocock/skills](https://github.com/mattpocock/skills) (`git-guardrails-claude-code`, `grill-with-docs`, `to-prd`)
- **Apache-2.0** — packaging pattern adapted from [aws/agent-toolkit-for-aws](https://github.com/aws/agent-toolkit-for-aws)
