# Local chat model comparison

Which model should write the demo's answers. The model does not find anything: retrieval, the dataset queries, the access checks and the answer's content are deterministic, and what reaches the model is a finished grounded draft. So this measures whether a model can write that draft without changing what it says.

| Measure | What it means |
| --- | --- |
| Invented | Figures or identifiers in the answer that were in neither the draft nor the evidence. Any number above zero disqualifies a model: the platform's claim is that answers are traceable. Reformatted dates do not count - a model writing `2026-05-14` as *May 14, 2026* has restated a supplied fact - and single digits are ignored because in these answers they are list ordinals. |
| Retention | Share of the draft's identifiers, dates, page numbers and amounts that survive into the answer. Losing them is quieter than inventing them and just as damaging - the answer still reads well, but it can no longer be checked. |
| Refusal kept | When the draft withholds a domain the user may not read, does the answer still say so. A refusal smoothed into silence reads as *there is nothing to report*. |
| Latency | Wall clock for one synthesis, CPU only. |

**What this cannot see.** Retention and invention are token measures. A model that picks a real figure from the context and attaches it to the wrong label scores clean on both, so the answers themselves were read. That review is what the notes below record.

## Results

| Model | Runs | Errors | Invented | Retention | Refusals kept | Mean latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `qwen2.5:0.5b-instruct` | 10 | 0 | 0 | 31% | 2/2 | 18.0 s |
| `qwen2.5:1.5b-instruct` | 10 | 4 | 2 | 26% | 2/2 | 87.5 s |

## Per scenario

| Model | Scenario | Latency | Invented | Retention |
| --- | --- | ---: | ---: | ---: |
| `qwen2.5:0.5b-instruct` | `brief-2-alarm-meaning-al017` | 13.9 s | 0 | 50% |
| `qwen2.5:0.5b-instruct` | `brief-2-alarm-meaning-al017` | 2.7 s | 0 | 50% |
| `qwen2.5:0.5b-instruct` | `brief-5-latest-quote-revision` | 12.2 s | 0 | 18% |
| `qwen2.5:0.5b-instruct` | `brief-5-latest-quote-revision` | 2.1 s | 0 | 18% |
| `qwen2.5:0.5b-instruct` | `brief-6-delivery-and-cost` | 12.0 s | 0 | 12% |
| `qwen2.5:0.5b-instruct` | `brief-6-delivery-and-cost` | 1.2 s | 0 | 12% |
| `qwen2.5:0.5b-instruct` | `access-technician-allowed-alarms` | 45.0 s | 0 | 75% |
| `qwen2.5:0.5b-instruct` | `access-technician-allowed-alarms` | 47.0 s | 0 | 75% |
| `qwen2.5:0.5b-instruct` | `access-commercial-denied-telemetry` | 32.9 s | 0 | 0% |
| `qwen2.5:0.5b-instruct` | `access-commercial-denied-telemetry` | 11.1 s | 0 | 0% |
| `qwen2.5:1.5b-instruct` | `brief-2-alarm-meaning-al017` | 23.4 s | 0 | 50% |
| `qwen2.5:1.5b-instruct` | `brief-2-alarm-meaning-al017` | 5.2 s | 0 | 50% |
| `qwen2.5:1.5b-instruct` | `brief-5-latest-quote-revision` | 29.4 s | 1 | 27% |
| `qwen2.5:1.5b-instruct` | `brief-5-latest-quote-revision` | 8.0 s | 1 | 27% |
| `qwen2.5:1.5b-instruct` | `brief-6-delivery-and-cost` | 180.0 s | 0 | 0% |
| `qwen2.5:1.5b-instruct` | `brief-6-delivery-and-cost` | 180.0 s | 0 | 0% |
| `qwen2.5:1.5b-instruct` | `access-technician-allowed-alarms` | 180.0 s | 0 | 0% |
| `qwen2.5:1.5b-instruct` | `access-technician-allowed-alarms` | 180.0 s | 0 | 0% |
| `qwen2.5:1.5b-instruct` | `access-commercial-denied-telemetry` | 65.6 s | 0 | 50% |
| `qwen2.5:1.5b-instruct` | `access-commercial-denied-telemetry` | 23.0 s | 0 | 50% |

## Reading the numbers, and the decision

Read alongside the table above; regenerating the measurement does not overwrite
this section.

**The 180-second rows are timeouts, not slow answers.** `qwen2.5:1.5b-instruct`
failed to finish four of ten runs on this machine, both attempts at
`brief-6-delivery-and-cost` and both at `access-technician-allowed-alarms`. Those
are the two largest contexts in the set — a machine with several quotations and
orders. A timed-out run scores zero retention, which is what drags that model's
mean below the smaller one's despite it writing better answers when it finishes.
This is CPU-only inference on a laptop; a machine with a GPU would not
necessarily show it.

**The smaller model is fast because it writes less.** `qwen2.5:0.5b-instruct`
answers the commercial scenarios in one or two sentences, retaining 12–18% of
the draft's identifiers and figures. Read one and the problem is obvious: the
draft has a service-standing section, a quotations section with three
quotations, and their revision states; the answer is a sentence. Nothing is
fabricated, but most of the answer is gone.

**A clean score can still hide a wrong answer.** In an earlier run the small
model wrote *"The total acquisition value is GBP 5,020.00"* for a machine whose
acquisition value is 10,050.00 — 5,020 is the total of a different, unordered
quotation that was also in the context. Invention scores zero, because the
figure is real; retention barely moves, because it is one token. Only reading
the answer catches it. This is the measure's limit, and the reason a token
score alone should not decide anything.

**The larger model preserves structure.** Where it finishes, it keeps the
sections, attaches the right figure to the right label, formats dates
sensibly, and keeps the refusal when a domain was withheld. Its retention on
completed runs is 27–50%, against 12–18% for the smaller model on the same
scenarios.

### Decision

**The demo default is `ollama` with `qwen2.5:1.5b-instruct`,** the better writer
of the two measured: it keeps the answer's sections, attaches figures to the
right labels, and preserves a refusal. The demo should show the model writing
the answer.

This is the slower default and it is chosen with the costs above in view. On
CPU-only hardware that model averages 87.5 s and did not finish the two
largest-context scenarios at all, so the gateway's request and stream-idle
timeouts are raised to 200 s to cover prompt evaluation. A synthesis that fails
or times out falls back to the grounded draft rather than losing the turn, so
the cost of a slow rewrite is the answer's wording and not its content.

**`LLM_PROVIDER=deterministic` remains one flag away** (`-LlmProvider
deterministic`), and is the right choice for a fast run, for hardware that
cannot carry the model, and for measurement: the evaluation harness runs
deterministically so a scenario's result reflects retrieval and access control
rather than a model's mood.

Note the caveat above before reading a green run as proof of quality: retention
is 26-31%, so most of the draft's identifiers do not survive into the model's
prose. That is a real cost of this default, not a rounding error.

What would change this decision: a GPU, or a machine where the larger model
completes the two large-context scenarios inside the timeout. Re-run the
command below to check.


Scenarios used: `brief-2-alarm-meaning-al017`, `brief-5-latest-quote-revision`, `brief-6-delivery-and-cost`, `access-technician-allowed-alarms`, `access-commercial-denied-telemetry`.

Reproduce with:

```bash
docker compose -f infrastructure/docker-compose.yml up -d ollama
docker exec infrastructure-ollama-1 ollama pull qwen2.5:1.5b-instruct
python scripts/compare_llm_models.py
```
