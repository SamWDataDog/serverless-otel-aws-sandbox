# Serverless x AWS OTel Sandbox

A sandbox for comparing Datadog's two documented ways to get
OpenTelemetry-API-shaped traces out of an AWS Lambda function — side by
side, across four languages:

| | Mode | What it is |
|---|---|---|
| **1** | OTel SDK via the Extension | A real OpenTelemetry SDK `TracerProvider`, exporting spans over OTLP/HTTP to the **Datadog Lambda Extension's** local receiver (`localhost:4318`). No Datadog tracing layer. |
| **2** | Native tracer + OTel API bridge | Plain OpenTelemetry **API** calls (no SDK), with `DD_TRACE_OTEL_ENABLED=true` so Datadog's own native tracer picks them up and reports real spans, via the Datadog language tracing layer. |

Reference: https://docs.datadoghq.com/serverless/aws_lambda/opentelemetry/

## Repo layout

| Path | What it is |
|---|---|
| [`FINDINGS.md`](FINDINGS.md) | The full investigation behind every conclusion on this page — evidence, corrections, and how each result was established. |
| [`BUILD_WALKTHROUGH.md`](BUILD_WALKTHROUGH.md) | A chronological account of every error hit building the Python side from scratch, and how each was fixed. |
| [`python/`](python/README.md) | Python sandbox. Own `serverless.yml`, own README. |
| [`node/`](node/README.md) | Node.js sandbox. Own `serverless.yml`, own README. |
| [`dotnet/`](dotnet/README.md) | .NET sandbox. Own `template.yaml` (AWS SAM), own README. |
| [`java/`](java/README.md) | Java sandbox. Own `template.yaml` (AWS SAM + Maven), own README. |

Each language directory is a fully independent stack — isolated
dependencies, isolated deploy/teardown, no shared infrastructure.

## Per-language status

This is the current-state reference. `FINDINGS.md` is the chronological
investigation journal behind every row below, including corrections —
read it in order for the evidence; read this table for the answer.

