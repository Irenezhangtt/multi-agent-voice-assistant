# Evaluation and latency results

Recorded on **2026-10-24** during local MVP development. These results cover different versions and execution modes; they are not interchangeable estimates of production reliability.

## Current v6 verification

- **89 local tests passed**, including optional tau2 integration tests. The base CI environment omits tau2 and skips its integration module.
- **12/12 Harbor Docker regression trials passed**, with zero exceptions. Four scripted-model scenarios were repeated three times, using actual MCP and SQL with runtime networking disabled.
- Dedicated concurrency tests use synchronization barriers to prove overlap; other tests cover dependency ordering, human-escalation priority, stale-write fencing, early-offer confirmation, recovery, and audio before text-stream completion.

## Historical v5 business and judge cohort

The original live cohort used the official pinned tau2 text simulator: eight project-authored clinic scenarios, two attempts each. Three additional live conversations produced a **19-conversation cohort**. Calls used real Claude, stdio MCP, and independent temporary SQLite state.

| Metric | Result |
|---|---:|
| SQL-verified business outcomes | 19/19 |
| New-booking requests successfully completed | 5/5 |
| Human-escalation markers | 2/19 |
| Required escalation scenarios correctly marked | 2/2 |
| Unnecessary escalation in other scenarios | 0/17 |
| Harbor Sandbox Judge verdicts | 16 pass, 3 needs review |
| Mean Judge quality score | 92.9/100 |
| Feedback records uploaded and read back from Cloud | 152 |

Appointment creation, modification, cancellation, insurance results, escalation, and review-without-consent have different success conditions. Only new-booking requests enter the 5/5 denominator. The escalation fraction reflects the authored task mix, not a production transfer rate.

Judge scoring used a separate Claude prompt and context inside Harbor Docker verifiers. It re-evaluated saved evidence; it did not re-run all 19 business conversations in Docker. SQL assertions independently gated success. The judge and business agents use the same model family and have not been human-calibrated.

The repeat evaluation changed five total scores and three verdicts on unchanged conversation evidence. The previous output formats also differed for some valid scores. These differences are retained as grading variability, not presented as an improvement to the business agent. Historical judge scores do not certify the optimized v6 prompt.

## Real API latency comparison

| Metric | Frozen sequential v5 | Parallel/streaming v6 |
|---|---:|---:|
| LLM calls | 6 | 4 |
| Server-side text-to-first-audio | 25.02 s | 7.67 s |
| Complete business result | 24.65 s | 11.22 s |

Each version was tested **once**, with the same fictional appointment-review and independent insurance request, real Claude/MCP/Deepgram TTS, and separate temporary databases. The candidate trace confirmed concurrent delegation and first audio before completion of the business turn. No appointment was created: the request only authorized preparing an offer.

The audio timestamp is the first Deepgram PCM received by the server. STT endpointing, microphone capture, client network delay, and playback buffering are excluded. These observations are not P50/P95 estimates or a guaranteed speedup; cloud latency is variable and 7.67 seconds remains an optimization target.

## What is published

[summary.json](summary.json) contains the aggregate metrics, selected code hashes, and the final regression snapshot ID. It excludes API keys, caller transcripts, database files, absolute machine paths, and private LangSmith identifiers. Raw artifacts remain under the ignored local `reports/` directory; they are not part of a fresh clone.

To reproduce your own results, follow the [README evaluation commands](../../README.md#evaluation-and-reproducibility), [tau2 integration](../tau-bench.md), and [Harbor Judge guide](../harbor-judge.md). Those commands regenerate artifacts using your credentials and current API responses; identical model scores or latency are not guaranteed.
