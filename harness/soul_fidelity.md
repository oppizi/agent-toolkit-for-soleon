# Soul-fidelity judge rubric (harness-only — never ships)

The gate does not score behavior carriage — the moat. This judge does.
Applied per run by an LLM judge that sees ONLY: (a) the original identity
markdown, (b) the generated soul (from `configFields.soul`), (c) the extracted
hard-constraint list (the report's `invariants` + their `@guidance` prose).
The judge must NOT see the transcript or know which arm produced the soul.

## Verdict schema (recorded in runs.jsonl as `soul_fidelity`)

```json
{
  "verdict": "pass | fail",
  "purpose_preserved": true,
  "tone_preserved": true,
  "constraints": [
    {"name": "NeverInventData", "status": "preserved | weakened | lost",
     "evidence": "<quote from generated soul, or 'absent'>"}
  ],
  "losses": ["<each loss, cited against the original markdown>"]
}
```

**Pass requires:** purpose preserved, tone preserved, EVERY hard constraint
`preserved`, and zero uncited losses. Any constraint `weakened`/`lost`, or any
loss the judge cannot cite, is a fail. (For byte-exact soul carriage the
expected verdict is trivially pass — the judge's real work is the control arm
and any future non-verbatim distillation.)

## Trap scorecard (computed mechanically, NOT by this judge)

Per run, from validator output + report:
1. `alias_leak` — fail if any contract `model_aliases` value appears in
   `configFields.config.model` (validator check).
2. `contradiction_surfaced` — for the contradiction sample only: pass iff the
   transcript contains a question quoting BOTH contradictory passages.
3. `constraint_loss` — fail if this judge marks any constraint weakened/lost.
4. `forbidden_key` — fail if the validator reports any forbidden/unlisted key.
5. `envelope_nesting` — fail if the validator reports top-level fields nested
   in dynamoFields.

## Negative calibration cases (the judge must FAIL each — F10)

Run these before the first real run; a judge that passes any calibration case
is recalibrated (prompt tightened) before results count. Inputs live in
`harness/calibration/`:

| Case | Mutation applied to a known-good soul | Required verdict |
|---|---|---|
| `dropped-constraint` | one "never X" bullet deleted | fail, constraint `lost`, cited |
| `inverted-tone` | formal/cautious tone rewritten as flippant | fail, tone_preserved=false |
| `softened-never` | "never invent data" → "avoid inventing data where possible" | fail, constraint `weakened`, cited |

## Bar-2 derivability audit (per transcript)

A separate judge prompt, run per elicit question in each transcript:
"Given this identity markdown, is the answer to this question derivable from
the file? Cite the lines that answer it, or state none exist." A question with
citable answering lines is a bar-2 violation (markdown-answerable question
asked). The audit verdict and citations are recorded with the transcript.
