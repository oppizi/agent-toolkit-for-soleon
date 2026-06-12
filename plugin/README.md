# soleon-deploy-agent

Turn a local Claude Code agent definition (`.claude/agents/<name>.md`) into a
**validated, deploy-ready agent-infra record** — without re-describing your
agent in a web form. The skill distills your agent's identity into an Allium
spec, asks only about genuine gaps the file can't answer, and emits the exact
`POST /agents` request body plus the predicted DynamoDB CONFIG row.

**What this plugin does NOT do (v0.1 — PoC):** it does **not** call any live
API, create agents in any environment, or deploy anything. The output is two
validated JSON files you (or a later pipeline phase) submit. Visibility is
emitted as `private` only in this slice; public visibility is out of scope.

## Install

From the GitHub marketplace:

```
/plugin marketplace add oppizi/agent-toolkit-for-soleon
/plugin install soleon-deploy-agent@agent-toolkit-for-soleon
```

Or from a local checkout of [oppizi/agent-toolkit-for-soleon](https://github.com/oppizi/agent-toolkit-for-soleon):

```
/plugin marketplace add ./agent-toolkit-for-soleon
/plugin install soleon-deploy-agent@agent-toolkit-for-soleon
```

(The only contents that matter at runtime are this directory's
`contract.json`, `bin/`, and `skills/`.)

## Use

```
/deploy-agent .claude/agents/my-agent.md
```

The skill will: run a 2-second selfcheck → distill your identity file into an
Allium spec → ask you only what the file cannot answer (typically one
question: the target framework) → validate → write three files beside the
spec:

- `<slug>.request_body.json` — the `POST /agents` body (top-level
  `slug/displayName/framework/appEnv` + `dynamoFields` + `configFields`)
- `<slug>.ddb_projection.json` — the predicted create-time CONFIG row
- `<slug>.report.json` — audit trail: dropped model aliases, extracted
  constraints, decode decisions, resolved engine version

Direct converter invocation (no LLM, spec already in hand):

```
python3 skills/deploy-agent/assets/allium_to_json.py spec.allium --app-env dev --out-dir out/
```

## Supported platforms (bundled engine)

| Platform | Bundled binary | Status |
|---|---|---|
| darwin-arm64 | `bin/allium-darwin-arm64` | ✅ shipped (built from juxt/allium-tools v3.2.4, provenance in `LICENSES/`) |
| darwin-x86_64 / linux-arm64 / linux-x86_64 | — | ❌ CI cross-build gap — PATH fallback applies |

No bundled binary for your platform? Install the pinned engine version:

```
cargo install --git https://github.com/juxt/allium-tools --tag v3.2.4 allium-cli
```

A different `allium` version on PATH is **refused by default** (this plugin's
output contract is verified against 3.2.4 exactly). Set
`ALLIUM_ENGINE_UNPINNED=1` to accept the mismatch at your own risk.

## Escape hatches

| Override | How | Default |
|---|---|---|
| Deploy target app env | skill argument / `--app-env` converter flag | `dev` |
| Bedrock model id | state an explicit catalog id in your identity file or when asked; Claude Code aliases (`fable`, `haiku`, …) are always dropped | omitted — platform template default |
| Framework | stated in the file or invocation → no question asked; otherwise one elicit turn | always asked (never silently defaulted) |
| Engine version pin | `ALLIUM_ENGINE_UNPINNED=1` | pinned to `contract.json.engine_version` |
| Visibility | not overridable in v0.1 — `private` only | `private` |

## Health check

```
python3 skills/deploy-agent/assets/engine.py --selfcheck
```

Verifies the contract loads, the engine binary resolves, and versions match —
in ~2 seconds, before any LLM work is spent.

## Troubleshooting

Every error message carries the problem, the cause, and the fix. The two most
common:

- **EngineNotFound** — no bundled binary for your platform and no `allium` on
  PATH → run the `cargo install` command above.
- **ContractError** — `contract.json` missing/corrupt → reinstall the plugin
  from its source (the contract ships with the bundle; it is not user-editable).
