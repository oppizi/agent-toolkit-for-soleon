---
name: write-evals
description: Design and write Soleon agent evals whose score actually means something. Covers how to choose WHAT to test (derive from decisions and failure modes, not happy paths), how the platform's LLM judge computes a score (it averages sub-scores, and one of them measures resemblance to your reference answer rather than correctness), and the four rules that turn a fuzzy 0-100 into a diagnostic pass/fail. Use when creating, reviewing, or debugging evals — especially when a suite passes but you don't trust it.
---

# /write-evals — evals whose score means something

Most Soleon eval suites go green while testing very little. This skill explains
why that happens, and how to write evals that fail when they should.

There are two separate problems. **What to test** is a design problem. **Whether
the number reflects it** is a mechanism problem. Get the second one wrong and
even a well-chosen eval reports nothing useful.

---

# Part 1 — Choosing what to test

If your evals feel arbitrary, it's usually because they were written by asking
"what might a user say?" That generates plausible inputs and tests nothing in
particular.

**Derive evals from two sources instead.**

## 1a. Every decision someone made

Every time a human chose between two defensible behaviours, that decision can
silently regress. Those are your evals. Go through the agent's soul and
decision log and list the choices:

- "Quote the minimum up front, unless they've signalled a small budget."
- "Cite published benchmarks, but always caveated."
- "Route sub-minimum visitors to the cheaper channel only when it fits their goal."

Each of those is one eval. A decision nobody wrote an eval for is a decision
that will quietly revert.

## 1b. Every way the agent can be confidently wrong

Not "what if it doesn't know" — models handle that reasonably. The dangerous
cases are where the agent produces something fluent and false:

- **Where the tool disagrees with the truth.** A calculator that returns a price
  below a minimum the business won't sell at.
- **Where general knowledge differs from your specifics.** The model knows what
  "direct mail minimums" usually are. It doesn't know yours.
- **Where the agent must NOT say something** it can plainly see (an internal name
  on a public page, a competitor priced lower in the same payload).
- **Where the visitor isn't who they appear to be** (a partner, a job seeker, a
  supplier — not a buyer).
- **Where inventing is easier than admitting a gap** (URLs, page names, figures).

## 1c. Sanity checks on the set

- **One eval, one behaviour.** Compound rubrics average into mush.
- **Prefer evals you expect to FAIL today.** An eval that passes on day one may
  be testing nothing. The valuable ones encode a standard you haven't met yet.
- **Cover each subsystem** — tools, knowledge retrieval, guardrails, routing,
  tone — rather than five variations of the same path.

---

# Part 2 — How the score is actually computed

You cannot write a good eval without knowing this. Source:
`lambda/eval_runner/judge.py` in agent-infra.

**The score is produced by an LLM.** A Bedrock judge model (Haiku by default,
constrained by `JUDGE_MODEL_ALLOWLIST`) is prompted with the agent's response
and your eval definition, and returns sub-scores. The platform then **recomputes
the final number itself** rather than trusting the judge's own total.

Four possible dimensions. **A dimension is scored only if you populated its
corresponding field:**

| Dimension | Active when you fill | What it measures |
|---|---|---|
| `toolsScore` | `expectedToolCalls` | Whether those calls happened (deterministic matching) |
| `skillsScore` | `expectedSkills` | Visible evidence the skill was applied |
| `outputScore` | `expectedOutput` | **Resemblance to your reference answer** |
| `criteriaScore` | `evaluationCriteria` | Your rubric |

```
final score = plain arithmetic mean of the non-null sub-scores
```

Declared a field but the judge omitted the dimension? It is **backfilled to 50**.

## The trap

Fill both `expectedOutput` and `evaluationCriteria` — the natural thing to do —
and you get:

```
score = (outputScore + criteriaScore) / 2
```

**Half your score is now "does this resemble the answer I wrote", not "did the
agent do the right thing."** A fluent, plausible reply scores well on
`outputScore` no matter how badly it violates your rubric. This is the single
most common reason a suite goes green while testing nothing.

## pass_fail does not fix it by itself

In `pass_fail` mode the judge is instructed:

> *"`outcome`: `pass` when the final averaged score is ≥ 70 AND no single
> applicable sub-score is < 40"*

and the orchestrator takes that verdict verbatim. So it is **still
average-driven** — a response that flunks one hard criterion but reads well
still passes.

---

# Part 3 — The four rules

**Rule 1 — Leave `expectedOutput` empty.**
Then `outputScore` is null and `score == criteriaScore`. One dimension, your
rubric, nothing diluting it. This alone fixes most bad evals. Only populate
`expectedOutput` when similarity to a reference answer is genuinely the thing
you're testing.

**Rule 2 — Give the judge an explicit scoring formula with discrete values.**
Don't describe quality and hope. State the arithmetic:

```
SCORING FORMULA — apply exactly, do not interpolate:
- All three criteria met                     -> 90
- Criteria 1 and 2 met, criterion 3 NOT met  -> 30
- Criterion 1 or 2 also missing              -> 10
Do not award any other value. Do not adjust for tone, length,
helpfulness or writing quality.
```

Judges comply with this, and the score becomes **diagnostic**: 30 tells you
exactly which criterion failed without opening the transcript.

**Rule 3 — Use `scoringMode: "score"` with a `scoreThreshold`.**
Threshold between your pass value and your top failure value. With the formula
above, `scoreThreshold: 70` makes 90 pass and 30 fail cleanly.