| Language | Mode 1 | Mode 2 | Gotcha to know before you deploy |
|---|---|---|---|
| **Python** | [Works](FINDINGS.md#result-it-works) | [Works](FINDINGS.md#1-the-other-otel-mode-native-ddtrace--dd_trace_otel_enabled) | None beyond the telemetry support matrix below. |
| **Node.js** | [Works](FINDINGS.md#correction-with-root-cause-the-real-otelsandboxnode-had-two-bugs-of-its-own-caught-by-live-ui-evidence) | [Works](FINDINGS.md#root-cause-found-and-partially-fixed-dd_trace_otel_enabledtrue-does-not-auto-register-the-bridge) — requires an explicit `new TracerProvider().register()` call; `DD_TRACE_OTEL_ENABLED=true` alone does not register it | `nodejs24.x` loads handlers via ESM `import()`. The Datadog instrumentation hook needs `--import .../hook.mjs` in `NODE_OPTIONS`, not just `--require`, or instrumentation never activates — silently: `200 OK`, zero trace activity, no error. |
| **.NET** | [Works](FINDINGS.md#mode-1-ported-siddhithas-sample-into-this-repo-re-verified-from-here) | [**Does not currently produce any verifiable traces**](FINDINGS.md#the-priority-test-corrected-twice--first-by-a-reconciliation-investigation-then-by-a-root-cause-investigation-showing-the-first-correction-still-wasnt-rigorous-enough) — zero spans are ever ingested, against either a cold-start or a warm invocation. See `FINDINGS.md` for the full investigation if you want the detail. | None for Mode 1. |
| **Java** | [Works](FINDINGS.md#mode-1-built-fresh-no-sample-to-port) | [Works](FINDINGS.md#mode-2-the-priority-investigation--registration-and-nesting-verified-not-assumed). Real span-construction and parent/child nesting evidence is available directly in the tracer's own debug logs, not just a transport-success line. | A scoped `sam build <FunctionName>` on this multi-function template deletes the sibling function's build artifacts. Always run a plain `sam build` with no function argument. |

Anchor links above point at the FINDINGS.md heading where GitHub's
markdown renderer resolves the exact text — if a heading gets reworded
later, re-derive the anchor from the new text (lowercase, punctuation
stripped, spaces to hyphens) rather than trusting the old link blindly.

## Telemetry signal support

Each sandbox function sends up to four telemetry signals. Status below:

- **✅** confirmed working
- **❌** confirmed not working (with the reason)
- **N/A** — Mode 2 never sets up an OTel SDK pipeline for this signal in
  the first place, so there's nothing to fail; it's a bridge from the
  OTel **API** to Datadog's native tracer, not a parallel OTel SDK
  metrics/logs stack
- **Not separately tested** — expected to behave the same as the
  confirmed case next to it, but not independently re-verified for this
  specific function

| Language | Mode | Traces | OTel Logs | OTel Metrics | Custom Metrics (DogStatsD) | Evidence |
|---|---|---|---|---|---|---|
| Python | 1 — OTel SDK | ✅ | ❌ `404` from the Extension | ❌ `404` from the Extension | ✅ | [Round 1](FINDINGS.md#all-4-forms-of-telemetry-traces-otel-metrics-otel-logs-custom-metrics) |
| Python | 2 — native tracer | ✅ | N/A | N/A | ✅ | [Round 2](FINDINGS.md#round-2-closing-summary) |
| Node.js | 1 — OTel SDK | ✅ | ❌ `404` from the Extension | ❌ `404` from the Extension | ✅ | [Round 4](FINDINGS.md#phase-3-full-signal-matrix-both-functions) |
| Node.js | 2 — native tracer | ✅ | N/A | N/A | ✅ | [Round 4](FINDINGS.md#phase-3-full-signal-matrix-both-functions) |
| .NET | 1 — OTel SDK | ✅ | Not separately tested | ❌ `404` from the Extension | ✅ | [Round 5](FINDINGS.md#round-5-closing-summary) |
| .NET | 2 — native tracer | ❌ Zero spans ever ingested | N/A | N/A | Not separately tested | [Round 5](FINDINGS.md#round-5-closing-summary) |
| Java | 1 — OTel SDK | ✅ | Not separately tested | ❌ `404` from the Extension | ✅ | [Round 6](FINDINGS.md#round-6-closing-summary) |
| Java | 2 — native tracer | ✅ | N/A | N/A | ✅ | [Round 6](FINDINGS.md#round-6-closing-summary) |

The Datadog Lambda Extension's OTLP receiver only implements the traces
route — OTel metrics and OTel logs both get a `404` wherever they've been
tested, by design, not as a bug in any of these sandboxes.

**Custom-metrics asymmetry (Mode 2, Python vs. Node.js)**: both show
✅, but they're not the same test. `python/handler2.py` sends its custom
metric via a raw DogStatsD UDP packet; `node/src/handler2.js` uses
`datadog-lambda-js`'s `sendDistributionMetric()` instead. Each file
documents its own reason for that choice (see the files directly) —
changing either to match the other would invalidate an already-verified
finding, so this is noted here rather than "fixed."

For the full evidence behind every row above, see `FINDINGS.md`.

## Layer versions

**Verified as of: 2026-10-07.**

**Non-goal, by design**: this repo does not track the newest Datadog
layer versions. It represents a verified point in time, not a rolling
target — chasing "latest" means recurring breakage in a tool whose
whole purpose is stable reproduction. If you need to test a newer
version (or a specific customer's version), override
`DD_EXTENSION_VERSION` (Python/Node) or the `ExtensionVersion` SAM
parameter (.NET/Java) — and expect the published signal matrix above to
not necessarily apply at that version until it's independently
re-verified:

```bash
aws lambda list-layer-versions --layer-name Datadog-Extension --region us-east-1
aws lambda get-layer-version-by-arn --arn <full-arn-including-version> --region us-east-1
```

**`Datadog-Extension` is the one layer shared across all four stacks**,
and all four now pin `101` by default, so a support engineer comparing languages is
comparing like with like on that one layer. This is an intentional
convergence, not a coincidence of four separately-verified rounds:

| Stack | Datadog-Extension default | Verified on 101? |
|---|---|---|
| Python | `101` | ❌ **No — see below.** |
| Node.js | `101` | ✅ Yes |
| .NET | `101` (SAM param default) | ✅ Yes |
| Java | `101` (SAM param default) | ✅ Yes |

**Python is the one open item.** Python's entire signal matrix in this
README and every Extension-related conclusion in `FINDINGS.md` was
established on Extension `100`, not `101`. Changing the default pin
does not retroactively change what was actually verified — do not read
"pinned at 101" as "verified at 101" for Python. The pin is
forward-looking; the matrix is not, until someone deploys Python's
stack on `101` and confirms the same results hold. **This is the single
outstanding action standing between this repo and a fully
self-consistent verified state** — one Python deploy-and-verify closes
it.

**What does NOT converge, and must not be forced to**: everything below
is a separate artifact with its own independent version numbering.
Forcing these onto a shared number would be fabricating a verification
that never happened, not consistency:

| Stack | Language tracer layer |
|---|---|
| Python | `Datadog-Python311:128` |
| Node.js | `Datadog-Node24-x:143` |
| .NET | `dd-trace-dotnet:26` (param default) |
| Java | `dd-trace-java:28` (param default) |

Python's ADOT variant also pins `aws-otel-python-amd64-ver-1-32-0:7`
(verified via `get-layer-version-by-arn`, see `FINDINGS.md` "Round 7").
**Unlike `Datadog-Extension` above, this ADOT layer's version numbers
are not published consistently across regions** — `7` is only confirmed
for `us-east-1`. A different region needs its own lookup (see
`python/README.md`'s "Region and Datadog site" section for the exact
command and the `DD_ADOT_LAYER_VERSION` override) before assuming it'll
resolve the same way; this hasn't been confirmed outside `us-east-1`.

Python's and Node's region overrides are `SANDBOX_AWS_REGION`, not the
more commonly-exported `AWS_REGION` — deliberately, to avoid silently
inheriting an ambient `AWS_REGION` from your shell/CI and deploying
somewhere these pinned layer versions were never verified.

## Known issues and references

The repo's two strongest findings. Neither has a tracked ticket
reference in this repo — that's a deliberate choice by the owner, not
an oversight — so both are restated here in full, each linking to the
`FINDINGS.md` evidence behind it, rather than pointing at an empty or
invented reference:

- **[.NET Mode 2 ingests zero spans under every tested condition](FINDINGS.md#the-priority-test-corrected-twice--first-by-a-reconciliation-investigation-then-by-a-root-cause-investigation-showing-the-first-correction-still-wasnt-rigorous-enough).**
  Confirmed against both cold-start and warm-invocation test requests
  directly in the Datadog UI/API — zero spans ever landed, across every
  variation tested. A clean deploy, a clean invoke, and the Extension's
  own "successfully sent trace" log line are all present and are not
  evidence this works. See `FINDINGS.md` for the full investigation.
  This finding stays open and un-ticketed in this repo by owner choice;
  the case for escalating it to engineering doesn't go away just
  because this repo doesn't track a Jira key for it.
- **[The Datadog Lambda Extension's OTLP receiver returns `404` on `/v1/logs` and `/v1/metrics`](FINDINGS.md#all-4-forms-of-telemetry-traces-otel-metrics-otel-logs-custom-metrics).**
  Confirmed across every language and mode in this repo that attaches
  the Extension — traces work, OTel metrics and OTel logs don't, by
  design (not a bug in any of these sandboxes). See `FINDINGS.md` for
  the full investigation and every language's confirmation of this.

**Open item worth knowing about, not a confirmed finding**:
[`FINDINGS.md`'s Round 5 closing summary](FINDINGS.md#round-5-closing-summary)
raises an unconfirmed lead, in the course of investigating .NET Mode 2's
zero-span finding above, that the Extension's "Universal
Instrumentation" feature may be synthesizing some span data
independently of the managed tracer rather than only relaying what it
receives. This was noted in passing during that investigation and
never independently confirmed or ruled out — stated here as a
documented open question, not an action item assigned to anyone.

## Maintenance

This repo is public. It is licensed under the MIT License (see
`LICENSE`), with one exception: the .NET Mode 1 function derives from
[Siddhitha Bhoopathy's sample](https://github.com/SiddhithaBhoopathy/otel-dotnet-lambda-extension-samples),
which is not covered by this repo's license. That source repo has no
`LICENSE` file anywhere in it — verified directly (GitHub's license
API and a full recursive tree listing both confirm it), not assumed —
so it's all-rights-reserved by default under copyright law. Attribution
to that sample is preserved in the code itself
(`dotnet/src/OtelSandboxDotnet/`).

CI (`.github/workflows/ci.yml`) runs `scripts/check-anchors.py` and
`scripts/check-yaml.py`, Python/Node/shell syntax checks across every
language's source files, `shellcheck` on `scripts/verify.sh`, and
`make -n` on all four Makefiles. See that workflow file for the exact
checks — it does not compile .NET or Java (that would need the .NET
SDK/Maven installed and package resolution over the network, adding
flakiness for a check that isn't essential), does not deploy anything,
and does not require AWS or Datadog credentials.

## Status: everything is torn down

Every AWS resource from all four sandboxes has been deleted. Nothing
from this repo is currently deployed.

To redeploy any language, you'll need:

- AWS credentials for the target account/region (`us-east-1` by
  default for every stack — overridable per-language, see "Region and
  Datadog site" in each README and "Layer versions" above).
- A Datadog API key (`DD_API_KEY`) — never hardcoded or committed, always
  passed via environment variable/deploy parameter.
- The CLI tool that language's stack uses:
  - **Python/Node.js**: [Serverless Framework](https://www.serverless.com/) (`npm i -g serverless`, or use the local `npx` devDependency in each directory).
  - **.NET/Java**: [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html), plus the .NET SDK or Maven respectively.

See each language's own README (linked in the table above) for the exact
build/deploy/invoke/teardown commands for that stack.