**Rule 4 — Demand quoted evidence, never list forbidden phrases.**
Open every rubric with *"Quote the exact sentences you are judging."* A judge
asked to check for the absence of listed phrases will pattern-match those
phrases against replies that don't contain them and hallucinate a match.
Ask what must be **present**, and make the judge quote it.

## Naming the loophole

If a criterion has an obvious cheap satisfier, close it explicitly:

> *Criterion 3 is judged STRICTLY. Describing the control as 'comparable',
> 'matched' or 'similar' does NOT satisfy it — that is mechanism (criterion 2),
> not a trade-off. The reply must state something the visitor GIVES UP.*

Without that sentence, an adjective ticks the box and the eval passes.

---

# Part 4 — What the judge cannot do

The judge prompt contains:

> *Rule 3: "Do not penalize the response for facts you cannot verify (URLs,
> statistics, citations, dates). Assume the content is what the rubric is asking
> you to grade."*

**So the judge will not catch a fabricated URL, an invented statistic, or a
wrong date.** An eval built on "must not hallucinate a link" silently cannot
work.

Test those a different way:

- **`expectedToolCalls`** — deterministic matching. Assert the agent actually
  queried the source rather than inferring it from how the prose reads. This is
  the only non-judgemental signal available.
- **Rubric the framing, not the fact.** You can't ask the judge whether a URL is
  real, but you can ask whether the reply *claims* a page exists — and pair that
  with a tool-call assertion.
- **Verify outside the harness** for anything factual.

Note that adding `expectedToolCalls` reintroduces averaging
(`(toolsScore + criteriaScore) / 2`). That's usually a good trade, since both
dimensions now measure something real — but be deliberate about it.

---

# Worked example

**Behaviour under test:** the knowledge base stores a one-line summary page and
a full source document for each topic. The agent should answer from the source.
Does it?

### Before — scored 80, told us nothing

```yaml
expectedOutput: "A substantive answer distinguishes attribution from
  incrementality and explains holdout testing with at least one practical
  qualification..."
evaluationCriteria: "PASS requires: 1. distinguishes response from lift.
  2. explains the holdout mechanism. 3. includes at least one practical
  qualification, such as opportunity cost, sample-size noise, or
  comparability of the control group."
scoringMode: score        # no threshold
```

Two problems. `outputScore` was half the result and only measured similarity to
the reference. And criterion 3 listed *"comparability of the control group"* as
a qualifying example — so the agent writing **"a comparable control group"**
ticked it with an adjective. Scored 80. Passed. Tested nothing.

### After — scored 30, failed, and said why

```yaml
expectedOutput: ""                # outputScore now null
scoringMode: score
scoreThreshold: 70
evaluationCriteria: |
  Quote the exact sentences you are judging.

  NUMBERED CRITERIA
  1. Distinguishes measuring response (attribution) from measuring
     incremental lift, in substance rather than by using the words alone.
  2. Explains the holdout mechanism: a comparable group is deliberately
     excluded, and the difference between groups is the lift.
  3. Names a concrete COST, LIMITATION or TRADE-OFF of running a holdout.
     Qualifying: the withheld group is deliberately not marketed to, so the
     cleaner number is paid for in lost reach; too small a control produces a
     noisy result; a single test measures one campaign at one moment.

  Criterion 3 is judged STRICTLY. Describing the control as 'comparable',
  'matched' or 'similar' does NOT satisfy it — that is mechanism (criterion 2),
  not a trade-off. The reply must state something the visitor GIVES UP or must
  BE CAREFUL ABOUT.

  SCORING FORMULA — apply exactly, do not interpolate:
  - All three criteria met                     -> 90
  - Criteria 1 and 2 met, criterion 3 NOT met  -> 30
  - Criterion 1 or criterion 2 also missing    -> 10

  Do not award any other value. Do not adjust for tone, length, helpfulness
  or writing quality.

  Before scoring, quote the exact clause you credit for criterion 3.
```

Result: `subScores: {"criteriaScore": 30.0}`, `outcome: fail`. The judge quoted
its evidence per criterion and landed on exactly one of the three permitted
values. **30 is now self-explanatory** — the agent nailed the concept and the
mechanism, and named no trade-off, which is precisely the shallow-retrieval
failure the eval exists to detect.

---

# Checklist

Before saving an eval:

- [ ] Tests **one** behaviour, traceable to a decision or a failure mode
- [ ] `expectedOutput` is **empty** (unless resemblance genuinely is the test)
- [ ] `evaluationCriteria` has **numbered criteria** and an **explicit formula
      with discrete values**
- [ ] `scoreThreshold` set, sitting between the pass value and the top failure
- [ ] Rubric opens by demanding **quoted evidence**
- [ ] Any obvious **cheap satisfier is named and excluded**
- [ ] No criterion depends on the judge **verifying a fact** it cannot check
- [ ] `expectedToolCalls` set if the point is *whether it consulted a source*
- [ ] You can say what each score value **means** without reading the transcript

## Reviewing an existing suite

Everything green is a warning, not a result. Check:

1. Does any eval have both `expectedOutput` and `evaluationCriteria`? Half its
   score is measuring the wrong thing.
2. Is `scoringMode: "score"` with no `scoreThreshold`? Then `outcome` is
   `score_only` and **nothing can fail**.
3. Pick the highest-risk eval and read the actual agent response. If a
   reasonable person would have failed it, the rubric is too loose.
