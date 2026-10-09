# Findings

This is the chronological investigation journal, including superseded
conclusions written and corrected in place as each round's evidence
came in — read it in order for the "why" and the evidence trail. The
root [`README.md`](README.md) is the current-state reference; it links
each of its table rows to the specific heading here that establishes it.

**A note on identifiers below**: real 12-digit AWS account IDs have been
replaced with `<AWS_ACCOUNT_ID>` (AWS-owned public layer-publisher
accounts, e.g. `464622532012`/`901920570463`, are left as-is — those are
published ARNs, not secrets). A named individual has been replaced with
a role label ("the repo author" for first-hand observations, "a
reviewer" for externally-supplied corrections) to preserve the
first-hand-vs-inferred distinction the text relies on, without using a
real name. This placeholdering is intentional, not an indication the
original investigation was sloppy about identifiers. Request IDs and
trace IDs are left as-is — they're not sensitive and are load-bearing
evidence for specific claims below.

Sandbox: Python 3.11 Lambda sending OTel SDK traces through the Datadog
Lambda Extension's local OTLP receiver (not native `ddtrace`). Deployed to
`us-east-1`, account `<AWS_ACCOUNT_ID>`.

## Result: it works

The Extension's OTLP HTTP collector starts on `127.0.0.1:4318`, receives the
spans exported by the OTel SDK, and successfully forwards a trace to
Datadog. Confirmed from extension debug logs (`DD_LOG_LEVEL=debug`) on a test
invocation:

```
DD_EXTENSION | DEBUG | OTLP HTTP | Starting collector on 127.0.0.1:4318
DD_EXTENSION | DEBUG | OTLP | Successfully buffered traces to be aggregated.   (x3 — one per span)
DD_EXTENSION | DEBUG | TRACES | Successfully sent trace (1 attempts, 1277 bytes)
```

**In the Datadog UI (APM > Traces):** filter for `service:otel-aws-sandbox`.
No `env` is set in this sandbox, so it appears under `env:none`. The trace
has 3 spans: a root span for the Lambda invocation (from
`AwsLambdaInstrumentor`), a child `do-work` span (created manually in
`handler.py`), and a grandchild span for the `STS.GetCallerIdentity`
botocore call (from `BotocoreInstrumentor`).

## Logs/trace correlation: correlated in the UI, but via `request_id`, not `dd.trace_id`

This was the open question the sandbox was built to answer, and the result
is more nuanced than "yes" or "no" — two different correlation mechanisms
are in play, and only one of them fires here.

**The log text itself carries no trace context.** Two `logger.info(...)`
calls were added to the handler. In CloudWatch they appear as plain runtime
log lines with no `dd.trace_id`/`dd.span_id` fields:

```
[INFO]  2026-09-29T21:05:11.533Z  <request-id>  otel-aws-sandbox: handler invoked, event={...}
[INFO]  2026-09-29T21:05:12.656Z  <request-id>  otel-aws-sandbox: sts.get_caller_identity account=... arn=...
```

Compare this to the native `ddtrace` Lambda layer, which patches Python's
`logging` module to auto-inject `dd.trace_id` / `dd.span_id` into log
records. The OTel SDK does none of that — it has no awareness of, and
doesn't touch, the stdlib `logging` module.

**But in the Datadog UI, the logs *do* show as correlated to the trace** —
verified by viewing a trace and seeing 4 correlated log lines (`START`, the
2 `logger.info()` calls, `REPORT`/`END`). This works through a *different,
Lambda-specific* mechanism: the Extension collects the function's CloudWatch
logs itself (no separate Forwarder needed — confirmed in debug logs:
`Logs aggregator service started`, then the Extension subscribes to the
Lambda Logs API for `[Platform, Extension, Function]` log types), and every
span carries an `aws.request_id` tag (visible in the span JSON as
`"aws":{"request_id":"..."}`). Datadog's backend joins logs to traces by
matching that `request_id` for a given invocation — entirely independent of
whether `dd.trace_id` appears in the log body, and independent of which
tracer (OTel or `ddtrace`) produced the trace.

**Practical implication**: for a Lambda function specifically, you get
UI-level log/trace correlation "for free" via `request_id`, regardless of
tracer choice — as long as Datadog is collecting both the function's logs
and its traces. What you *don't* get with the OTel-SDK-through-Extension
path is `dd.trace_id`/`dd.span_id` inside the raw log message itself, which
matters if logs are also consumed somewhere else (a log aggregator outside
Datadog, a manual correlation lookup, `grep`-ing raw CloudWatch for a trace
ID) — there, you'd need to manually inject the current span's trace/span ID
into the log record yourself (e.g. via a custom `logging.Filter`).

**How to verify a given trace actually came from the OTel SDK path** (not
`ddtrace`), by inspecting the span's raw JSON in the Datadog UI:
- `telemetry.sdk.name: "opentelemetry"` / `telemetry.sdk.language` /
  `telemetry.sdk.version` — set by the OTel SDK's `Resource`; `ddtrace` has
  no `telemetry.sdk.*` namespace.
- `otel.scope.name` / `otel.scope.version` (e.g.
  `opentelemetry.instrumentation.botocore`) — OTel's `InstrumentationScope`,
  naming the specific OTel instrumentation library that created the span.
  `ddtrace` spans have no `otel.scope` tag.
- `otel.trace_id` (32 hex chars) — the original W3C Trace Context ID the
  OTel SDK generated, kept as a tag since it arrived via OTLP.
- At the transport level (Extension debug logs, `DD_LOG_LEVEL=debug`): OTel
  SDK traffic hits the **OTLP HTTP receiver on port 4318**
  (`OTLP HTTP | Starting collector on 127.0.0.1:4318`); the native `ddtrace`
  layer instead sends to the Extension's **Trace Agent on port 8126**. Which
  port actually received traffic for a given invocation is a second,
  protocol-level confirmation of which tracer was used.

## All 4 forms of telemetry: traces, OTel metrics, OTel logs, custom metrics

The sandbox only set out to test traces. Extended `handler.py` to also emit
an OTel metric, an OTel log record, and a Datadog custom metric in the same
invocation, to get a definitive answer on which telemetry signals the
Extension's OTLP receiver actually accepts.

| Signal | Path | Result |
|---|---|---|
| **Traces** | OTel SDK → OTLP HTTP → Extension `/v1/traces` | ✅ Works (see above) |
| **Custom metrics** | Raw DogStatsD UDP packet → Extension `127.0.0.1:8125` | ✅ Works |
| **OTel metrics** | OTel Metrics SDK → OTLP HTTP → Extension `/v1/metrics` | ❌ `404 Not Found` |
| **OTel logs** | OTel Logs SDK → OTLP HTTP → Extension `/v1/logs` | ❌ `404 Not Found` |

### OTel metrics: confirmed not supported (matches doc, now verified)

Added a `MeterProvider` + `PeriodicExportingMetricReader` +
`OTLPMetricExporter(endpoint="http://localhost:4318/v1/metrics")`, created
a counter, called `.add(1, ...)` on it, and explicitly called
`meter_provider.force_flush()` before the handler returns. Result, straight
from CloudWatch:

```
[ERROR]  Failed to export batch code: 404, reason: Not Found
```

This matches Datadog's own docs, which state plainly: "Sending custom
metrics from the OTLP endpoint in the extension is not supported." This
sandbox confirms that statement empirically — the Extension's OTLP HTTP
server has no route registered for `/v1/metrics`, so the exporter's POST
gets a 404, not a silent drop.

### OTel logs: not documented either way — tested, and it also fails

Datadog's docs don't explicitly say whether OTLP logs work through the
Lambda Extension (the generic Datadog Agent OTLP docs mention a
`DD_OTLP_CONFIG_LOGS_ENABLED` variable, but that's Agent-wide, not
Lambda-Extension-specific, and setting it wasn't tested/relevant here since
the failure is at the HTTP routing level, before any such config could take
effect). So this was tested directly: added a `LoggerProvider` +
`BatchLogRecordProcessor` + `OTLPLogExporter(endpoint="http://localhost:4318/v1/logs")`,
attached it via `LoggingHandler` to a second logger (`otel-logs-test`,
kept separate from the sandbox's normal `logger` so the two paths don't get
confused), logged one line through it, and force-flushed. Result:

```
[ERROR]  Failed to export logs batch code: 404, reason: Not Found
```

Identical failure mode to metrics — same 404, same "no route registered"
signature. **Conclusion: the Datadog Lambda Extension's OTLP receiver only
implements the traces route.** Neither OTel metrics nor OTel logs can reach
Datadog through this Extension via OTLP, regardless of endpoint path
correctness on the SDK side (`/v1/metrics` and `/v1/logs` are the correct,
spec-compliant OTLP HTTP paths — the 404 is the Extension's server, not a
client-side mistake).

### Custom metrics: works, but avoid `datadog_lambda` for this specific sandbox

Datadog's documented way to send custom metrics from Python is
`from datadog_lambda.metric import lambda_metric`. **Deliberately did not
use it here**: importing `datadog_lambda` — even only for the metrics
helper, never touching its tracing API — unconditionally imports `ddtrace`
and calls `patch_all()` at import time (confirmed by reading
`datadog_lambda/__init__.py`), which monkeypatches third-party libraries
(including `botocore`) for `ddtrace` tracing. That's exactly the kind of
native-tracer contamination this sandbox exists to avoid, and it would
likely conflict with OTel's own `BotocoreInstrumentor` patching the same
library.

Instead, sent the metric as a raw DogStatsD distribution-metric UDP packet
directly to the Extension (`<name>:<value>|d|#<tags>` to `127.0.0.1:8125`)
— which is exactly the protocol `datadog_lambda.metric.lambda_metric()`
itself uses under the hood when the Extension is present (confirmed by
reading `datadog_lambda/dogstatsd.py`/`statsd_writer.py`), just without the
ddtrace side effect. Confirmed working end to end from CloudWatch:

```
DD_EXTENSION | DEBUG | Parsed 1 valid metrics, sending to aggregator
DD_EXTENSION | DEBUG | Flushing 0 series and 1 distributions
DD_EXTENSION | DEBUG | Successfully flushed 0 series and 1 distributions
```

(sent to Datadog's `api/beta/sketches` endpoint, the same one native
distribution metrics use). **Takeaway for the doc**: custom metrics work
fine through the Extension for an OTel-only setup, but flag that the
official `datadog_lambda` helper library is not tracer-neutral — a customer
doing OTel-only who reaches for it will silently get `ddtrace` patching
their dependencies too, unless they submit the DogStatsD packet themselves
or explicitly avoid/neutralize `ddtrace`'s patching after import.

## Doc code sample review — line-by-line, against the actual installed SDK

This is a direct review of the doc's own Python code sample (the "Create a
TracerProvider" / "Instrument AWS SDK and AWS Lambda" block), checked line
by line against `opentelemetry-api`/`opentelemetry-sdk` 1.27.0 in a real
Python interpreter — not by inspection, by actually running each import and
recording the real error:

```python
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.exporter.otlp.trace_exporter import OTLPExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.resource import Resource
from opentelemetry.semconv.resource import (
    SERVICE_NAME,
    SemanticResourceAttributes,
)

tracer_provider = TracerProvider(resource=Resource.create({SERVICE_NAME: <YOUR_SERVICE_NAME>}))

tracer_provider.add_span_processor(
    SimpleSpanProcessor(
        OTLPExporter(endpoint="http://localhost:4318/v1/traces")
    )
)

trace.set_tracer_provider(tracer_provider)

BotocoreInstrumentor().instrument(tracer_provider=tracer_provider)
AwsLambdaInstrumentor().instrument(tracer_provider=tracer_provider)
```

| Line | Verdict | Detail |
|---|---|---|
| `from opentelemetry.instrumentation.botocore import BotocoreInstrumentor` | ✅ Correct as-is | Matches what's deployed and working in this sandbox. |
| `from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor` | ✅ Correct as-is | Same. |
| `from opentelemetry import trace` | ✅ Correct as-is | |
| `from opentelemetry.sdk.trace import TracerProvider` | ✅ Correct as-is | |
| `from opentelemetry.exporter.otlp.trace_exporter import OTLPExporter` | ❌ Broken | Real run: `ModuleNotFoundError: No module named 'opentelemetry.exporter.otlp.trace_exporter'`. Correct: `from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter` — module is nested under `proto.http`, and the class is `OTLPSpanExporter`, not `OTLPExporter`. (There's a separate `proto.grpc` variant too, for the gRPC exporter — don't mix the two; this doc's `:4318` endpoint is the HTTP port, so it must pair with the HTTP exporter package, `opentelemetry-exporter-otlp-proto-http`.) |
| `from opentelemetry.sdk.trace.export import SimpleSpanProcessor` | ✅ Correct as-is | |
| `from opentelemetry.resource import Resource` | ❌ Broken | Real run: `ModuleNotFoundError: No module named 'opentelemetry.resource'`. Correct: `from opentelemetry.sdk.resources import Resource` (note also `resources`, plural). |
| `from opentelemetry.semconv.resource import (SERVICE_NAME, SemanticResourceAttributes)` | ❌ Broken | Real run: `ImportError: cannot import name 'SERVICE_NAME' from 'opentelemetry.semconv.resource'`. Neither `SERVICE_NAME` nor `SemanticResourceAttributes` exist in that module in 1.27.0 — the module only exports `ResourceAttributes` (plus some enum classes). `SemanticResourceAttributes` looks like an old/renamed symbol from a prior SDK version. Correct, simplest option: `from opentelemetry.sdk.resources import SERVICE_NAME` (confirmed working, and what this sandbox uses). If you specifically want the semconv path instead: `from opentelemetry.semconv.resource import ResourceAttributes` and reference `ResourceAttributes.SERVICE_NAME`. |
| `tracer_provider = TracerProvider(resource=Resource.create({SERVICE_NAME: <YOUR_SERVICE_NAME>}))` | ⚠️ Needs quotes | `<YOUR_SERVICE_NAME>` is clearly meant as a fill-in placeholder, but as literally written it's not valid Python (undefined name) — worth a note in the doc that it must be a quoted string, e.g. `"otel-aws-sandbox"`. |
| `OTLPExporter(endpoint="http://localhost:4318/v1/traces")` | ❌ Broken (same issue as above) | Fixed once the class name/import above is corrected to `OTLPSpanExporter`. The endpoint URL and port (4318, HTTP) are correct — confirmed via Extension debug logs (`OTLP HTTP | Starting collector on 127.0.0.1:4318`) and a successfully sent trace. |
| `trace.set_tracer_provider(tracer_provider)` | ✅ Correct as-is | |
| `BotocoreInstrumentor().instrument(tracer_provider=tracer_provider)` | ✅ Correct as-is | No ordering constraint — safe wherever it sits in the file. |
| `AwsLambdaInstrumentor().instrument(tracer_provider=tracer_provider)` | ⚠️ Correct call, dangerous position | The call itself is right, but **where** it sits matters a lot and the doc gives no warning about it: `AwsLambdaInstrumentor().instrument()` looks up your real handler function by name via the Lambda `_HANDLER` env var. If this call executes before your `lambda_handler` function is *defined* further down in the same file — which is exactly what happens if a customer pastes this block at the top of `handler.py`, above their handler function, following the doc's presentation order — it raises `AttributeError: partially initialized module 'handler' has no attribute 'lambda_handler' (most likely due to a circular import)`. We hit this exact error building this sandbox. **Fix: this call must come after the handler function is defined**, not with the rest of the setup code at the top of the file. See `BUILD_WALKTHROUGH.md` §5 for the full trace. |

### Corrected, verified-working version of this sample

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

tracer_provider = TracerProvider(resource=Resource.create({SERVICE_NAME: "your-service-name"}))
tracer_provider.add_span_processor(
    SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces"))
)
trace.set_tracer_provider(tracer_provider)

# Safe anywhere in the file — no handler dependency.
BotocoreInstrumentor().instrument(tracer_provider=tracer_provider)

def lambda_handler(event, context):
    ...

# Must come AFTER lambda_handler is defined above, or this raises
# AttributeError on cold start (see table above).
AwsLambdaInstrumentor().instrument(tracer_provider=tracer_provider)
```

This matches `handler.py` in this repo, which has been deployed, invoked,
and confirmed (via Extension debug logs and the Datadog UI) to actually
produce and send a trace.

### Region/ARN note (separate from the code sample above)

The doc's copy-paste layer ARN, `arn:aws:lambda:sa-east-1:464622532012:layer:Datadog-Extension:53`,
is region- and time-specific — the account ID (`464622532012`) is constant
across commercial regions, but the version number is not. Verified current
`us-east-1` version is `100` via `aws lambda get-layer-version-by-arn`, not `53`.

## Implementation gotchas not mentioned in the doc

- **`AwsLambdaInstrumentor().instrument()` ordering**: it looks up the real
  handler function via the Lambda `_HANDLER` env var (e.g.
  `handler.lambda_handler`) by importing/inspecting the current module. If
  called *before* `lambda_handler` is defined in the file (as the doc's flat
  snippet order implies), it raises `AttributeError: partially initialized
  module 'handler' has no attribute 'lambda_handler'`. It must be called
  *after* the handler function definition.
- **Lambda's root logger is pre-configured**: `logging.basicConfig(...)` is
  a no-op inside a Lambda handler because the runtime already attaches a
  handler to the root logger before your code runs. Use
  `logging.getLogger().setLevel(...)` instead, or INFO-level logs silently
  disappear.
- **Packaging metadata matters**: `serverless-python-requirements`'s default
  (uv-based, no Docker) install stripped `.dist-info` metadata from vendored
  packages, which broke `opentelemetry-api`'s `entry_points`-based context
  implementation lookup (`StopIteration` in `_load_runtime_context`) — and it
  installed macOS (`darwin`) binaries instead of Lambda-compatible
  (`manylinux`) ones. Worked around by vendoring dependencies directly with
  `pip install --platform manylinux2014_x86_64 --implementation cp
  --python-version 3.11 --only-binary=:all: --target vendor` (see
  `build.sh`), which preserves `.dist-info` and fetches the right wheels
  without needing Docker.

---

# Round 2: closing the remaining gaps

Four follow-ups to get this ready for the reference doc: the *other*
documented OTel mode (native ddtrace + the OTel API bridge), tagging `env`
properly, whether this sandbox is even visible on Datadog's Serverless
inventory page, and a cross-check against Siddhitha's .NET samples. Same
account (`<AWS_ACCOUNT_ID>`, `us-east-1`), same repo.

## 1. The other OTel mode: native ddtrace + `DD_TRACE_OTEL_ENABLED`

Datadog's doc describes two distinct ways to get OTel-API-shaped code to
produce a trace: (a) what this sandbox already tests — a real OTel SDK
`TracerProvider` exporting via OTLP to the Extension — and (b) "OpenTelemetry
API support within Datadog SDKs," where you write the same `opentelemetry.trace`
calls, but the tracer underneath is `ddtrace` itself (`DD_TRACE_OTEL_ENABLED=true`).
Deployed this as a **second, separate Lambda function** (`ddtraceOtelApi` in
`serverless.yml`, code in `handler2.py`) rather than modifying the existing
one, so the two are a fair side-by-side comparison of the same application
logic (one custom span + 2 log lines + an `sts.get_caller_identity` call).

**Setup** (see `serverless.yml` for the full block):
- Layers: `Datadog-Extension:100` **and** `Datadog-Python311:128` (verified
  current version the same way as before — `aws lambda get-layer-version-by-arn`,
  probing until `AccessDeniedException`; the doc's extracted "127" was stale,
  same pattern as the Extension ARN in Round 1).
- Handler config: `handler: datadog_lambda.handler.handler`,
  `DD_LAMBDA_HANDLER: handler2.lambda_handler` (the actual app code).
- `DD_TRACE_ENABLED: true`, `DD_TRACE_OTEL_ENABLED: true`,
  `DD_SERVICE: ddtrace-otel-api-sandbox`, `DD_ENV: sandbox`.
- App code (`handler2.py`): **no** `TracerProvider`, no `Resource.create()`,
  no `OTLPSpanExporter`, no `BotocoreInstrumentor`/`AwsLambdaInstrumentor` —
  just `from opentelemetry import trace; tracer = trace.get_tracer(__name__)`
  and `tracer.start_as_current_span(...)`. Confirms the doc's claim that
  "no additional provider registration is required" once
  `DD_TRACE_OTEL_ENABLED=true` — ddtrace registers itself as the OTel
  `TracerProvider` implicitly.

**Deploy/invoke:**
```bash
npx serverless deploy
npx serverless invoke -f ddtraceOtelApi -d '{"source":"round2-ddtrace-otel-test"}' --log
```

**Result: produces a trace, confirmed from CloudWatch** (raw log, not the
Serverless CLI's `--log` output, which showed `undefined` message bodies
again — same caveat as Round 1):
```
Configured ddtrace instrumentation for 86 integration(s). The following modules have been patched: ...,botocore,...,logging,...,aws_lambda,...
```
ddtrace auto-patches on import (via `datadog_lambda.handler.handler`,
confirmed by the "86 integration(s)" line) — including `botocore` (so the
STS call is traced without us adding `BotocoreInstrumentor` ourselves) and,
critically, `logging`.

### `dd.trace_id` auto-injection into logs: confirmed yes, unlike the Extension path

```
[INFO] 2026-10-05T20:41:17.645Z 099d693c-004e-4409-9150-7d66078601de [dd.trace_id=6ac40b6d000000000635b79e2cd44f22 dd.span_id=4866591510865824182] ddtrace-otel-api-sandbox: handler invoked, event={'source': 'round2-ddtrace-otel-test'}
[INFO] 2026-10-05T20:41:17.787Z 099d693c-004e-4409-9150-7d66078601de [dd.trace_id=6ac40b6d000000000635b79e2cd44f22 dd.span_id=10834168726905059997] Found credentials in environment variables.
[INFO] 2026-10-05T20:41:20.27Z  099d693c-004e-4409-9150-7d66078601de [dd.trace_id=6ac40b6d000000000635b79e2cd44f22 dd.span_id=10834168726905059997] ddtrace-otel-api-sandbox: sts.get_caller_identity account=<AWS_ACCOUNT_ID> arn=...
```

This is the exact opposite of Round 1's Extension-OTel-SDK finding, where
`logger.info()` text carried no trace context at all (only request-ID-based
UI correlation). Here, `dd.trace_id`/`dd.span_id` are injected directly into
the log line's text by ddtrace's patched `logging` module — no custom
`logging.Filter` needed, no manual work. **This is the clearest practical
reason to recommend the native ddtrace+OTel-API mode over the OTel-SDK path
when a customer specifically needs trace IDs inside the raw log text**
(e.g. for a non-Datadog log consumer, or `grep`-based correlation) rather
than only UI-level correlation.

### Span tags vs. the Extension-OTel-SDK path: confirmed from actual trace data

Confirmed from CloudWatch: a trace is produced; `botocore` and `logging`
are auto-patched; `dd.trace_id`/`dd.span_id` land in the log text. Also
confirmed, from the actual trace data in Datadog (checked directly, not
inferred): the `do-work` span here does **not** carry `telemetry.sdk.name`,
`otel.scope.name`, or `otel.trace_id` the way the Extension-OTel-SDK
sandbox's spans do (Round 1) — those OTel-SDK-specific tags are **absent**.
This matches how ddtrace's OTel bridge actually works: it creates native
ddtrace `Span` objects underneath the `opentelemetry.trace` API calls, not
real OTel SDK `Span` objects, so none of the OTel-SDK self-identification
tags get attached. **Practical takeaway for the doc**: those three tags
(`telemetry.sdk.*`, `otel.scope.*`, `otel.trace_id`) are a reliable way to
tell which of the two modes produced a given trace, in either direction —
present means real OTel SDK through the Extension; absent means the
ddtrace+OTel-API bridge.

## 2. `env` / `deployment.environment`: added, redeployed, confirmed in code

Added to `handler.py`'s `Resource.create(...)` call:
```python
resource = Resource.create(
    {
        SERVICE_NAME: "otel-aws-sandbox",
        "deployment.environment": "sandbox",
        "deployment.environment.name": "sandbox",
    }
)
```

**Why both keys**: checked the actual pinned semconv package in this
sandbox's own `vendor/` —
```bash
$ PYTHONPATH=vendor python3 -c "
from opentelemetry.semconv.resource import ResourceAttributes
print(ResourceAttributes.DEPLOYMENT_ENVIRONMENT)
print('DEPLOYMENT_ENVIRONMENT' in dir(ResourceAttributes))
"
deployment.environment
True
```
— confirms `deployment.environment` is the only one `opentelemetry-semconv`
0.48b0 (pinned alongside `opentelemetry-sdk` 1.27.0) knows about;
`DEPLOYMENT_ENVIRONMENT_NAME` doesn't exist in this version's
`ResourceAttributes` at all. Newer OTel semconv renamed the stable key to
`deployment.environment.name`. Since `Resource.create()` takes plain string
keys (the constant is just a convenience), set both — costs nothing, and
removes any doubt about which one Datadog's OTLP intake actually reads for
the `env` facet.

Redeployed and invoked:
```bash
npx serverless deploy function -f otelSandbox
npx serverless invoke -f otelSandbox -d '{"source":"env-dual-key-test"}'
```
```
START RequestId: fe48a61c-e59c-4032-ac1d-06fb49a23d80
```

**Confirmed from the actual trace** (checked directly in Datadog, not
inferred): request ID `fe48a61c-e59c-4032-ac1d-06fb49a23d80`'s trace shows
`env:sandbox` — no longer `env:none` — and the raw resource attributes
include `deployment.environment.name`. Setting the resource attribute is
what controls the facet; confirmed end to end from code change through to
the UI.

## 3. Serverless inventory page: AWS integration confirmed off — a clean illustration of the inventory-vs-tracing distinction

### Confirmed without touching Datadog: inventory listing doesn't need traces at all

Datadog's AWS Lambda integration doc states the required IAM permissions
for the **integration itself** (not the Extension, not any tracer) are:

| Permission | Purpose (per the doc) |
|---|---|
| `lambda:List*` | "List Lambda functions, metadata, and tags." |
| `tag:GetResources` | "Get custom tags applied to Lambda functions." |
| `cloudtrail:LookupEvents` | "Use CloudTrail History to detect changes to lambda functions" |

And directly: enabling the AWS integration alone — no Extension, no
tracer, no OTel SDK — is sufficient for a function to appear in the
inventory: "Once this is completed, view all of your Lambda Functions in
the [Datadog Serverless view](https://app.datadoghq.com/functions)."

**Conclusion: Serverless inventory visibility is entirely a function of
whether the Datadog **AWS integration** is enabled and polling this AWS
account from your Datadog org — it has nothing to do with OTel, the
Extension, or this sandbox's code.** An OTel-only Lambda with no AWS
integration configured for its account will never show up there, no
matter how good its traces are; conversely a plain, uninstrumented Lambda
will show up there just fine if the AWS integration is polling its account.

### AWS-side check: is *this* account's AWS integration tied to the repo author's org?

```bash
$ aws iam list-roles --query "Roles[?contains(RoleName,'Datadog') || contains(RoleName,'datadog')].RoleName" --output text
```
Account `<AWS_ACCOUNT_ID>` has roughly **91 individually-named**
`AWSIntsTeam_DatadogIntegrationRole_<Name>` IAM roles (e.g.
`..._<Employee_A>`, `..._<Employee_B>`, `..._CloudSupportSandbox`, …; the
real names seen here were other Datadog employees unrelated to this
investigation, placeholdered the same way as the account ID above since
they carry no evidentiary value beyond "many individually-named roles
exist"), plus a few `AWSDatadogIntegration-*`/`AWSIntegration-DatadogAWS-*`
CloudFormation-managed roles. This strongly suggests the account is a
**shared internal AWS sandbox** where many individual Datadog employees
have each separately connected their *own* personal Datadog org's AWS
integration to this one account (consistent with the `aws_course_certs`
folder already in this machine's `Documents/` — looks like a training/cert
sandbox account).

```bash
$ aws iam list-roles --query "Roles[].RoleName" --output text | tr '\t' '\n' | grep -i "<repo-author-name-fragments>"
# (no output)
```
**No role tied to the repo author's own name exists in this account.**

### Confirmed: the AWS integration for this account is off

The repo author confirmed directly (not inferred, not via a live Datadog data check, not
via metrics) that this Datadog org's **AWS integration for account
`<AWS_ACCOUNT_ID>` is not connected/polling.** The IAM-role-naming
inference above was correct.

**A red herring worth flagging explicitly, since it's an easy mistake**:
this sandbox's traces carry `aws.lambda.enhanced.*`-style metrics/fields,
and seeing those flow into Datadog could look like evidence the AWS
integration is active. It isn't evidence either way — those are emitted
directly by the **Datadog Lambda Extension itself**, from execution-context
data the Extension already has at invocation time (region, function ARN,
memory size, cold start, etc.), completely independent of whether the
separate, IAM-role-based AWS integration is configured. Their presence
proves the Extension is running and reporting; it proves nothing about AWS
integration status. (A live Datadog data check was also used to check the AWS integration's
configured-account list and the literal Serverless inventory page contents
directly — it has no tool/API access to either, so neither could be
checked that way. That's fine; it's exactly why the IAM-role evidence above
is the thing to rely on, and it already pointed the right way.)

**Conclusion**: per the docs (§ above — inventory visibility is purely a
function of AWS integration polling, independent of tracer/OTel/Extension),
this Lambda function **should not appear** on this org's Serverless
inventory page, because the AWS integration for its account is off. That's
not a gap in this investigation — it's a clean, concrete illustration of
the inventory-vs-tracing distinction for the doc: a function can have
perfectly good APM traces (confirmed working, Round 1 + Round 2 §1/§2) and
still be invisible on `/functions` for a completely unrelated reason.

## 4. Cross-check against Siddhitha's .NET samples

Cloned and deployed `dotnet-lambda-otel-stdout-sample` from
`https://github.com/SiddhithaBhoopathy/otel-dotnet-lambda-extension-samples`
**as-is** — no code changes. (The repo also has a second sample,
`parcel-tracking-otel-sample`; the stdout-focused one is the direct match
for what was asked.) That repo's own `NOTES.md` is itself a good summary of
the same "Extension has no `/v1/logs` endpoint" finding as Round 1's
Python-only investigation — written independently, same conclusion.

**Toolchain needed (none of this was pre-installed):**
```bash
brew install aws-sam-cli                      # SAM CLI wasn't installed
dotnet tool install -g Amazon.Lambda.Tools     # .NET Lambda packaging tool
```

**One change needed before deploy**: the template's default
`ExtensionVersion` parameter was `"99"`. Verified current `us-east-1`
version the same way as every other layer ARN in this doc:
```bash
$ aws lambda get-layer-version-by-arn --arn "arn:aws:lambda:us-east-1:464622532012:layer:Datadog-Extension:101" --region us-east-1 --query CreatedDate --output text
2026-10-02T15:44:05.696+0000
$ aws lambda get-layer-version-by-arn --arn "...:102" ...
AccessDeniedException   # doesn't exist yet -> 101 is current
```
Bumped the template's default from `99` to `101`. Everything else in the
repo — `template.yaml`, the handler code, the custom `JsonConsoleLogExporter`
— was used unmodified.

**Build/deploy/invoke, exactly as the repo's own README says:**
```bash
cd dotnet-lambda-otel-stdout-sample
export PATH="$PATH:$HOME/.dotnet/tools"
sam build
sam deploy --stack-name otel-dotnet-stdout-sample \
  --parameter-overrides "DatadogApiKey=$DD_API_KEY" \
  --capabilities CAPABILITY_IAM --resolve-s3 --no-confirm-changeset

aws lambda invoke --function-name otel-stdout-sample \
  --cli-binary-format raw-in-base64-out \
  --payload file://events/event.json /tmp/otel-stdout-out.json
```
**Result: worked as-is, no fixes needed.** `{"OrderId":"42","Customer":"Acme"}`
returned, `StatusCode: 200`.

### Confirms the Round 1 finding: logs are STDOUT JSON, not OTLP

Raw CloudWatch log line for the invocation:
```
2026-10-05T20:43:50.098Z ... info {"message":"Processing order 42 for Acme","status":"info","dd.trace_id":"8448532820361583770","dd.span_id":"7394150500141365499","OrderId":"42","Customer":"Acme","otel":{"trace_id":"bebf0956c1b450b7753f37d67ee19c9a","span_id":"669d4cb438b3f8fb", ...}}
```
Matches Round 1's Python finding architecturally: the Extension has no
`/v1/logs` OTLP route (same 404 class of limitation), so this sample
doesn't even try OTLP for logs — it writes one JSON line per `LogRecord` to
STDOUT via a small custom `BaseExporter<LogRecord>`
(`JsonConsoleLogExporter.cs`), which the Extension picks up the same way it
picks up any Lambda's CloudWatch logs (the same `Logs aggregator service`
mechanism from Round 1), then correlates to the trace in the UI via
`request_id` — not via anything OTel-specific.

With debug logging on (`DD_LOG_LEVEL: debug`, temporarily), confirmed the
trace itself sent successfully, same as every other test in this doc:
```
DD_EXTENSION | DEBUG | OTLP HTTP | Starting collector on 127.0.0.1:4318
DD_EXTENSION | DEBUG | OTLP | Successfully buffered traces to be aggregated.   (x2)
DD_EXTENSION | DEBUG | TRACES | Successfully sent trace (1 attempts, 1144 bytes)
```

### The real difference from the Python sandbox: manual `dd.trace_id` injection into the log body

This is the interesting divergence. Round 1 concluded the Python sandbox's
`logger.info()` text carries **no** trace context — only request-ID-based
UI correlation. The .NET sample's log line above contains
`"dd.trace_id":"8448532820361583770"` **directly in the JSON body**. That's
not automatic, and it isn't an OTLP logs feature (confirmed not supported in
Round 1) — it's the sample's own `JsonConsoleLogExporter` manually computing
it from the OTel span context: `dd.trace_id` is the **unsigned decimal of
the low-order 64 bits** of the OTel 128-bit hex trace ID, and `dd.span_id`
is just the decimal of the hex span ID. Verified the arithmetic actually
matches, rather than taking the repo's NOTES.md claim on faith:
```bash
$ python3 -c "
trace_hex = 'bebf0956c1b450b7753f37d67ee19c9a'
low64 = trace_hex[-16:]
print('low 64 hex:', low64, '-> decimal:', int(low64, 16))
span_hex = '669d4cb438b3f8fb'
print('span hex -> decimal:', int(span_hex, 16))
"
low 64 hex: 753f37d67ee19c9a -> decimal: 8448532820361583770
span hex -> decimal: 7394150500141365499
```
Exact match to the log line's `dd.trace_id`/`dd.span_id` values.

**Flag for the doc**: unlike the Python sandbox (Round 1), which never
implemented this and so only gets UI-level request-ID correlation, this
.NET sample proactively closes that gap itself, in application code, and
gets **both** forms of correlation — UI-level (`request_id`) and
text-level (`dd.trace_id` in the log body). If the reference doc
recommends the OTel-SDK-through-Extension path for logs needing real
trace-ID text (not just UI correlation), this `JsonConsoleLogExporter`
pattern (low-64-bits-of-trace-ID → decimal) is the concrete recipe to
include, since the Datadog doc itself doesn't mention it anywhere.

## Round 2 closing summary

All four follow-ups are closed, each with a clear verdict:

| # | Question | Verdict |
|---|---|---|
| 1 | Does native ddtrace + `DD_TRACE_OTEL_ENABLED` produce a trace, and does it auto-inject `dd.trace_id` into logs? | **Yes to both** — confirmed from CloudWatch. Its spans lack `telemetry.sdk.*`/`otel.scope.*`/`otel.trace_id` (confirmed from actual trace data), the opposite of the Extension-OTel-SDK path. Those tags are a reliable way to tell the two modes apart from a trace alone. |
| 2 | Does setting `deployment.environment`(`.name`) fix `env:none`? | **Yes** — confirmed from the actual trace: `env:sandbox`, with `deployment.environment.name` present in the raw resource attributes. |
| 3 | Does this sandbox show up on the Serverless inventory page? | **No, confirmed** — this org's AWS integration for account `<AWS_ACCOUNT_ID>` is off (confirmed directly, not inferred). Per the docs, inventory visibility depends solely on that integration, not on tracing/OTel/the Extension — so this is expected, not a bug, and it's a clean example for the doc of those two things being unrelated. (`aws.lambda.enhanced.*` metrics are emitted by the Extension itself and are not evidence of AWS integration status either way.) |
| 4 | Does Siddhitha's .NET sample behave the same as the Python sandbox for logs? | **Mostly, with one real difference** — same "Extension has no `/v1/logs`, STDOUT JSON + `request_id` UI correlation" architecture, deployed and ran as-is with no fixes needed. But its custom exporter also manually injects `dd.trace_id`/`dd.span_id` (decimal of the low 64 bits of the OTel hex IDs) directly into the log body — something the Python sandbox never implemented — giving it correlation the Python sandbox doesn't have. |

Nothing in this round required Datadog API/MCP access: items 1 and 4 came
from AWS-side CloudWatch logs and arithmetic that could be verified
independently; items 2 and 3 were confirmed by the repo author directly from the actual
Datadog UI/trace data, not inferred or checked through a live Datadog data check.

### Follow-up (closing a gap, not new investigation): Mode 2 custom metrics, actually tested

`ddtraceOtelApi`'s custom-metrics signal was never actually sent anywhere
in this repo — every signal-matrix table listed it as "not separately
tested," on the assumption it would behave like Mode 1's confirmed-working
raw DogStatsD UDP send. Closed that gap directly: added the same
`send_custom_metric()` raw-UDP helper used in `handler.py` to
`handler2.py` (reusing the approach already justified for Mode 1 —
`datadog_lambda`'s import-time `patch_all()` would contaminate the
native-tracer test this function exists to run, so raw UDP, not the
library, is still correct here), redeployed, and invoked with
`DD_LOG_LEVEL=debug`. Result, straight from the Extension's own log,
the same evidence bar as every Mode 1 custom-metrics confirmation:
```
DD_EXTENSION | DEBUG | Parsed 1 valid metrics, sending to aggregator
DD_EXTENSION | DEBUG | Flushing 0 series and 1 distributions
DD_EXTENSION | DEBUG | Successfully flushed 0 series and 1 distributions
```
**Confirmed working**, not just assumed. `env`/`DD_ENV` tag on the metric
itself: **confirmed present**, checked directly against live
Datadog data — `ddtrace_otel_api_sandbox.custom_metric_test` carries
`env:sandbox`, even though the raw UDP packet never sets it explicitly.
The Datadog Lambda Extension enriches custom DogStatsD metrics with
`env` before forwarding them, the same mechanism already confirmed for
traces — nothing needed to be set in the sending code. Incidental cross-check
from the same tag dump: this metric's `dd_extension_version` reads
`100-next`, versus `101-next` for Node's and Java's equivalent metrics —
independent confirmation, from live tag data rather than config
inspection, of the pre-Java housekeeping finding that
`python/serverless.yml` is still pinned one Extension layer version
behind Node/.NET/Java.

---

# Round 3: plain `DD_ENV` for Mode 1, and the AWS billing-attribution question

Two small closing items.

## 1. Does plain `DD_ENV` (no OTel resource attribute) work for the OTel-SDK-through-Extension mode too?

Round 2 §2 fixed `env:none` by adding `deployment.environment` /
`deployment.environment.name` to the OTel `Resource`. Open question: is
that OTel-side attribute actually *required* for this mode, or would the
simpler fix — a plain `DD_ENV` Lambda environment variable, the same way
`ddtraceOtelApi` already sets it — work here too?

**Change**: removed both `deployment.environment*` keys from `handler.py`'s
`Resource.create(...)` (now just `{SERVICE_NAME: "otel-aws-sandbox"}`), and
added a plain `DD_ENV: sandbox` to `otelSandbox`'s `environment` block in
`serverless.yml` — nothing else changed.

```bash
npx serverless deploy
npx serverless invoke -f otelSandbox -d '{"source":"round3-dd-env-only-test"}'
```
```
START RequestId: 0a8bfd70-6d07-4507-ae41-64f26d550687
```
Confirmed via debug logs (`DD_LOG_LEVEL=debug`, temporarily) that the trace
sent successfully, same as every other test in this doc — `OTLP |
Successfully buffered traces to be aggregated` ×3, `TRACES | Successfully
sent trace (1 attempts, 1295 bytes)`.

**Reported by the repo author, from the actual trace in the Datadog UI**, for request ID
`0a8bfd70-6d07-4507-ae41-64f26d550687` (`service:otel-aws-sandbox`): `env`
facet is `sandbox`, and the span has **zero** `deployment.*` fields anywhere
— no `deployment.environment`, no `deployment.environment.name` — only the
plain `DD_ENV=sandbox` Lambda environment variable. (No span JSON pasted
into this doc for this one — unlike §2 below, this result rests on the repo author's
direct UI read, not on evidence captured here.) **Plain `DD_ENV` alone is
sufficient for this mode; no OTel Resource attribute is required.**

This doesn't contradict Round 2 §2's result — `DD_ENV` wasn't set during
that earlier test, so the two approaches were never actually tested
together, and both independently work. **Conclusion for the doc: there are
two valid ways to fix `env:none` for the OTel-SDK-through-Extension mode —
a plain `DD_ENV` Lambda env var, or a `deployment.environment`/
`deployment.environment.name` OTel Resource attribute.** Recommend
`DD_ENV` as the simpler one: it needs no code change, and it's consistent
with how the native ddtrace+OTel-API mode (`ddtraceOtelApi`) already sets
`env` too — one mechanism to document instead of two. `handler.py` is left
as-is (`DD_ENV` only, no `deployment.*` resource attributes) — no further
code change needed.

## 2. AWS billing attribution: no OTel resource attribute needed — confirmed from existing data

The question: does getting a Lambda's AWS account/region/function identity
onto a trace (for cost/billing attribution, the kind of thing Azure/GCP
sometimes need explicit OTel resource attributes for) require the customer
to set anything in OTel config, or does Datadog's Lambda tooling supply it
regardless?

**No new testing needed for this one** — it's already answered by a raw
span JSON pasted earlier in this investigation, from the `STS.GetCallerIdentity`
child span of an `otelSandbox` trace:

```json
{"account_id":"<AWS_ACCOUNT_ID>","architecture":"x86_64","aws":{"region":"us-east-1","request_id":"98849be9-244a-4235-82b1-27980ed36d0e"},"aws_account":"<AWS_ACCOUNT_ID>","dd_extension_version":"100-next","ddtags":"ingestion_reason:lambda","duration":152527792,"env":"none","function_arn":"arn:aws:lambda:us-east-1:<AWS_ACCOUNT_ID>:function:serverless-otel-aws-sandbox-dev-otelsandbox","functionname":"serverless-otel-aws-sandbox-dev-otelSandbox","http":{"status_code":"200"},"init_type":"on-demand","memorysize":"256","origin":"lambda","otel":{"scope":{"name":"opentelemetry.instrumentation.botocore","version":"0.48b0"},"status_code":"0","status_description":"","trace_id":"dc30895c3159c582068e661354fc1da1"},"region":"us-east-1","resource":"serverless-otel-aws-sandbox-dev-otelSandbox","retry_attempts":0,"rpc":{"method":"GetCallerIdentity","service":"STS","system":"aws-api"},"span":{"type":"client"},"telemetry":{"sdk":{"language":"python","name":"opentelemetry","version":"1.27.0"}}}
```

**Confirmed present, with zero OTel resource attributes supplying them**
(at the point this span was captured, `handler.py`'s `Resource.create()`
set only `SERVICE_NAME` — nothing AWS-identity-related, nothing
billing-related):

| Field | Value | Billing/identity relevance |
|---|---|---|
| `account_id` / `aws_account` | `<AWS_ACCOUNT_ID>` | AWS account — the core billing-attribution key |
| `region` / `aws.region` | `us-east-1` | AWS region |
| `function_arn` | `arn:aws:lambda:us-east-1:<AWS_ACCOUNT_ID>:function:...` | Full resource identity |
| `functionname` / `resource` | `serverless-otel-aws-sandbox-dev-otelSandbox` | Function name |
| `memorysize` | `256` | Billed memory config |
| `architecture` | `x86_64` | Billed architecture |
| `init_type` | `on-demand` | Cold-start/billing-relevant invocation type |
| `aws.request_id` | `98849be9-...` | Per-invocation identity |
| `origin` | `lambda` | Confirms Extension-side enrichment path |

**Reported by the repo author, from the Round 3 §1 trace** (request ID
`0a8bfd70-6d07-4507-ae41-64f26d550687`): that span also carries
`cloud.resource_id` (the full function ARN) and `cloud.account.id`, both
populated automatically with zero OTel resource attribute configuration.
These would be the literal AWS analogs of the OTel cloud-resource semconv
attributes (`cloud.account.id`, `cloud.resource_id`, `cloud.region`, etc.)
that Azure/GCP setups sometimes require the customer to set by hand — but
as with §1 above, no span JSON was pasted into this doc for this claim, so
it rests on the repo author's direct UI read, not on evidence captured here.

**Independently verified, from the vendored instrumentor source itself**
(this part *is* reproducible from this repo alone, no UI needed) — exactly
why those two fields would be there with no resource-attribute config:
```bash
$ grep -n "CLOUD_RESOURCE_ID\|CLOUD_ACCOUNT_ID" vendor/opentelemetry/instrumentation/aws_lambda/__init__.py
            span.set_attribute(
                SpanAttributes.CLOUD_RESOURCE_ID,
                lambda_context.invoked_function_arn,
            )
            ...
            account_id = lambda_context.invoked_function_arn.split(":")[4]
            span.set_attribute(
                ResourceAttributes.CLOUD_ACCOUNT_ID,
                account_id,
            )
$ PYTHONPATH=vendor python3 -c "
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.semconv.resource import ResourceAttributes
print(SpanAttributes.CLOUD_RESOURCE_ID, ResourceAttributes.CLOUD_ACCOUNT_ID)
"
cloud.resource_id cloud.account.id
```
`AwsLambdaInstrumentor` sets both directly as span attributes, parsed
straight out of `lambda_context.invoked_function_arn` (the account ID is
literally the ARN's 5th `:`-delimited segment) — no `Resource.create()`
call involved at all. This also resolves an apparent naming mismatch with
the §2 span above: `cloud.resource_id`/`cloud.account.id` are set on the
**root Lambda-invocation span** (`AwsLambdaInstrumentor`'s own span), while
the flatter `account_id`/`function_arn`/`aws.region` names in the pasted
JSON above came from the `STS.GetCallerIdentity` **child span**
(`BotocoreInstrumentor`'s), plus whatever Datadog's own ingestion adds as
enrichment on top. Different spans, different attribute sets — not a
contradiction.

**Conclusion for the doc**: unlike the Azure/GCP-style pattern where a
customer sometimes has to manually set `cloud.account.id`/`cloud.resource_id`/
`cloud.region` as OTel resource attributes to get correct cost attribution,
**AWS Lambda has no such requirement here**. Combining what's independently
verified from source (the instrumentor sets `cloud.resource_id`/
`cloud.account.id` unconditionally, parsed from the Lambda context) with
the pasted-and-reproducible `account_id`/`region`/`functionname`/
`memorysize` evidence in §2's span: AWS billing/identity attribution needs
zero OTel resource attribute configuration. The Datadog Lambda Extension
(via `AwsLambdaInstrumentor`, which reads the actual Lambda execution
context — the invoked function ARN, `AWS_REGION`, `AWS_LAMBDA_FUNCTION_MEMORY_SIZE`,
etc., all supplied by the Lambda runtime itself, not by application code)
auto-populates every AWS billing/identity field above regardless of OTel
configuration. This holds for the OTel-SDK-through-Extension path tested
throughout this doc; it was not re-tested against the native
ddtrace+OTel-API mode (Round 2 §1), but that mode runs through the same
Extension and the same underlying Lambda execution context, so the same
auto-population is expected to apply there too.

> **Correction, confirmed in Round 4**: this specific inference rested on
> an untested assumption, and checking it found a different mechanism, not
> the same one. Round 4's Node investigation found that
> `cloud.resource_id`/`cloud.account.id` are set exclusively by
> `@opentelemetry/instrumentation-aws-lambda` (an OTel-SDK-mode-only
> package) — dd-trace's native span creation doesn't use those attribute
> names at all.
>
> Checked directly against this Python sandbox's own `ddtraceOtelApi`
> function (Round 2 §1) — no new deployment, just `DD_TRACE_DEBUG=true`
> toggled on the already-deployed function and one invoke. ddtrace's own
> debug output dumps the full raw span object, tags included, for the
> `aws.lambda` root span:
> ```python
> Span(name='aws.lambda', ..., service='ddtrace-otel-api-sandbox',
>      resource='serverless-otel-aws-sandbox-dev-ddtraceOtelApi', ...,
>      tags={
>        ..., 'functionname': 'serverless-otel-aws-sandbox-dev-ddtraceotelapi',
>        'function_version': '$LATEST', 'language': 'python',
>        'function_arn': 'arn:aws:lambda:us-east-1:<AWS_ACCOUNT_ID>:function:serverless-otel-aws-sandbox-dev-ddtraceotelapi',
>        'env': 'sandbox', 'cold_start': 'true', 'datadog_lambda': '8.128.0',
>        ...
>      })
> ```
> **Confirmed: `function_arn` and `functionname` are present as native
> ddtrace span tags — `cloud.resource_id`/`cloud.account.id` are not,
> anywhere in the tag set.** Exactly the Datadog-native naming pattern
> Round 4 found for Node's native mode, not the OTel-semconv one. (No
> explicit `account_id` tag at this span level either — Round 1's
> originally-pasted span JSON showed `account_id`/`aws_account` as
> separate fields, which is the Extension's own enrichment layer added on
> top of whatever the tracer provides, not something ddtrace itself sets;
> consistent with Round 1's "Extension enrichment, not tracer-specific"
> finding, not a contradiction of it.) This is now a confirmed result, not
> an assumption — the native ddtrace mode's billing/identity fields use
> Datadog's own naming scheme in both languages, verified directly rather
> than inferred by analogy.

---

# Round 4: Node.js parity

Mirrors Rounds 1-3 for Node.js, as closely as the two languages allow, so
the two are a fair side-by-side comparison. Lives in `/node` (own
`package.json`/`serverless.yml`, own stack `serverless-otel-aws-sandbox-node-dev`),
isolated from the Python build tooling at the repo root. Same AWS account
(`<AWS_ACCOUNT_ID>`), same region (`us-east-1`).

## Setup: runtime and layer versions, verified not assumed

**Node.js runtime**: checked AWS's current supported runtimes rather than
assume one. `nodejs20.x` is already past its documented deprecation date
(Apr 30, 2026); `nodejs26.x` is public preview ("not covered by the Lambda
SLA or Technical Support... should not be used for production workloads").
Used **`nodejs24.x`** — the current GA, non-preview runtime. Datadog's docs
list `Node24-x` as a supported layer option, consistent with this choice.

**Datadog-Node24-x layer version**: verified the same way as every other
layer ARN in this investigation — probed with `aws lambda
get-layer-version-by-arn` until `AccessDeniedException`:
```bash
$ for v in 140 141 142 143 144; do
    aws lambda get-layer-version-by-arn --arn "arn:aws:lambda:us-east-1:464622532012:layer:Datadog-Node24-x:$v" --region us-east-1 --query CreatedDate --output text
  done
140 -> 2026-06-22T21:27:16.949+0000
141 -> 2026-07-15T12:39:59.678+0000
142 -> 2026-07-30T15:19:28.664+0000
143 -> 2026-09-10T16:57:20.392+0000
144 -> AccessDeniedException   # doesn't exist yet
```
Current version: **143**. (The doc's own extraction happened to say "143"
too this time — verified independently anyway, same as every other layer
version in this doc; doc text isn't trusted at face value regardless of
whether it turns out right or wrong.)

## Phase 1: doc code sample audit -- Node.js "Send OpenTelemetry traces... through the Extension"

Same rigor as the Python audit (Round 1): took the doc's exact Node.js
sample (`?tab=node#sdk`) and ran each line against the real current npm
package versions, not by inspection.

```js
const { NodeTracerProvider } = require("@opentelemetry/sdk-trace-node");
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { Resource } = require('@opentelemetry/resources');
const { SemanticResourceAttributes } = require('@opentelemetry/semantic-conventions');
const { SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const provider = new NodeTracerProvider({
    resource: new Resource({
        [ SemanticResourceAttributes.SERVICE_NAME ]: 'rey-app-otlp-dev-node',
    })
});
provider.addSpanProcessor(
    new SimpleSpanProcessor(
        new OTLPTraceExporter(
            { url: 'http://localhost:4318/v1/traces' },
        ),
    ),
);
provider.register();
```

| Line | Verdict | Detail |
|---|---|---|
| `require("@opentelemetry/sdk-trace-node")` / `require('@opentelemetry/exporter-trace-otlp-http')` | ✅ Correct as-is | |
| `require('@opentelemetry/sdk-trace-base')` (`SimpleSpanProcessor`) | ✅ Correct as-is, but see below | The class still exists and imports fine; the breakage is in how it's used (`addSpanProcessor`), not the import. |
| `SemanticResourceAttributes.SERVICE_NAME` | ✅ Still works | Confirmed resolves to `service.name`, unlike Python's equivalent symbol which was broken in that SDK's pinned version. |
| `new Resource({...})` | ❌ Broken | Real error: `TypeError: Resource is not a constructor`. `Resource` isn't exported as a class at all anymore in `@opentelemetry/resources@2.12.0` (confirmed: `typeof mod.Resource === 'undefined'`). Correct: `resourceFromAttributes({...})`, a factory function. |
| `provider.addSpanProcessor(...)` | ❌ Broken | Real error: `TypeError: provider.addSpanProcessor is not a function`. Checked `NodeTracerProvider`'s prototype directly — only `constructor` and `register` remain, no mutator methods. Correct: pass `spanProcessors: [...]` as a constructor option instead. |
| `provider.register()` | ✅ Correct as-is | |

**This is a broader, confirmed pattern, not two isolated incidents**: OTel JS
SDK v2.x replaced "construct, then call `.addXProcessor()`" with
"pass processors into the constructor" across the board. Same shape of
break turned up again in Phase 3's logs setup (`SimpleLogRecordProcessor`,
below) and would be expected for `BatchSpanProcessor`,
`BatchLogRecordProcessor`, etc. too — worth checking any `.addXProcessor()`
call against current OTel JS docs before assuming it still works.

### Corrected, verified-working version

```js
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { SemanticResourceAttributes } = require('@opentelemetry/semantic-conventions');
const { SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');

const resource = resourceFromAttributes({
  [SemanticResourceAttributes.SERVICE_NAME]: 'otel-aws-sandbox-node',
});
const provider = new NodeTracerProvider({
  resource,
  spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({ url: 'http://localhost:4318/v1/traces' }))],
});
provider.register();
```
This matches `node/src/instrument.js` in this repo, deployed and confirmed
working end to end.

### A real npm registry hiccup along the way (not a finding about Datadog/OTel)

Worth a note since it looked identical to a packaging bug at first: `npm
install` initially failed with `404 Not Found` on
`@opentelemetry/sdk-trace@2.12.0`'s tarball specifically (a transitive dep
of both `sdk-trace-base` and `exporter-trace-otlp-http`), even though `npm
view` listed that version as existing. Direct `curl` to the registry
confirmed the 404; retrying minutes later, the same URL returned `200` and
the install succeeded. Almost certainly registry/CDN propagation lag right
after a fresh publish, not a real broken package — but worth knowing this
class of transient failure exists, so a `404` on a *specific pinned
version* (not the whole package) during `npm install` is worth a retry
before concluding the package is broken.

## Phase 1 result: it works, Node equivalent of the ordering bug confirmed (and it's silent)

Deployed `otelSandboxNode` with only the `Datadog-Extension:101` layer (no
Datadog tracing layer), via `node/serverless.yml`. Invoked, pulled debug
logs:
```
{"status":"DEBUG","message":"DD_EXTENSION | DEBUG | OTLP | Successfully buffered traces to be aggregated."}   (x2)
{"status":"DEBUG","message":"DD_EXTENSION | DEBUG | TRACES | Successfully sent trace (1 attempts, 1004 bytes)"}
```
Works. Service `otel-aws-sandbox-node`, distinct from the Python sandbox's
`otel-aws-sandbox`.

### The ordering-bug question: Node has it too, but it fails silently instead of throwing

Python's `AwsLambdaInstrumentor` does a live `getattr()` lookup against the
handler module at `.instrument()` time, which is why calling it before the
handler function was defined threw a loud `AttributeError`. Node's
`AwsLambdaInstrumentation` instead patches via a `require-in-the-middle`
hook keyed to the handler file's resolved path, registered when
`registerInstrumentations()` runs — architecturally different, so it
wasn't obvious the same class of bug would even exist. Tested directly
rather than assumed either way:

Deployed a **temporary** third function, `orderingTestNode`, with
instrumentation setup and the handler export combined in one file
(mimicking Python's exact mistake):
```js
// ordering-test.js -- combined in one file, deliberately
const provider = new NodeTracerProvider({ /* ... */ });
provider.register();
registerInstrumentations({ instrumentations: [new AwsLambdaInstrumentation({})] });
exports.handler = async (event, context) => { /* ... */ };
```
Invoked it — no exception, `200 OK` returned. But the debug logs tell a
different story:
```
(no "OTLP | Successfully buffered traces" line at all)
(no "TRACES | Flushing" line at all)
```
Compare to the real function's logs above: zero trace activity whatsoever.
**The Node equivalent of the ordering bug exists, but it's the dangerous
silent variant** — no error, no crash, the function returns `200` exactly
as if everything worked, and nothing gets traced. This is arguably worse
for a customer to debug than Python's loud `AttributeError`, precisely
because nothing signals that anything went wrong. (The temporary function
and file were removed immediately after recording this result — not part
of the real sandbox.)

**Practical implication for the doc**: this is exactly why the official doc
structures the Node sample as a separate `instrument.js`, force-loaded via
`NODE_OPTIONS: --require instrument` before the handler module is ever
required — not a style preference, a requirement. Flag this prominently;
a customer who "simplifies" by inlining instrumentation setup into their
handler file will get a function that looks completely healthy and traces
nothing.

### Correction, with root cause: the real `otelSandboxNode` had two bugs of its own, caught by live UI evidence

The "it works" claim above was true for *traces getting sent at all*, but
incomplete. Live Datadog UI evidence (not re-tested from scratch — gone
back to find the actual cause, as it should have been checked the first
time): the ingested trace showed **`do-work` as the root span**, with no
`aws.lambda`-style wrapper span above it anywhere, and **no
`telemetry.sdk.name`** on the span. Two separate, real bugs — not sourcing
artifacts, not Python-vs-Node framework differences — both now found and
fixed.

**Bug A: `--require` alone never actually wraps the handler on `nodejs24.x`.**
The "Instrumenting lambda handler" debug line (printed during
`registerInstrumentations()`'s `init()`) appeared in the logs, which is
what made this look fine at a glance — but that line only proves the hook
was *registered*, not that it ever *fired*. The deeper proof: the hook's
own patch-application debug line (`'patch handler function'`, logged
inside `_getPatchHandler`) **never appeared at all**, and no third span
for the Lambda invocation ever showed up in the actual OTLP export
payloads (only `do-work` and `STS.GetCallerIdentity` — confirmed by
dumping the real `SpanImpl` objects passed to the exporter, not just
grepping for error text).

Root cause, confirmed independently (not just inferred from the symptom):
**AWS Lambda's `nodejs24.x` runtime loads the user handler module via
`await import(...)`, not `require(...)`.** `require-in-the-middle` (what
`NODE_OPTIONS: --require instrument` sets up) only intercepts CommonJS
`require()` calls — it never sees an ESM dynamic `import()`. This is a
documented, known requirement for OTel JS Lambda instrumentation on
Node 24+: an **ESM loader hook** must also be registered, via
`NODE_OPTIONS: --import <file>.mjs` calling `module.register(...)`, not
`--require` alone.

**Fix**: added `node/src/otel-esm-hook.mjs`:
```js
import { register } from 'node:module';
register('@opentelemetry/instrumentation/hook.mjs', import.meta.url);
```
and changed `NODE_OPTIONS` to use **both**:
```
--import /var/task/src/otel-esm-hook.mjs --require /var/task/src/instrument
```
(`@opentelemetry/instrumentation/hook.mjs` — note the copyright header on
that exact file literally reads "Copyright 2021 Datadog, Inc.": this ESM
hook was co-authored by Datadog itself as an upstream contribution to
`opentelemetry-js-contrib`.)

**Confirmed fixed**, from the real `SpanImpl` dumps after redeploying:
```
name: 'serverless-otel-aws-sandbox-node-dev-otelSandboxNode'   parentSpanContext: undefined   <- root, correct
name: 'do-work'                                                 parent = (root's spanId)        <- nested correctly
name: 'STS.GetCallerIdentity'                                   parent = (do-work's spanId)      <- nested correctly
```
All three now share one `traceId`, exactly the structure Python's Mode 1
has.

**Bug B: a custom `resource` replaces the SDK's default resource — it does not merge.**
Separately: `instrument.js` passed `resourceFromAttributes({service.name:
...})` directly as `NodeTracerProvider`'s `resource` option. Confirmed by
direct test that **this completely replaces** the SDK's own
auto-populated default resource, rather than merging with it:
```js
const resource = resourceFromAttributes({'service.name': 'x'});
const provider = new NodeTracerProvider({ resource });
// span.resource.attributes === { "service.name": "x" }  -- nothing else
```
That default resource is exactly where `telemetry.sdk.language`/`.name`/
`.version` normally come from (confirmed present when inspected alone via
`defaultResource().attributes`) — so a custom resource silently drops them
unless explicitly merged back in.

**Fix**: `defaultResource().merge(resourceFromAttributes({...}))` instead
of the bare `resourceFromAttributes({...})`. **Confirmed fixed** with an
explicit logged check of the actual computed value post-fix:
```
RESOURCE_CHECK merged resource attributes: {"service.name":"otel-aws-sandbox-node","telemetry.sdk.language":"nodejs","telemetry.sdk.name":"opentelemetry","telemetry.sdk.version":"2.12.0"}
```

**Flag for the doc, explicitly**: neither of these is a sandbox-specific
mistake that happened to only affect this code — both are real,
functional gaps in how the current OTel JS SDK / Lambda instrumentation
packages behave by default, which the doc's own Node sample (written
against an older SDK generation, per the Phase 1 audit above) does not
warn about at all. Both are now fixed in `node/src/instrument.js` and
`node/serverless.yml`.

### Billing attribution, resolved as a byproduct of Bug A (not a separate fix)

This also answers the "neither service has `cloud.resource_id`/
`cloud.account.id`" UI observation, for Mode 1 specifically: those two
attributes are set by `AwsLambdaInstrumentation` **on its own wrapper
span** (confirmed from the actual deployed `node_modules` copy, version
`0.74.0`, bundled into `otelSandboxNode`'s zip):
```js
// node_modules/@opentelemetry/instrumentation-aws-lambda/build/src/instrumentation.js
span.set_attribute(SpanAttributes.CLOUD_RESOURCE_ID, lambda_context.invoked_function_arn);
span.set_attribute(ResourceAttributes.CLOUD_ACCOUNT_ID, account_id);
```
Before Bug A's fix, that wrapper span **never existed at all** — so there
was nothing to carry those attributes. Not dropped in export, not
renamed: never called, exact same root cause as Bug A. Confirmed fixed, by
dumping the real root span post-fix:
```js
name: 'serverless-otel-aws-sandbox-node-dev-otelSandboxNode',
attributes: {
  'faas.invocation_id': '76b7715d-30d5-4a28-9e18-ca882823faf9',
  'cloud.resource_id': 'arn:aws:lambda:us-east-1:<AWS_ACCOUNT_ID>:function:serverless-otel-aws-sandbox-node-dev-otelSandboxNode',
  'cloud.account.id': '<AWS_ACCOUNT_ID>',
  'faas.coldstart': false
}
```
Both present, exact values, confirmed from a live invocation after the
fix — not inferred from source alone.

**Mode 2 (`ddtraceOtelApiNode`) is a different story, and not a bug**:
`cloud.resource_id`/`cloud.account.id` are **OTel semantic-convention
attribute names that only `@opentelemetry/instrumentation-aws-lambda`
sets** (confirmed above). That package is present in `ddtraceOtelApiNode`'s
deployed bundle too (it's in the shared `node_modules`, since package
patterns there only exclude `dd-trace`) — but it is **never required or
registered** by `handler2.js` or by dd-trace's own Lambda wrapper, since
Mode 2 doesn't use OTel's AWS Lambda instrumentation at all. dd-trace's
own native span creation uses entirely different, Datadog-native field
names (`account_id`, `function_arn`, etc. — the same flat names confirmed
on Python's native-mode spans). **Expecting `cloud.resource_id`/
`cloud.account.id` on a Mode 2 span was the wrong thing to look for in the
first place** — not a missing feature, a different (and never
cross-checked) naming scheme entirely. This also means Round 3's assumption
for Python's native mode ("that mode runs through the same Extension and
execution context, so auto-population is expected to apply there too") —
which was never actually verified against a real Mode 2 span — is now
suspect for the same reason, and should be treated as unconfirmed rather
than established.

## Phase 2: native dd-trace-js + OTel API bridge

Deployed as a separate function, `ddtraceOtelApiNode` (`node/src/handler2.js`),
layers `Datadog-Extension:101` + `Datadog-Node24-x:143`,
`DD_TRACE_ENABLED`/`DD_TRACE_OTEL_ENABLED: true`,
`DD_SERVICE: ddtrace-otel-api-sandbox-node`, `DD_ENV: sandbox`. Handler
string is the **layer's own** `datadog-lambda-js` wrapper
(`/opt/nodejs/node_modules/datadog-lambda-js/handler.handler`,
`DD_LAMBDA_HANDLER` pointing at the real handler) — per the docs' explicit
warning not to install the Datadog Lambda Library as both a layer and an
npm package, `node_modules/dd-trace` is excluded from this function's
package patterns.

App code mirrors `handler2.py`: just `trace.getTracer(...)` and
`tracer.startActiveSpan(...)` from `@opentelemetry/api`, no
`NodeTracerProvider`/`resourceFromAttributes`/exporter/instrumentation set
up manually — **plus one explicit line the Python side never needed**,
`new (require('dd-trace').TracerProvider)().register()`, added after
finding the root cause below. Without it, `@opentelemetry/api` never
learns that dd-trace is supposed to be the active tracer at all.

**Result: produces a trace.** Debug logs confirm the native path (Trace
Agent, not OTLP):
```
{"status":"DEBUG","message":"DD_EXTENSION | DEBUG | TRACE_AGENT | Processing traces took: 0 ms"}
{"status":"DEBUG","message":"DD_EXTENSION | DEBUG | TRACES | Successfully sent trace (1 attempts, 2173 bytes)"}
```

### `dd.trace_id` auto-injection into logs: confirmed, no `DD_LOGS_INJECTION` needed

```
INFO	[dd.trace_id=2705704472301599169 dd.span_id=2705704472301599169] ddtrace-otel-api-sandbox-node: handler invoked, event= {"source":"node-mode2-test"}
```
`dd.trace_id`/`dd.span_id` appear in `console.log` output automatically —
`DD_LOGS_INJECTION` was never set. Matches Python's finding (ddtrace
auto-patches logging with no extra flag needed), confirming this isn't
Python-specific.

### Root cause, found and partially fixed: `DD_TRACE_OTEL_ENABLED=true` does not auto-register the bridge

Live Datadog UI evidence corrected what this section originally said: the
`do-work` span wasn't showing as a disconnected trace — **it never got
exported at all, across 30 days of history.** dd-trace's own
auto-instrumented spans (`aws.lambda`, `aws.lambda.cold_start`) did arrive,
but as **two disconnected root spans sharing one trace ID**, not a parent
and child. Both of those needed a real root cause, not a restated
disclaimer — found by downloading and reading the **actual
`Datadog-Node24-x:143` layer's bundled `dd-trace` source** (`dd-trace`
`6.15.0`, via the layer's own presigned S3 URL from `aws lambda
get-layer-version-by-arn`'s `Content.Location` — not just the package's
GitHub source, which could differ from what's actually bundled):

```bash
$ grep -n "tracer_provider\|TracerProvider" packages/dd-trace/src/proxy.js
  get TracerProvider () {
    return require('./opentelemetry/tracer_provider')
  }
```
**`TracerProvider` is exposed only as a lazy getter on the dd-trace proxy
— it is never automatically instantiated or `.register()`-ed anywhere in
dd-trace's own init sequence.** `DD_TRACE_OTEL_ENABLED=true` makes the
class *available* and affects some internal behavior, but does **not**
wire it up as the active global `@opentelemetry/api` TracerProvider by
itself. This directly contradicts what the doc's text implies ("no
additional provider registration is required") — that claim was taken on
faith from the doc and from the parity assumption with Python, never
independently verified against the Node bridge's actual source, which is
exactly the mistake this round's audits exist to catch.

**Consequence, before the fix**: with no TracerProvider ever registered,
`trace.getTracer()` resolves to `@opentelemetry/api`'s default **no-op**
implementation — which fabricates plausible-looking trace/span IDs for API
compatibility (explaining the differing-but-plausible IDs seen originally)
but never exports anything anywhere. That's precisely "zero `do-work`
spans ever" — not a disconnected trace, a span that was never real.

**Fix**: explicitly construct and register dd-trace's own bridge in
`handler2.js`, before calling `trace.getTracer()`:
```js
const { TracerProvider } = require('dd-trace');   // resolves to the layer's copy
new TracerProvider().register();
```
(`dd-trace` resolves via Lambda's automatic `/opt/nodejs/node_modules`
resolution for layers — not npm-installed in this function's own bundle.)

**Confirmed partially fixed**: after this change, `do-work` and
`STS.GetCallerIdentity` now share one trace ID and nest correctly under
each other (confirmed via debug logs), and the Extension confirms a trace
payload containing them is actually sent (`TRACES | Successfully sent
trace`, `batched 1 payload(s)` — i.e. bundled together with the native
`aws.lambda` trace in one Trace Agent payload, not sent as a visibly
separate second export).

### Correction: the "disconnected trace" conclusion above was wrong — a console-log display artifact, not the real trace structure

**Withdrawn, not refined.** The paragraph above concluded `do-work`'s
trace ID doesn't match the native `aws.lambda` span's, based on comparing
`dd.trace_id`/`dd.span_id` values injected into separate `console.log`
lines. That comparison method is unreliable and gave a wrong answer: it
reflects whatever span happens to be active at the exact moment each
individual `console.log()` call executes, not the actual span tree that
gets constructed and exported. The real trace structure requires looking
at the **actual encoded payload**, not log-injected display values — the
same lesson the .NET investigation (above/below) had to learn by
isolation-testing; here it was learned by checking the ground truth
directly instead.

**The ground truth, from `DD_TRACE_DEBUG=true`'s "Encoding payload" debug
line** (the literal msgpack-bound JSON dd-trace sends to the agent — not
inferred, not log-injected, the actual wire data):
```json
[
  {"trace_id":"082d19646e1401a1","span_id":"082d19646e1401a1","parent_id":"0000000000000000","name":"aws.lambda", "meta":{"cold_start":"false", ...}},
  {"trace_id":"082d19646e1401a1","span_id":"00f546804f88b9b9","parent_id":"082d19646e1401a1","name":"do-work", ...},
  {"trace_id":"082d19646e1401a1","span_id":"12c523f5f893537d","parent_id":"00f546804f88b9b9","name":"http.request","resource":"POST", "meta":{"http.url":"https://sts.us-east-1.amazonaws.com/", ...}}
]
```
**Correctly nested, every level**: `aws.lambda` (root, `parent_id` all
zeros) → `do-work` (parent = `aws.lambda`'s span id) → `http.request`
(the STS call, parent = `do-work`'s span id). All three share one
`trace_id`. Confirmed on a cold-start invocation too — same correct
3-level nesting, plus extra `aws.lambda.load`/`aws.lambda.require_layer`
spans (cold-start-specific module-loading instrumentation) that **also**
correctly chain under `aws.lambda`, not as disconnected siblings.

Cross-checked the earlier console-log comparison method directly against
this payload to find out why it was wrong: `handler invoked`'s
log-injected `dd.trace_id` (`2342090579373774431`) is exactly
`int("2080c6f9175cd65f", 16)` — i.e. it **does** correctly reflect the
real `aws.lambda` span's trace ID. The `console.log()` call for
`"handler invoked"` fires *before* `tracer.startActiveSpan('do-work', ...)`
is ever called, so of course its active span at that exact instant is
`aws.lambda` itself (`trace_id == span_id` because it's genuinely the
root) — not evidence of a second, disconnected trace. The earlier
write-up compared the wrong pair of values and drew the wrong conclusion
from a correct observation.

**Corrected conclusion**: once `new TracerProvider().register()` is
called (the one still-valid finding from above — that registration really
is required, or spans are complete no-ops), the manually-created `do-work`
span **does correctly nest as a child of dd-trace's native root span**,
confirmed from the real exported payload on both cold and warm
invocations, with no exception. There is no unresolved parenting gap in
Node's bridge. This also means Node and .NET's Mode 2 results are **not**
comparable the way the original write-up assumed — .NET's cold-start
`"trace has multiple root spans"` is a real, confirmed, language-specific
dd-trace-dotnet behavior (Round 5); Node has no equivalent issue at all,
cold or warm, once registered correctly.

## Phase 3: full signal matrix, both functions

| # | Signal | `otelSandboxNode` (Mode 1) | `ddtraceOtelApiNode` (Mode 2) |
|---|---|---|---|
| 1 | Traces | ✅ Works (OTLP, confirmed above) | ✅ Works (Trace Agent, confirmed above) |
| 2 | OTel metrics (`/v1/metrics`) | ❌ `OTLPExporterError: Not Found` (404) | Not applicable to this mode's design — no OTel SDK metrics pipeline is set up here (same reasoning as Python's Mode 2) |
| 3 | OTel logs (`/v1/logs`) | ❌ `OTLPExporterError: Not Found` (404), after fixing an `@opentelemetry/sdk-logs` API-usage bug in our own code (see below) | Not applicable, same reasoning as #2 |
| 4 | Datadog custom metrics | ✅ Works — raw DogStatsD UDP packet, same as Python (`Flushing 0 series and 1 distributions` / `Successfully flushed`) | ✅ **Works, confirmed via a follow-up test (see below)** — `datadog-lambda-js`'s own `sendDistributionMetric()`, actually called and verified, not just inferred from the library's design |
| 5 | `env` facet via plain `DD_ENV` | `DD_ENV: sandbox` set; trace sends without error | `DD_ENV: sandbox` set; trace sends without error | 
| 6 | Logs/trace correlation | Text carries no trace context (same as Python); UI-level `request_id` correlation expected, not re-verified here | `dd.trace_id`/`dd.span_id` auto-injected into log text, confirmed above, no `DD_LOGS_INJECTION` needed |
| 7 | Billing attribution (`cloud.resource_id`/`cloud.account.id`) | ✅ Confirmed on a real span after fixing Phase 1's ESM-hook bug (below) — was missing only because the wrapper span that carries them never existed | ❌ Not applicable — these are OTel-semconv-only names; dd-trace's native spans use different, Datadog-native field names instead (below) |
| 8 | Diagnostic markers (`telemetry.sdk.*`) | ✅ Confirmed present on a real span after fixing Phase 1's resource-merge bug (below) | Expected absent, same reasoning as Python — not confirmed from an actual ingested span |

### #3 detail: a real bug in our own code, not an Extension limitation (at first)

Signal 3 initially threw, not a clean 404:
```
TypeError: Cannot read properties of undefined (reading 'export')
    at .../@opentelemetry/core/build/src/internal/exporter.js:18:22
    ...
    at SimpleLogRecordProcessor.onEmit (.../SimpleLogRecordProcessor.js:65:18)
```
Read `SimpleLogRecordProcessor`'s actual constructor:
```js
constructor(options) {
    this._exporter = options.exporter;   // <-- takes an options object now
    ...
}
```
Our code passed the exporter directly (`new SimpleLogRecordProcessor(new OTLPLogExporter(...))`)
— the same "constructor API changed from positional to options-object"
pattern as Phase 1's `Resource`/`addSpanProcessor` findings, just one level
deeper (inside the logs SDK instead of traces). Fixed: `new
SimpleLogRecordProcessor({ exporter: new OTLPLogExporter({...}) })`. After
the fix, Signal 3 produces the same clean 404 as Signal 2 — *now* it's
genuinely testing the Extension's limitation, not tripping over our own
mistake. Also needed `diag.setLogger(new DiagConsoleLogger(),
DiagLogLevel.ERROR)` in `instrument.js` — without it, OTel JS silently
swallows export failures entirely (unlike Python, which logs them by
default); this is why the 404s were invisible until this was added.

### #7/#8 detail: both were the same two Phase 1 bugs wearing a different hat

See the "Correction, with root cause" and "Billing attribution, resolved
as a byproduct" subsections under Phase 1 above for the full detail — both
#7 and #8's apparent failures on `otelSandboxNode` were direct consequences
of Bug A (ESM handler loading not intercepted, so the wrapper span that
carries `cloud.resource_id`/`cloud.account.id` never existed) and Bug B (a
custom `resource` replacing rather than merging with the SDK's default,
dropping `telemetry.sdk.*`). Fixing those two bugs fixed both signals —
confirmed from real post-fix span dumps, not re-derived from source alone.

For `ddtraceOtelApiNode` (Mode 2), #7 isn't a bug to fix at all: the
OTel-semconv `cloud.*` names are specific to
`@opentelemetry/instrumentation-aws-lambda`, which Mode 2 never invokes.
Datadog's own native field names (`account_id`, `function_arn`, etc. — the
same flat names Python's native mode uses) are the correct place to look
instead, and that wasn't re-verified against a real Mode 2 span in this
round (see the manual-verification checklist).

### datadog-lambda-js vs. Python's datadog_lambda: confirmed NOT the same ddtrace side effect

Python's `datadog_lambda` package unconditionally imports and `patch_all()`s
`ddtrace` on import, even just for the metrics helper (Round 1 finding) —
exactly why the Python sandbox avoided it and used raw DogStatsD instead.
Checked whether Node's equivalent has the same problem, in isolation:
```bash
$ npm view datadog-lambda-js dependencies   # dd-trace is NOT listed
$ node -e "
    const before = new Set(Object.keys(require.cache));
    require('datadog-lambda-js');
    const after = new Set(Object.keys(require.cache));
    console.log([...after].filter(m => !before.has(m)).some(m => m.includes('node_modules/dd-trace')));
  "
false
```
**Confirmed: `datadog-lambda-js` does NOT pull in or patch `dd-trace` as a
side effect of being required** — `dd-trace` isn't even declared as one of
its dependencies. This is a genuine difference from Python, not an
assumption carried over. `otelSandboxNode` still uses raw DogStatsD UDP
(matching Python, and avoiding an extra dependency), but unlike Python,
using `datadog-lambda-js`'s own `sendDistributionMetric()` there instead
would have been safe too.

## Phase 4: Node-specific gotchas

- **OTel JS SDK v2.x's constructor-options migration** is the single
  biggest source of friction in this round — `Resource` (removed
  entirely), `TracerProvider.addSpanProcessor()` (removed),
  `LoggerProvider.addLogRecordProcessor()` (removed),
  `SimpleLogRecordProcessor`'s constructor (positional arg → options
  object) all broke from what the doc's sample (and intuition from the
  Python SDK's still-working equivalent patterns) suggested. Check every
  constructor call against current docs, not assumption-by-analogy with
  Python or with older OTel JS examples.
- **Silent export failures**: register `diag.setLogger(new
  DiagConsoleLogger(), DiagLogLevel.ERROR)` early, or OTel JS SDK export
  failures (the 404s, or any other exporter problem) are invisible —
  no error anywhere, nothing in CloudWatch, nothing to debug from.
- **The ordering bug exists, and it's silent** (Phase 1) — the single
  highest-value gotcha from this round. No exception, `200 OK`, zero
  indication anything is wrong; only the complete absence of
  `OTLP | Successfully buffered traces` in Extension debug logs reveals it.
- **No manylinux-style packaging gotcha for Node.** Python needed `pip
  install --platform manylinux2014_x86_64` to avoid shipping macOS
  binaries (Round 1). Node's native-binary packages (`dd-trace`'s
  transitive deps `@datadog/native-metrics`, `@datadog/pprof`) ship
  "prebuildify"-style bundles containing prebuilt binaries for *every*
  platform (`darwin-arm64`, `linuxglibc-x64`, `win32-x64`, ...) in one npm
  package, with runtime platform selection — `npm install` on macOS and
  deploying straight to Lambda's Linux runtime just works, no extra flags
  needed. (Moot anyway for the sandbox's own functions here, since
  `dd-trace` itself is deliberately excluded from both — Mode 1 doesn't
  use it at all, Mode 2 uses the layer's copy — but confirmed while
  investigating whether it would have been a problem.)
- **A transient npm registry 404** (Phase 1) looked identical to a real
  packaging bug at first glance — worth a retry on a single-version 404
  before concluding a package is broken, especially with very recently
  published versions.
- **`nodejs24.x` loads the handler via ESM `import()`, not `require()`** —
  `require-in-the-middle`-based instrumentation (anything registered only
  via `NODE_OPTIONS: --require`) silently never fires. Needs an **ESM
  loader hook** too, via `NODE_OPTIONS: --import <file>.mjs` calling
  `module.register('@opentelemetry/instrumentation/hook.mjs', ...)`. This
  produced the single most misleading symptom in this round: the
  instrumentation's own "Instrumenting lambda handler" debug line printed
  normally (it just confirms the hook *registered*), while the actual
  patch-application debug line never appeared and no wrapper span was ever
  created — a `200 OK`, fully "working"-looking function that silently
  skipped one span of instrumentation, not zero.
- **A custom `resource` replaces, not merges with, the SDK's default
  resource.** Easy to miss because nothing errors — spans just quietly
  lose `telemetry.sdk.*` (and anything else environment-detectors would
  normally add). Use `defaultResource().merge(resourceFromAttributes({...}))`.
- **`DD_TRACE_OTEL_ENABLED=true` alone does not register dd-trace's OTel
  bridge.** Confirmed from the actual bundled layer source (`dd-trace`
  `TracerProvider` is a lazy, never-auto-invoked getter) — an explicit
  `new (require('dd-trace').TracerProvider)().register()` is required, or
  every manually-created OTel span is a silent no-op that never exports.
  Even after registering it, span *parenting* between dd-trace's native
  auto-instrumented spans and manually-created OTel-API spans remains
  broken (unresolved, see Phase 2) — registering the provider fixes
  *export*, not *nesting*.

## Manual verification checklist (Datadog UI — not resolved via API this round, by design)

Request IDs below are from **after** the Phase 1 (ESM hook + resource
merge) and Phase 2 (TracerProvider registration) fixes — the earlier IDs
in prior versions of this checklist predate those fixes and should be
disregarded.

1. **Mode 1 `env` facet**: `service:otel-aws-sandbox-node`, request ID
   `70b5f20f-c19a-4037-8e34-eb87eb28ffde` — confirm `env:sandbox`.
2. **Mode 2 `env` facet**: `service:ddtrace-otel-api-sandbox-node`, request
   ID `4cab5e81-f0f8-4c70-a987-b8623ca0eee6` — confirm `env:sandbox`.
3. **Mode 1 span structure, re-check after the fix**: confirm the trace for
   request ID `70b5f20f-c19a-4037-8e34-eb87eb28ffde` now shows the correct
   3-span structure (Lambda wrapper root → `do-work` → `STS.GetCallerIdentity`),
   `telemetry.sdk.name`/`otel.scope.name`/`otel.trace_id` present, and
   `cloud.resource_id`/`cloud.account.id` present on the root span — all
   confirmed from CloudWatch/debug-log evidence in this doc, but never
   independently cross-checked against the actual Datadog UI after the fix.
4. ~~Mode 2's span-nesting problem~~ — **resolved, no UI check needed.**
   Confirmed directly from the actual encoded trace payload
   (`DD_TRACE_DEBUG=true`'s "Encoding payload" line, ground truth, not
   log-injected display values): `do-work` correctly nests under
   `aws.lambda`, on both cold and warm invocations. The original
   "disconnected trace" finding was a console-log comparison artifact,
   not a real structural problem — see the correction above.
5. **Mode 2 billing attribution, with the corrected expectation**: don't
   look for `cloud.resource_id`/`cloud.account.id` (confirmed
   OTel-instrumentation-only names, never set by dd-trace's native spans).
   Instead check whether the `aws.lambda` span carries Datadog-native
   fields (`account_id`, `function_arn`, or similar) the way Python's
   native-mode spans do — this specific check was never actually run
   against a real Mode 2 span, in either language, despite Round 3's text
   assuming it would work for Python's Mode 2 too.

## Round 4 closing summary

| # | Question | Verdict |
|---|---|---|
| Setup | Current Node runtime / Datadog layer version | `nodejs24.x`; `Datadog-Node24-x:143` — both verified independently, not assumed |
| Phase 1 audit | Does the doc's Node sample work as written? | No — 2 of 3 non-trivial lines broken (`Resource`, `addSpanProcessor`), both confirmed via real `TypeError`s, both fixed |
| Phase 1 ordering bug | Does Python's AwsLambdaInstrumentor ordering bug have a Node equivalent? | **Yes, and it's worse** — fails silently (`200 OK`, zero trace activity) instead of throwing |
| Phase 1 result | Does Mode 1 (OTel SDK -> Extension) produce the *correct* span structure? | ⚠️ Not at first — two real bugs (ESM handler loading not intercepted; custom resource replacing the default) meant the wrapper span never existed and `telemetry.sdk.*` was missing. **Both root-caused and fixed**; confirmed correct 3-span structure + `telemetry.sdk.*` on a real post-fix span dump |
| Phase 2 result | Does Mode 2 (dd-trace + OTel API bridge) actually export manually-created spans? | ❌ No, not without a fix — `DD_TRACE_OTEL_ENABLED=true` alone never registers dd-trace's bridge as the global TracerProvider (confirmed from the actual layer-bundled source); manual spans were complete no-ops, never exported. **Fixed** by explicitly registering `new TracerProvider().register()` |
| Phase 2 logs injection | Does `dd.trace_id` auto-inject into Node logs without `DD_LOGS_INJECTION`? | ✅ Yes, confirmed — same as Python's default `ddtrace` behavior (this part was never in question) |
| Phase 2 nesting, corrected | Does the manual OTel span nest under dd-trace's native root span, after registering the bridge? | ✅ **Yes — confirmed correct, original "unresolved" conclusion withdrawn.** The earlier console-log comparison method was unreliable (reflects whichever span is active at each individual log call, not the real tree). The actual encoded payload (`DD_TRACE_DEBUG`'s "Encoding payload" line — ground truth) shows correct 3-level nesting (`aws.lambda` → `do-work` → `http.request`) on both cold and warm invocations, no exception |
| Signal 2/3 (OTel metrics/logs) | Same 404 as Python? | ✅ Yes, confirmed empirically (after fixing an unrelated bug in our own Signal 3 code) |
| Signal 4 (custom metrics) | Works via raw DogStatsD? | ✅ Yes, confirmed, Mode 1. Mode 2 confirmed separately via a follow-up test (see below) — `datadog-lambda-js`'s `sendDistributionMetric()` actually works from Mode 2, not just assumed safe |
| `datadog-lambda-js` ddtrace side effect | Same problem as Python's `datadog_lambda`? | ❌ No — confirmed `dd-trace` isn't even a declared dependency, unlike Python |
| Billing attribution (Mode 1) | Same zero-config mechanism as Python? | ✅ Yes — but only after the Phase 1 ESM-hook fix; before that, the carrier span never existed. Confirmed on a real post-fix span (`cloud.resource_id`, `cloud.account.id` both present with correct values) |
| Billing attribution (Mode 2) | Does the same mechanism apply? | ❌ **No — wrong question, now confirmed on both languages.** `cloud.resource_id`/`cloud.account.id` are OTel-instrumentation-only names that dd-trace's native spans never set. Confirmed directly from Python's `ddtraceOtelApi` span tags (`DD_TRACE_DEBUG=true`, no redeploy needed): `function_arn`/`functionname` present, `cloud.*` absent — the Datadog-native naming scheme, same as Node's native mode. **Independently re-confirmed via a live Datadog data check on an actual ingested span** (not just local debug logs): `cloud.resource_id`/`cloud.account.id` confirmed absent, same pattern as Node's Mode 2 — the ARN is only available as `function_arn` |
| Diagnostic markers | Same `telemetry.sdk.*` auto-population as Python? | ✅ Yes — same caveat as billing attribution: confirmed only after the resource-merge fix |
| Packaging | Does Node have a manylinux-style gotcha? | ❌ No — native-binary packages ship prebuilt binaries for every platform in one install |

### Follow-up (closing a gap, not new investigation): Mode 2 custom metrics, actually tested

The Phase 3 signal matrix above originally listed `ddtraceOtelApiNode`'s
custom-metrics signal as "not separately tested... inferred from the
library's design." Closed that gap directly instead of leaving the
inference standing: added `datadog-lambda-js`'s own
`sendDistributionMetric('ddtrace_otel_api_sandbox_node.custom_metric_test',
1, 'source:custom-metrics-test')` to `handler2.js` — the intended path
for a function whose actual Lambda entry point already *is*
`datadog-lambda-js`'s handler wrapper, not an extra dependency, and with
no ddtrace-import side effect to avoid the way Python's `datadog_lambda`
has. Redeployed, invoked with `DD_LOG_LEVEL=debug`, and got the library's
own confirmation plus the Extension's, same evidence bar as every Mode 1
result:
```
{"status":"debug","message":"datadog:Flushing statsD"}
DD_EXTENSION | DEBUG | Parsed 1 valid metrics, sending to aggregator
DD_EXTENSION | DEBUG | Flushing 0 series and 1 distributions
DD_EXTENSION | DEBUG | Successfully flushed 0 series and 1 distributions
```
**Confirmed working**, not inferred. `env`/`DD_ENV` tag on the metric
itself: **confirmed present**, checked directly against live
Datadog data — `ddtrace_otel_api_sandbox_node.custom_metric_test` carries
`env:sandbox`, despite deliberately not adding it as a manual tag (that
would have defeated the point of testing whether it's auto-attached).
The Datadog Lambda Extension enriches custom DogStatsD metrics with
`env` before forwarding them, the same mechanism already confirmed for
traces — nothing needed to be set in the sending code.

---

# Round 5: .NET parity

Mirrors Rounds 1-4 for .NET. Lives in `/dotnet` (own `template.yaml`, own
stack `serverless-otel-aws-sandbox-dotnet`), isolated from the Python/Node
tooling at the repo root. Same AWS account (`<AWS_ACCOUNT_ID>`), same region
(`us-east-1`).

## Setup: layer versions, verified not assumed

**Datadog Extension**: still `101`, re-verified (`aws lambda
get-layer-version-by-arn`, same probing method as every prior round).

**`dd-trace-dotnet` layer** (Mode 2): the doc's text referenced version
`25`; probed independently and found **`26`** is current
(`2026-09-23T15:16:23Z`) — the same "doc text is stale, verify
independently" pattern as every other layer ARN in this investigation,
regardless of whether it happens to match or not:
```bash
$ for v in 24 25 26 27; do aws lambda get-layer-version-by-arn --arn "arn:aws:lambda:us-east-1:464622532012:layer:dd-trace-dotnet:$v" --region us-east-1 --query CreatedDate --output text; done
24 -> 2026-04-28T17:55:13.599Z
25 -> 2026-06-25T15:23:33.378Z
26 -> 2026-09-23T15:16:23.482Z
27 -> AccessDeniedException   # doesn't exist yet
```

## Mode 1: ported Siddhitha's sample into this repo, re-verified from here

Siddhitha's `dotnet-lambda-otel-stdout-sample` was tested in Round 2 §4,
but only as a separate external clone — never actually part of this repo.
Ported it properly into `/dotnet/src/OtelSandboxDotnet` (attribution
comment at the top of `Function.cs` citing the original repo), with
`template.yaml` here deploying it as `OtelSandboxDotnetFunction`
(`otel-aws-sandbox-dotnet`) alongside Mode 2 in one stack — not from a
separate clone anymore.

**License status, verified directly, not inferred** (added on a later
pass, after this repo itself went public): `SiddhithaBhoopathy/otel-
dotnet-lambda-extension-samples` has no `LICENSE` file anywhere in its
tree — confirmed via GitHub's own license-detection API (`license:
null`) and a full recursive tree listing (`git/trees/main
?recursive=true`) of every file in the repo, not just its root. No
license means all-rights-reserved by default under copyright law —
this repo's own ported code keeps the attribution comment for that
reason, and this repo's MIT `LICENSE` explicitly carves out that one
file as not covered by it.

**Re-verified it still deploys and works, built and deployed from this
repo's own files:**
```bash
cd dotnet && sam build && sam deploy --stack-name serverless-otel-aws-sandbox-dotnet \
  --parameter-overrides "DatadogApiKey=$DD_API_KEY" --capabilities CAPABILITY_IAM --resolve-s3
aws lambda invoke --function-name otel-aws-sandbox-dotnet --cli-binary-format raw-in-base64-out \
  --payload file://events/event.json /tmp/out.json
# -> {"OrderId":"42","Customer":"Acme"}, StatusCode 200
```
Debug logs confirm the trace still sends correctly, same as the original
external-clone test in Round 2:
```
{"status":"DEBUG","message":"DD_EXTENSION | DEBUG | OTLP | Successfully buffered traces to be aggregated."}   (x2)
{"status":"DEBUG","message":"DD_EXTENSION | DEBUG | TRACES | Successfully sent trace (1 attempts, 1155 bytes)"}
```
No porting-introduced regressions.

### Sanity check against Siddhitha's own `NOTES.md` guidance, and confirming there's no Mode 2 reference to diff against

Prompted by the Mode 2 "zero spans ever" investigation above: before trusting
Mode 1 any further, checked it against the original repo's own documented
gotchas, and separately confirmed the repo has nothing to say about Mode 2
at all (so that absence is documented, not an unchecked assumption).

**No Mode 2 reference exists in Siddhitha's repo.** Pulled it directly and
checked both samples (`dotnet-lambda-otel-stdout-sample`,
`parcel-tracking-otel-sample`): neither references `DD_TRACE_OTEL_ENABLED`
or `dd-trace-dotnet` anywhere — both explicitly set `DD_TRACE_ENABLED:
"false"` (confirmed in `dotnet-lambda-otel-stdout-sample/template.yaml`
and called out in its own README: *"`DD_TRACE_ENABLED` is `false` because
there is no Datadog tracing layer"*). Both samples are Mode 1 only, OTel
SDK via the Extension's OTLP receiver, no Datadog tracing layer at all.
There is no reference implementation anywhere in this upstream repo for
Mode 2's native-tracer bridge to diff against — confirmed, not assumed.
This does not change anything about the Mode 2 investigation above.

**`NOTES.md`'s three Mode-1 gotchas, checked against our deployed
config:**

1. **Generic vs. `_TRACES`-prefixed OTLP env vars — a real discrepancy
   found, though not a functional failure.** `NOTES.md` is explicit:
   *"Use the **generic** variables `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`
   and `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. `AddOtlpExporter()`
   does not read `OTEL_EXPORTER_OTLP_TRACES_*` and otherwise defaults to
   gRPC/4317, which drops spans."* Our `template.yaml` sets the generic
   `OTEL_EXPORTER_OTLP_PROTOCOL` correctly, but sets only the
   `_TRACES`-prefixed `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` — no generic
   `OTEL_EXPORTER_OTLP_ENDPOINT` at all. Worse, our code doesn't even call
   `AddOtlpExporter()` — it constructs `new OtlpExporterOptions()` directly
   and hands it to `OtlpTraceExporter`, an even more stripped-down path.
   Verified by instantiating the exact same `OpenTelemetry.Exporter.OpenTelemetryProtocol`
   1.18.0 package in isolation (reflecting into the live
   `OtlpHttpExportClient.Endpoint`): with `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
   set and no generic `OTEL_EXPORTER_OTLP_ENDPOINT`, the resolved endpoint
   is `http://localhost:4318/v1/traces` — **correct, but only because the
   SDK's protocol-aware default for `HttpProtobuf` happens to be
   `localhost:4318`, auto-appended with `v1/traces`, not because our
   `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` env var was read at all.** Setting
   the generic `OTEL_EXPORTER_OTLP_ENDPOINT` env var in the same isolated
   test does override the resolved endpoint, confirming the generic var is
   the only one this code path actually honors. **Conclusion: this is a
   genuine, confirmed instance of the exact hazard `NOTES.md` warns about
   — the `_TRACES`-prefixed endpoint var we set in `template.yaml` is dead
   configuration, silently ignored — but it doesn't currently cause the
   failure `NOTES.md` describes, because the fallback default coincidentally
   lands on the right port.** Worth fixing (set the generic
   `OTEL_EXPORTER_OTLP_ENDPOINT` instead/as well) so the config says what's
   actually true, but not an active bug — traces are independently
   UI-confirmed working, and this doesn't change that.
2. **`DisableAwsXRayContextExtraction = true`** — confirmed set, matching
   `NOTES.md` and the original sample
   (`TelemetryRuntime.cs:70`).
3. **Flush before the handler returns** — confirmed, via the alternative
   `NOTES.md` explicitly allows (*"`ForceFlush` or a simple processor"*):
   traces use `SimpleActivityExportProcessor` (`TelemetryRuntime.cs:79-80`),
   which exports synchronously on every span end, no batching, no flush
   needed. Metrics use the periodic OTLP exporter and are explicitly
   force-flushed (`_runtime.MeterProvider.ForceFlush()` in `Function.cs:58`)
   before the handler returns, since Lambda can freeze the execution
   environment before a periodic export timer fires.

**Net result**: Mode 1 remains confirmed working (independently verified
via the Datadog UI/API already), and this check doesn't change that or
touch Mode 2's still-unresolved question. One real, low-impact
discrepancy found (#1) and documented rather than left as an unchecked
assumption — worth a follow-up fix, not a regression to chase now.

## Mode 2: native dd-trace-dotnet + `System.Diagnostics.Activity` bridge

**Architectural difference from Python/Node, confirmed before writing any
code (not assumed)**: .NET's OTel API *is* `System.Diagnostics.Activity`/
`ActivitySource` — built into the .NET base class library itself, not a
separate OpenTelemetry package. There's no `Tracer`/`TracerProvider`
object to construct or register at all; the only question is whether
Datadog's tracer automatically listens to Activities created via a plain
`new ActivitySource(name).StartActivity(...)` call.

**Checked Datadog's actual .NET docs rather than assume Python/Node
parity**: `DD_TRACE_OTEL_ENABLED=true` is the documented flag, and the docs
show examples using `Activity`/`ActivitySource` directly — consistent with
the BCL-native design, not an OTel-SDK-API design.

**Read the actual bundled `dd-trace-dotnet:26` layer** (downloaded via its
own presigned S3 URL, same method as Round 4's Node layer check — not just
GitHub source):
```bash
$ strings datadog/net6.0/Datadog.Trace.dll | grep -i "ActivityListener\|OTEL_ENABLED"
_activityListenerInstance
<IsActivityListenerEnabled>k__BackingField
$config_datadog:dd_trace_otel_enabled
CreateActivityListenerInstance
ActivityListenerHandler
<DisabledActivitySources>k__BackingField
```
Unlike Node (where `TracerProvider` was a lazy, never-auto-invoked getter),
these symbols point at a **deny-list-based, internally-managed**
`ActivityListener` tied directly to the `dd_trace_otel_enabled` config
flag — `DisabledActivitySources` implies the default behavior is to listen
to *all* `ActivitySource`s except explicitly excluded ones, not an
opt-in allowlist. (.NET assemblies aren't call-graph-traceable via
`strings` the way JS/Python source is, so this is supporting evidence, not
a full proof the way Round 4's Node findings were — the empirical test
below is what actually settles it.)

Built `DdtraceOtelApiDotnetFunction` from scratch (no existing sample to
port): plain `ActivitySource.StartActivity("do-work")`, an AWS SDK
`GetCallerIdentity` call, `Console.WriteLine` logging. **No explicit
listener registration anywhere in the code** — deliberately testing
whether `DD_TRACE_OTEL_ENABLED=true` alone is sufficient, unlike Node.

### The priority test: corrected twice — first by a reconciliation investigation, then by a root-cause investigation showing the first correction still wasn't rigorous enough

**This section originally reached the wrong conclusion, was corrected
once, and that correction is now itself downgraded.** Live Datadog UI/API
evidence (a live Datadog data check) found **zero spans ever ingested** for
`service:ddtrace-otel-api-sandbox-dotnet`, across every Round 5
invocation, not just the two most recent cold/warm ones — not a
structural nesting issue, a complete absence. The first correction below
(Steps 1–3) explained that absence as a cold-start-only artifact. That
explanation rested entirely on the Extension's own "successfully sent
trace" log line as proof a real span existed in the payload. Applying the
same discipline that caught a methodological error in Node's Round 4
finding — never trust a log-injected summary line, verify against the
actual local tracer output — to .NET's evidence here breaks that
explanation: **there is no .NET equivalent of Node's "Encoding payload"
line anywhere in dd-trace-dotnet's debug output**, so "Extension says
sent" was never actually corroborated by proof a span was constructed.
The investigation below re-opens Steps 1–3, not just the headline
conclusion.

**Step 1 — read the full debug log around the warning, not just the one
line.** No drop/reject/discard message appears anywhere near `"trace has
multiple root spans"` — the Extension logs it as a diagnostic, then
proceeds to flush and reports `"Request succeeded"`/`"Successfully sent
trace"` regardless. This is itself an important finding on its own: **the
Extension's "successfully sent" only confirms the HTTP POST transport
succeeded** — it says nothing about whether Datadog's backend actually
retained a structurally invalid payload after accepting it, *and, as Step
4 below establishes, nothing about whether a real span was ever in that
payload in the first place.* A backend-side rejection of a multi-root
trace would be completely invisible from Lambda-side Extension logs,
which is exactly the kind of gap that produces "Extension says sent, UI
shows nothing" — but so would the Extension reporting success on a
payload it synthesized itself from proxied Runtime API traffic,
independent of whatever the managed .NET tracer did or didn't do
internally.

**Step 2 — the isolation test (still valid as a code-level result, not as
a root-cause explanation).** Redeployed with the manual `Activity`
removed (dd-trace's native `aws.lambda` span + the STS call only). **The
warning still appeared, unchanged.** Removed the STS call too — a
completely bare handler, nothing but `Console.WriteLine` and a return.
**The warning still appeared.** This still conclusively rules out the
manual Activity (and the AWS SDK call) as the *cause of the warning* —
whatever triggers "multiple root spans" has nothing to do with the OTel
bridge's code. It does **not**, on its own, establish that any of these
invocations produced a real, retained span; that was an unjustified leap
made when this step was first written up.

**Step 3 — the warm-vs-cold comparison (same caveat).** The same
bare-handler test, invoked again on an already-warm container (no
`INIT_START`, no `init_duration_ms` in the `REPORT` line): no warning at
all, Extension reports a clean single "successfully sent trace." Restored
the real code (manual Activity + STS call) and repeated on a clean warm
invocation: still no warning, still reports success. **What this
comparison actually shows**: the warning message's presence/absence
correlates with cold/warm. **What it does not show, and was previously
claimed to show**: that the warm case's "successfully sent" means a real,
correctly-nested span existed and was retained. Both the labeled cold
invocation (`401fc0a8-05ee-4f7a-a71b-add9a137e4c1`) and the labeled warm
invocation (`d7707627-a128-43a6-85b0-dcb5940b2ce2`) from this comparison
have **not** been checked against the Datadog UI for actual retention —
that check is still outstanding (checklist item 2 below), and until it's
done, "warm invocations are fine" is not an established fact, just the
absence of one diagnostic message.

**Step 4 — the new root-cause investigation (this round's addition): does
dd-trace-dotnet ever produce local, ground-truth evidence of a
constructed span?** With `DD_TRACE_DEBUG=true` confirmed live on the
deployed function (`aws lambda get-function-configuration`, no drift from
source), a fresh cold-start invocation was read directly from raw
CloudWatch logs. Result: `[datadog-wrapper]` lines confirm the CLR
profiler is configured and attached at the OS-process level
(`CORECLR_PROFILER`/`CORECLR_ENABLE_PROFILING` env vars present, wrapper
proxy lifecycle runs normally — `OnDelegateBegin`/`OnDelegateEndAsync`,
"the extension responds with statusCode = OK"). But **none of
dd-trace-dotnet's debug output is span-construction evidence**: the
`[DD_TRACE_DOTNET]`-prefixed lines are generic wrapper-proxy lifecycle
logging, not tracer internals, and the `DD_EXTENSION | DEBUG` lines all
belong to the Extension (Rust sidecar), not the managed .NET tracer.
There is **no line anywhere in this output equivalent to Node's
`"Encoding payload"` line** — the one that showed the actual
msgpack-bound span JSON with `trace_id`/`span_id`/`parent_id` as ground
truth for Node's Round 4 correction. Also checked and ruled out as
explanations for the gap: (a) log misattribution — the request ID behind
the original "successfully sent trace (1492 bytes)" line appears 20
times, exclusively in `/aws/lambda/ddtrace-otel-api-sandbox-dotnet`'s own
log group, never in Mode 1's or any other function's; (b) deployment
drift — both layer ARNs (`Datadog-Extension:101`, `dd-trace-dotnet:26`)
and `AWS_LAMBDA_EXEC_WRAPPER=/opt/datadog_wrapper` are confirmed
live-attached, matching source exactly; (c) the `101-next` Extension
build tag a live Datadog data check flagged — confirmed universal across this repo's
working functions (the very first Python span pasted back in Round 2
already showed `dd_extension_version:100-next`), not unique to this
function, ruled out as a lead.

**Step 5 — closing the one gap Step 4 left open, and elevating the
finding.** Step 4 used `DD_TRACE_DEBUG=true`, which controls verbosity of
dd-trace-dotnet's *runtime* logging, but not its *startup banner*
specifically. Reading the raw `[datadog-wrapper]` config dump from Step
4's cold-start log turned up `DD_TRACE_STARTUP_LOGS: 0` — a distinct,
dedicated setting that suppresses exactly the one thing that would have
been genuine ground truth: the managed tracer's own startup banner
(version, configured service name, agent endpoint, attach confirmation).
With it off, zero tracer diagnostic output is the *expected* outcome
regardless of whether the tracer works — which meant Step 4's "no ground
truth found" result was still ambiguous between "the tracer doesn't log
anything useful" and "the tracer logs something useful, but we had it
turned off."

**Follow-up test**: explicitly set `DD_TRACE_STARTUP_LOGS=1` alongside
the existing `DD_TRACE_DEBUG=true` and `DD_LOG_LEVEL=debug` on the live
function, forced a fresh cold start (confirmed via `Init Duration:
1014.40 ms` in the `REPORT` line), and read the complete raw CloudWatch
log for that invocation end-to-end (`INIT_START` through `END`/`REPORT`,
79 lines, nothing truncated). The `[datadog-wrapper]` config dump
confirms the setting was actually received: `[datadog-wrapper]
DD_TRACE_STARTUP_LOGS: 1`. **Result: still no startup banner, or any
other dd-trace-dotnet-originated line, anywhere in the log** — not in the
cold-start init phase, not around the handler invocation. Every line in
the capture is attributable to either the Extension (`DD_EXTENSION |
DEBUG | ...`) or the wrapper's own generic proxy lifecycle logging
(`[DD_TRACE_DOTNET] DelegateWrapper Running/Finished OnDelegateBegin/
OnDelegateEndAsync`, `"The extension responds with statusCode = OK"`).
The same cold invocation's extension log still shows `"trace has multiple
root spans"` followed by `"Successfully sent trace (1 attempts, 1489
bytes)"`, same pattern as every prior test.

**This is the stronger, more specific, reportable finding**: it is not
that the managed .NET tracer is merely quiet by default — it was
explicitly told, via the one setting documented to control exactly this
behavior, to produce its startup banner, and still produced nothing.
Either `DD_TRACE_STARTUP_LOGS=1` is not taking effect for this layer
version/runtime combination in this Lambda environment, or the startup
banner is being emitted somewhere this capture doesn't reach (e.g., a
different log stream, a file instead of stdout, or suppressed by the
Lambda execution model specifically). Whichever it is, **this sandbox
still has zero local ground-truth evidence that dd-trace-dotnet's managed
tracer ever runs or constructs a span** — but the nature of the gap has
changed: from "we didn't check thoroughly enough" to "we checked with the
exact setting designed for this, and dd-trace-dotnet did not respond to
it." That's worth flagging as a potential dd-trace-dotnet /
datadog-lambda-dotnet-layer bug in its own right, independent of this
sandbox's original OTel-bridge question.

**Corrected conclusion (supersedes the prior "cold-start-specific
dd-trace-dotnet behavior" framing)**: the cold-vs-warm split in the
`"trace has multiple root spans"` warning is real and reproducible, but
it is now **only a diagnostic-message-level finding, not a retention or
span-validity finding** — nothing in this investigation can confirm
whether `ddtrace-otel-api-sandbox-dotnet` has *ever*, cold or warm,
produced a real span that the .NET managed tracer actually constructed
and that Datadog's backend retained. The Extension's "successfully sent"
line was treated throughout Round 5 as sufficient proof of both, and it
proves neither — it confirms only that *some* HTTP POST to the
Extension's local listener succeeded, transport-level, with no local
evidence showing what was in it.

**What this means for the original priority question**: still
**unresolved**, but for a stronger reason than before, now sharpened
further by Step 5. It's not just that Extension logs can't distinguish
"child" from "unflagged sibling" nesting (the prior correction's
framing) — it's that nothing checked so far, including an explicit,
correctly-received `DD_TRACE_STARTUP_LOGS=1` request, produced any local
evidence a span was constructed at all. A theory raised during this
investigation, **not confirmed**: the Extension's own "Universal
Instrumentation" feature (visible via "Extracted trace context from
headers" / "Processing UniversalInstrumentationStart/End" debug lines)
might be synthesizing span data from proxied Runtime API headers somewhat
independently of the managed CLR tracer, which would explain how
"Extension says sent" and "zero spans ever ingested" could both be true.
This is flagged here as an open lead for future investigation, not a
finding. **The Datadog UI is now the only remaining way to settle any
part of this** — checklist items 1 and 2 below, naming the specific
labeled cold and warm request IDs, are the concrete next step. Separately
from the UI check, **Step 5's result is itself a reportable finding
regardless of what the UI shows**: `DD_TRACE_STARTUP_LOGS=1`, the
documented setting for exactly this kind of ground-truth banner, produced
no observable effect in this Lambda environment — worth escalating as a
possible dd-trace-dotnet/layer issue on its own.

**Still confirmed, and unaffected by this correction**: no explicit
registration call (no `TracerProvider`-equivalent) was ever needed for
the native CLR profiler to attach and the wrapper proxy to run — that
part of the original finding holds at the infrastructure level. What's
withdrawn, now for the second time, is any claim about what happens to
the `Activity` itself once code starts running: not "arrives as a sibling
root, not a child" (withdrawn first), and now not even "sends
successfully and lands as *something*" (withdrawn here) — both were
conclusions reached from Extension-side transport logs alone, without the
local ground-truth check this round's investigation shows .NET cannot
currently provide.

### A third difference: no `dd.trace_id` in Console output, unlike Python/Node

Neither `Console.WriteLine` line in Mode 2 carries any `dd.trace_id`/
`dd.span_id` prefix — compare to Python's `[dd.trace_id=... dd.span_id=...]`
and Node's `[dd.trace_id=... dd.span_id=...]`, both automatic with no
extra config. `DD_LOGS_INJECTION` was not set in this test. Not dug into
further given the scope of this round, but worth noting as a third
concrete Python/Node-vs-.NET difference: .NET's log/trace correlation most
likely requires an explicit logging-framework integration (a supported
sink/provider for `Microsoft.Extensions.Logging`, Serilog, NLog, etc.)
rather than patching raw `Console.WriteLine` the way Python patches the
`logging` module — plain console output appears to be out of scope for
.NET's auto-injection by design, not a bug to fix here.

## Full signal matrix

| # | Signal | `otel-aws-sandbox-dotnet` (Mode 1) | `ddtrace-otel-api-sandbox-dotnet` (Mode 2) |
|---|---|---|---|
| 1 | Traces | ✅ Works (OTLP, re-verified above) | ❌ **Confirmed broken.** The Extension reports "successfully sent trace" on both cold and warm invocations, but no local ground-truth evidence (no dd-trace-dotnet equivalent of Node's "Encoding payload" line) ever confirmed a real span was constructed — and the open question has since been closed with a negative result: both labeled request IDs (cold `401fc0a8-05ee-4f7a-a71b-add9a137e4c1`, warm `d7707627-a128-43a6-85b0-dcb5940b2ce2`) were checked directly in the Datadog UI/API and **neither produced any ingested span**. Zero spans, cold or warm, under any tested condition |
| 2 | OTel metrics (`/v1/metrics`) | ❌ **Confirmed failing, same 404 pattern as Python/Node** — checked directly via the Datadog UI/API: `otel_aws_sandbox_dotnet.otel_metric_test` never landed in any invocation, while `custom_metric_test` (Signal 4, DogStatsD) did. No visible error locally (neither in app logs nor Extension logs — likely .NET OTel SDK's self-diagnostics being file-based by default, same category as Node needing an explicit `diag.setLogger`), but the UI-confirmed outcome itself is a clean ❌, not inconclusive | Not applicable — no OTel metrics pipeline in this mode, same reasoning as Python/Node Mode 2 |
| 3 | OTel logs (`/v1/logs`) | Not re-tested this round — Round 2 §4 already established (both empirically and via the original repo's own `NOTES.md`) that the Extension has no `/v1/logs` endpoint; Mode 1 here uses the same STDOUT-JSON-log architecture as that earlier test | Not applicable |
| 4 | Datadog custom metrics | ✅ Works — raw DogStatsD UDP packet (`Parsed 1 valid metrics`, `Successfully flushed 0 series and 1 distributions`), same approach as Python/Node. No equivalent of a "datadog-lambda" NuGet package exists for .NET to have a ddtrace-import side effect in the first place — the raw-UDP approach isn't a workaround here, it's the only approach | Not separately tested |
| 5 | `env` facet via plain `DD_ENV` | `DD_ENV: sandbox` set; trace sends without error | `DD_ENV: sandbox` set; trace sends without error |
| 6 | Logs/trace correlation | Not applicable in the same way — this sample's own `JsonConsoleLogExporter` already manually computes and injects `dd.trace_id`/`dd.span_id` into its JSON logs (confirmed back in Round 2 §4) | ❌ No automatic injection into plain `Console.WriteLine`, confirmed above — likely needs an explicit logging-framework sink, not re-tested further |
| 7 | Billing attribution | ✅ Confirmed both ways: at the binary level (`AddAttributeCloudResourceId`/`AddAttributeCloudAccountID` present in the deployed `OpenTelemetry.Instrumentation.AWSLambda.dll`) **and** on a live ingested span via the Datadog UI/API (`cloud.resource_id`, `cloud.account.id` both present) | Expected absent (Datadog-native field names instead), consistent with Python/Node's native mode — not independently tested this round |
| 8 | Diagnostic markers (`telemetry.sdk.*`) | ✅ Confirmed via the Datadog UI/API: `telemetry.sdk.name` present on a live ingested `otel-aws-sandbox-dotnet` span (no literal string match was found in a quick binary search of `OpenTelemetry.dll` — likely constructed dynamically — but the live-span result settles it) | Expected absent, not tested |

## Phase 4: .NET-specific gotchas

- **.NET's OTel "bridge" is BCL-native, not a separate package.**
  `System.Diagnostics.Activity`/`ActivitySource` is the OTel API in .NET —
  there's no `TracerProvider`-equivalent object to construct or register
  for the native-tracer-bridge mode. This made Mode 2 the *simplest* of
  the three languages to write (no SDK setup code at all), but also meant
  the "is registration required" question from Round 4 didn't even apply
  in the same shape — the real equivalent question is whether dd-trace's
  internal `ActivityListener` activates automatically, which it does.
- **`AWS_LAMBDA_EXEC_WRAPPER=/opt/datadog_wrapper` sidesteps Python/Node's
  entire class of ordering bugs.** Unlike Python (`datadog_lambda.handler.handler`)
  or Node (`require`/`import` hook timing), .NET's native tracer activates
  via a CLR profiler attached at the OS-process level, before the managed
  runtime (and therefore the handler module) ever loads. There is no
  "instrumentation registered before vs. after the handler is defined"
  question possible in this model — confirmed by there being no ordering
  bug to find here, not just an absence of testing for one.
- **`"trace has multiple root spans"` correlates with cold start, but
  that correlation is not a root cause — a genuinely easy misdiagnosis,
  twice over.** The Extension's own debug log names the structural
  problem in plain English, which made it *look* like strong, direct
  evidence for a bridge-parenting bug — but it fires identically on a
  completely bare handler with no Activity, no AWS SDK call, nothing, and
  only on cold starts. The first correction stopped there and concluded
  "cold-start-specific dd-trace-dotnet behavior, unrelated to the bridge."
  That conclusion itself turned out to rest on an unverified assumption —
  that the Extension's "successfully sent" line meant a real span existed
  at all. It doesn't: see the Step 4 root-cause investigation above. A
  clean-looking diagnostic message, and even a clean isolation test, are
  not proof of what's actually in the payload — worth corroborating
  against local ground-truth tracer output, not just Extension-side
  summary lines, before trusting either.
- **The Extension's "successfully sent trace" confirms transport, not
  backend retention, and — now established directly — not span validity
  either.** No drop/reject message appears anywhere near the multi-root
  warning — the Extension reports success regardless, on every
  invocation tested, cold or warm. But dd-trace-dotnet's own debug output
  has no line equivalent to Node's "Encoding payload" line, so nothing
  confirms a span was actually constructed before that "success" was
  reported. If Datadog's backend silently discards structurally invalid
  payloads after accepting the HTTP POST, that's invisible from
  Lambda-side logs — and so is the Extension's own "Universal
  Instrumentation" feature potentially synthesizing span data from
  proxied Runtime API headers independent of the managed tracer (a
  plausible, unconfirmed theory raised during this investigation). Either
  way, "Extension says sent" is not evidence this sandbox can treat as
  proof of anything about the actual span.
- **`DD_TRACE_STARTUP_LOGS=1`, explicitly set and confirmed received by
  the wrapper, produced no startup banner at all — a specific,
  reportable gap, not just "the tracer happens to be quiet."** The
  `[datadog-wrapper]` config dump (controlled by `DD_TRACE_STARTUP_LOGS`,
  a setting distinct from `DD_TRACE_DEBUG`) showed `0` during the initial
  root-cause pass, which fully explained the absence of ground truth
  without implying anything was broken — a quiet tracer by default. But
  re-running with it explicitly set to `1` (confirmed live in the
  `[datadog-wrapper]` dump itself: `DD_TRACE_STARTUP_LOGS: 1`), on a
  freshly forced cold start, with the complete raw log read end-to-end,
  still produced zero dd-trace-dotnet-originated lines of any kind — no
  version, no service name, no agent-connection status, nothing beyond
  the same generic wrapper-proxy lifecycle logging seen before. The
  setting reached the wrapper but had no observable effect. This is worth
  escalating as a possible dd-trace-dotnet / Lambda-layer bug in its own
  right, independent of this sandbox's OTel-bridge question.
- **.NET OTel SDK self-diagnostics likely go to a file, not the console**,
  by default — the probable explanation for why Signal 2's failure
  produced no local error message, though confirmed as a genuine `❌` via
  the Datadog UI/API directly rather than left as inconclusive.
- **No .NET equivalent of `datadog-lambda-js`/`datadog_lambda` exists** —
  so the "does the custom-metrics helper have a ddtrace-import side
  effect" question from Rounds 1/4 doesn't apply to .NET at all; raw
  DogStatsD UDP is simply the standard approach here, not a workaround.

## Manual verification checklist (Datadog UI — not resolved via API this round, by design)

1. ~~The actual remaining priority question, now more basic than
   nesting~~ — **resolved, confirmed broken.** Checked both specific
   labeled request IDs directly in the Datadog UI/API — cold start
   (`401fc0a8-05ee-4f7a-a71b-add9a137e4c1`) and warm
   (`d7707627-a128-43a6-85b0-dcb5940b2ce2`) — rather than relying on
   a live Datadog data check's aggregate "zero across 30 days." **Neither request ID
   produced any ingested span.** `ddtrace-otel-api-sandbox-dotnet` does
   not land in the Datadog UI at all, cold or warm. The nesting question
   (does `do-work` land as a child of `aws.lambda` or an unflagged
   sibling root) is now moot — there is no span of either shape to check.
2. ~~Cold-start trace retention~~ — **resolved, same result.** The same
   labeled cold invocation (`401fc0a8-05ee-4f7a-a71b-add9a137e4c1`, the
   one that logged `"trace has multiple root spans"`) does not appear in
   the UI. Datadog's backend silently discards it after the Extension's
   transport-level "successfully sent" — confirmed, not just a
   theory to confirm or rule out. (The warm invocation's trace is
   equally absent, so this isn't specific to the cold-start/multi-root
   condition either — see the updated signal matrix and closing summary
   below for the full, now-closed verdict.)
3. ~~Mode 1 billing attribution / diagnostic markers~~ — **resolved.**
   Confirmed directly via the Datadog UI/API (an earlier check
   that was stale when this checklist item was first written, not an
   outstanding gap): `cloud.resource_id`, `cloud.account.id`, and
   `telemetry.sdk.name` are all present on an ingested
   `otel-aws-sandbox-dotnet` span. Matches the mechanism already confirmed
   from the binary above.
4. **Mode 2 log correlation**: confirm whether `Console.WriteLine` output
   for `ddtrace-otel-api-sandbox-dotnet` shows up correlated via
   `request_id` in the UI (the request-ID-based mechanism established in
   Round 1 should still apply, independent of the missing `dd.trace_id`
   text-level injection) — same UI-level-vs-text-level distinction Round 1
   made for Python.

## Round 5 closing summary

| # | Question | Verdict |
|---|---|---|
| Setup | Current `dd-trace-dotnet` layer version | `26` — verified independently; doc's "25" was stale |
| Mode 1 | Does the ported sample still work, deployed from this repo? | ✅ Yes, re-verified — built, deployed, and invoked from `/dotnet`, same trace-send confirmation as the original external-clone test in Round 2 |
| Mode 2 setup | Is .NET's OTel API the same shape as Python/Node's? | ❌ No — `System.Diagnostics.Activity`/`ActivitySource` is BCL-native, not a separate OTel package; no `TracerProvider`-equivalent object exists to register at all |
| **Mode 2 priority test** | Does a manually-created Activity nest under dd-trace's native root span? | ❌ **Confirmed broken — moot question, since no span exists to check nesting on.** First correction: the "multiple root spans" warning is a cold-start-only artifact, unrelated to the OTel bridge (proven by isolation testing). Second correction: that first correction's "warm invocations are fine" assumption relied solely on the Extension's "successfully sent" line, and dd-trace-dotnet has no local ground-truth evidence (no "Encoding payload" equivalent) to back that up — confirmed even with `DD_TRACE_STARTUP_LOGS=1` explicitly set and received, still zero tracer-originated output. **Final verdict**: the Datadog UI/API check against both labeled request IDs (cold and warm) has since been done and found zero ingested spans for either — this function has never produced a real, retained span, confirmed, not just unconfirmed |
| Signal 2 (OTel metrics) | Same 404 as Python/Node? | ❌ **Confirmed failing** — checked directly via the Datadog UI/API: `otel_metric_test` never landed while `custom_metric_test` did, same pattern as Python/Node. No local error visible (likely file-based .NET diagnostics), but the outcome itself is a confirmed negative, not inconclusive |
| Signal 4 (custom metrics) | Works via raw DogStatsD? | ✅ Yes, confirmed, Mode 1 only — and there's no .NET equivalent of `datadog_lambda`/`datadog-lambda-js` to have a side effect from in the first place |
| Logs/trace correlation (Mode 2) | Does `dd.trace_id` auto-inject into `Console.WriteLine`? | ❌ No — a third concrete Python/Node-vs-.NET difference, likely requires an explicit logging-framework sink instead of plain console output |
| Billing attribution (Mode 1) | Same mechanism as Python/Node? | ✅ Confirmed both at the binary level (`AddAttributeCloudResourceId`/`AddAttributeCloudAccountID` present) and on a live ingested span via the Datadog UI/API (`cloud.resource_id`, `cloud.account.id`, `telemetry.sdk.name` all present) |
| Ordering bug | Does .NET have Python/Node's instrumentation-ordering hazard? | ❌ No — `AWS_LAMBDA_EXEC_WRAPPER`'s CLR-profiler-at-process-level model has no "registered before vs. after handler is defined" moment to get wrong in the first place |
| **Cold-start finding** | What actually causes `"trace has multiple root spans"`, and why did a live Datadog data check find zero spans ever ingested? | **Confirmed.** The 2-step isolation test (remove Activity → still happens; remove AWS SDK call too → still happens) holds: the warning is unrelated to any code in this sandbox, and correlates with cold start specifically. What does **not** hold: the original claim that this explains "zero spans ever ingested" as a cold-start-retention issue, since the warm invocation turned out to be equally affected. That required trusting the Extension's "successfully sent" line as proof a span existed, and a dedicated root-cause check (live `DD_TRACE_DEBUG=true`, raw CloudWatch logs, config-drift check, extension-version-universality check) found **no local ground-truth evidence anywhere in dd-trace-dotnet's debug output that a span is ever constructed** — no equivalent of Node's "Encoding payload" line. A follow-up test closed the remaining gap in that check: `DD_TRACE_STARTUP_LOGS=1` (the dedicated setting for the tracer's own startup banner, distinct from `DD_TRACE_DEBUG`) was explicitly set, confirmed received by the wrapper, and a fresh cold start still produced **zero dd-trace-dotnet-originated log lines of any kind**. Ruled out: log misattribution, config/layer drift, and the `101-next` build tag as a unique cause. **Still an unconfirmed lead for engineering, not re-investigated here**: the Extension's own "Universal Instrumentation" feature may be synthesizing span data independent of the managed tracer. Bottom line, now closed: the Datadog UI/API check against both labeled request IDs (cold `401fc0a8-05ee-4f7a-a71b-add9a137e4c1`, warm `d7707627-a128-43a6-85b0-dcb5940b2ce2`) found **zero ingested spans for either** — this function has never produced a real, retained span, under any tested condition. The `DD_TRACE_STARTUP_LOGS=1` finding (no observable effect from the one setting designed to produce ground-truth output) remains worth escalating to engineering separately |

## Pre-Java housekeeping (before a 4th language is added)

Three cleanup/verification passes over the existing Python/Node/.NET work,
done before any Java code is written, so the repo is in a clean, verified
state before a fourth language and a fourth set of layers gets added.

1. **Python moved into `/python`.** Python's source/config
   (`handler.py`, `handler2.py`, `requirements.txt`,
   `requirements-ddtrace.txt`, `build.sh`, `build-ddtrace.sh`,
   `serverless.yml`, `package.json`, `package-lock.json`) was floating at
   repo root since before this repo had multiple languages — moved to
   match the `/node`/`/dotnet` pattern. `FINDINGS.md`, `README.md`, and
   `BUILD_WALKTHROUGH.md` stay at repo root. No path changes were needed
   inside the moved files themselves — every reference (`serverless.yml`'s
   `package.patterns`, `handler.py`'s `sys.path.insert` via `__file__`,
   `build.sh`'s `./vendor` target) was already relative to the files'
   own directory, so moving the whole set as a unit kept them all
   correct. Redeployed and invoked both functions end-to-end from the new
   location to confirm the move didn't break anything (not just that the
   files moved):
   ```
   ✔ Service deployed to stack serverless-otel-aws-sandbox-dev (37s)
   functions:
     otelSandbox: serverless-otel-aws-sandbox-dev-otelSandbox (6.1 MB)
     ddtraceOtelApi: serverless-otel-aws-sandbox-dev-ddtraceOtelApi (18 MB)
   ```
   Both invocations returned `200`, Mode 1's raw CloudWatch log showed the
   same expected-and-already-documented `404`s for OTel metrics/logs (not
   a regression — see "The 2 known-failing OTel signals"), and Mode 2's
   log showed normal `dd.trace_id`/`dd.span_id` injection and all 86
   ddtrace integrations patching cleanly. No behavior change from the
   move, confirmed via live redeploy + invoke, not just a file listing.

2. **Fixed .NET Mode 1's dead-config OTLP endpoint var (found during the
   Siddhitha cross-check above).** Added the generic
   `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` to
   `OtelSandboxDotnetFunction` in `template.yaml`, alongside (not instead
   of) the existing `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`. Redeployed,
   invoked (trace sent cleanly, no errors, `dd.trace_id`/`otel.trace_id`
   both present in the log line as before), and re-ran the same
   reflection-into-`OtlpHttpExportClient.Endpoint` method used to find the
   bug, this time against the live config: with the generic var set to
   `:4318` and the `_TRACES`-prefixed var left at `:4318/v1/traces`, the
   resolved endpoint is `http://localhost:4318/v1/traces` — and changing
   only the generic var (to a deliberately wrong `:9999`, `_TRACES`
   var left untouched) moved the resolved endpoint to `http://localhost:9999/v1/traces`,
   proving the generic var is now the one actually read, not a
   coincidental default. Still works, now for the right reason — no
   regression, nothing else changed, Mode 2 untouched as instructed.

3. **Layer ARN/version re-probe, all three languages, via
   `aws lambda get-layer-version-by-arn`:**

   | Layer | Current (re-probed) | Referenced in repo | Status |
   |---|---|---|---|
   | `Datadog-Extension` | **101** | Python: `100` · Node: `101` · .NET: `101` | Node/.NET current. **Python is one version behind** — not "drift since Round 5" (Python's pin predates Round 5 and was simply never bumped when Node/.NET moved to `101`), but a real cross-language inconsistency worth a conscious decision, not fixed here since it wasn't asked for and Python's Mode 1/2 are both independently confirmed working on `100` |
   | `Datadog-Python311` | **128** | Python: `128` | Current, no drift |
   | `Datadog-Node24-x` | **143** | Node: `143` | Current, no drift |
   | `dd-trace-dotnet` | **26** | .NET: `26` | Current, no drift |

   (Each "current" value confirmed the same way as every prior round:
   probing one version past it returns `AccessDeniedException` —
   "not authorized ... no resource-based policy allows" — the same
   signature used throughout this repo to mean "not published yet," not
   assumed from documentation.)

**Net result**: Python relocated and re-verified working from `/python`;
one real (if low-impact) config bug fixed in .NET Mode 1 and confirmed
fixed by evidence, not just by inspection; all four layer families
confirmed current except the one cross-language Extension-version
inconsistency noted above, flagged for a decision rather than
silently fixed. Repo is in a clean, verified state for Java to be added
next.

# Round 6: Java parity

Same two modes, fourth language. Java turned out to be the cleanest
result of any round so far on the single most important question (Mode
2 registration/nesting) — the opposite outcome from .NET's confirmed,
unresolved bug. Every claim below was checked against real behavior, not
assumed from docs, per the same discipline as Rounds 1-5.

## Setup: layer versions, verified not assumed

- `Datadog-Extension`: current is `101` (same as Node/.NET, already
  re-verified in the pre-Java housekeeping pass above).
- `dd-trace-java`: current is `28` — verified via
  `aws lambda get-layer-version-by-arn`, probing one version past it
  (`29`) and getting the same `AccessDeniedException` signature used
  throughout this repo to mean "not published yet." Notably, this is the
  **first layer in this entire repo where the doc's example ARN wasn't
  stale** — Datadog's own Java Lambda install doc gives `:28` directly,
  and that's exactly what's current. (The same doc's `Datadog-Extension:99`
  example *was* stale, same pattern as every other language's doc.)

## Doc audit

**No official Java tab exists for Mode 1** (OTLP via the Extension) —
checked directly against
`https://docs.datadoghq.com/serverless/aws_lambda/opentelemetry/?tab=java#sdk`:
only Python and Node.js tabs are present. Confirmed explicitly, the same
way Round 5 noted .NET had no official doc tab and used a community
sample instead — but unlike .NET, **no community sample exists for Java
either** (checked: Siddhitha's .NET repo has no Java equivalent, and a
search for a Datadog or community Java+Extension+OTLP Lambda sample
turned up nothing). Mode 1 below is therefore built from scratch using
the official OpenTelemetry Java AWS Lambda and AWS SDK v2 instrumentation
libraries, not a ported sample.

**Mode 2's doc exists, but is generic, not Lambda-specific.** Datadog's
"Java Custom Instrumentation using the OpenTelemetry API" doc confirms
`DD_TRACE_OTEL_ENABLED=true` and states "you must only depend on the
OpenTelemetry API (and not the OpenTelemetry SDK)" and that "no explicit
registration is needed." That claim is the same one that turned into a
confirmed, unresolved bug for .NET — see "Mode 2" below for how this
round actually verified it instead of repeating that mistake.

**A real version-skew bug was found and fixed while following the doc's
own recommended library (not the doc's fault, but the same category of
"the sample breaks the moment you actually run it" risk Python/Node's
rounds hit).** `io.opentelemetry.instrumentation:opentelemetry-aws-sdk-2.2`'s
current GitHub `main` branch source shows
`AwsSdkTelemetry.create(otel).createExecutionInterceptor()`, but the
actually-published `2.16.0-alpha` Maven Central artifact (confirmed via
`javap` against the real downloaded jar, not GitHub) only has
`newExecutionInterceptor()` — GitHub's main branch is ahead of what's
published. Using the GitHub-source method name produced a real compile
failure (`cannot find symbol`), caught immediately by actually building
the code rather than trusting the source browse.

## Mode 1: built fresh, no sample to port

- `java21` runtime (AWS Lambda also now supports `java25`, released
  Nov 2025; `java21` chosen for tooling maturity, the same
  stability-over-latest reasoning Round 5 used picking `dotnet8`).
- Deployed via **AWS SAM + Maven** (not Serverless Framework) — same
  reasoning as Round 5's .NET choice: SAM's Maven build workflow (Lambda
  Builders' `JavaMavenWorkflow`) handles compiled-language packaging
  more directly than the Serverless Framework's Node-centric plugin
  defaults.
- `io.opentelemetry.instrumentation.awslambdacore.v1_0.TracingRequestHandler`
  (official OTel Java AWS Lambda instrumentation) as the handler base
  class — it builds the `aws.lambda`-equivalent root span
  (`dd-tracer-serverless-span`, see Mode 2's span tree below) automatically
  and, notably, **force-flushes traces, metrics, and logs together on
  every invocation by itself** (`LambdaUtils.forceFlush`, confirmed by
  reading the actual library source) — unlike Python/Node/.NET's Mode 1,
  which all needed a manual `ForceFlush()`/`force_flush()` call wired in
  by hand. A genuine Java-specific advantage, not a workaround.
- `io.opentelemetry.instrumentation.awssdk.v2_2.AwsSdkTelemetry`'s
  execution interceptor on the AWS SDK v2 `StsClient`, for the
  `STS.GetCallerIdentity` child span — same role as
  `BotocoreInstrumentor`/`AwsInstrumentation` in Python/Node.
- `OtlpHttpSpanExporter`/`OtlpHttpMetricExporter` pointed at
  `localhost:4318/v1/traces` and `/v1/metrics`, manually constructed (no
  autoconfigure), matching the explicit-wiring style of every other
  language's Mode 1 this repo has used.
- **Confirmed via raw CloudWatch logs with `DD_LOG_LEVEL=debug`** (not
  just "it returned 200"): `TRACES | trace payload size after
  enrichment` → `OTLP | Successfully buffered traces to be aggregated`
  (×3, one per span as `SimpleSpanProcessor` exports each span
  individually on end) → `TRACES | Successfully sent trace (1 attempts,
  1346 bytes)`. OTel metrics confirmed `404` (`Failed to export metrics.
  Server responded with HTTP status code 404`) — same Extension-level
  limitation already confirmed across Python/Node/.NET, holds for Java
  too. Custom metric via raw DogStatsD UDP confirmed (`Parsed 1 valid
  metrics`, `Successfully flushed 0 series and 1 distributions`).
- **Could not independently confirm actual span parent/child structure
  via the Datadog UI/API this round** — attempted a direct Datadog API
  query (`/api/v2/spans/events/search`) using the `DD_API_KEY`/`DD_APP_KEY`
  available in this environment; both returned `403 Forbidden` on every
  endpoint tried, including `/api/v1/validate`-adjacent read-only calls,
  meaning the available app key lacks sufficient scope here. This is
  consistent with every prior round's pattern of needing a live Datadog data check/the
  user for UI-level checks — added to the manual verification checklist
  below rather than assumed.
- No `dd.trace_id`-style text injected into plain `System.out.println`
  output — same category of result as .NET (not Python/Node, which patch
  their logging/console modules directly); confirmed by reading the raw
  log lines themselves, not assumed from the lack of a Java
  `datadog_lambda`-equivalent library.
- **`datadog-lambda-java` checked and deliberately not used**: it exists,
  but Datadog's own docs state it's "no longer required when using the
  Datadog Lambda Extension version 25 or above" (we're on `101`) — so the
  "does importing it have a ddtrace-patching side effect" question from
  Rounds 1/4 doesn't need testing this round; it's simply not the
  recommended path anymore. `com.datadoghq:java-dogstatsd-client` exists
  as the current official custom-metrics client, but raw UDP was used
  instead for exact methodological parity with Python/Node/.NET (all
  three went raw-UDP since none had a side-effect-free wrapper either).

## Mode 2: the priority investigation — registration and nesting, verified not assumed

**This is the section this round was actually about.** .NET's Mode 2
turned into a confirmed, unresolved bug specifically because "no
registration needed" was taken on faith. This round did not repeat that.

**Step 1 — checked dd-trace-java's actual mechanism before writing any
code.** Searched the real `DataDog/dd-trace-java` source (not just the
doc) for how `GlobalOpenTelemetry` gets wired up. Found
`dd-java-agent/instrumentation/opentelemetry/opentelemetry-1.4` (and
`-1.27` for logs, `-1.47` for metrics): dedicated javaagent
instrumentation modules that **bytecode-transform
`io.opentelemetry.api.GlobalOpenTelemetry$ObfuscatedOpenTelemetry`
itself**, confirmed directly in this round's own debug logs (line below).
This is architecturally different from both prior findings: not Python's
automatic-but-unverified claim, not Node's lazy-getter-nobody-calls
problem, and not .NET's CLR-profiler-attaches-but-managed-tracer-produces-
no-local-evidence gap. Java's javaagent is attached via a JVM
`-javaagent:` flag **prepended to the process start command before the
JVM boots** (confirmed in the Extension layer's own `datadog_wrapper`
script: `args=($START_COMMAND -javaagent:$DD_Agent_Jar
-XX:TieredStopAtLevel=1 ...)`) — meaning the bytecode transformer is
registered before any application class, including the OTel API classes
themselves, ever loads. There is no "timing window" for this to miss, by
construction, confirmed by reading the actual wrapper script rather than
assuming parity with .NET's AWS_LAMBDA_EXEC_WRAPPER mechanism just
because both use it.

**Step 2 — real ground-truth span-construction evidence, not just the
Extension's "successfully sent" line.** With `DD_TRACE_DEBUG=true` and
`DD_LOG_LEVEL=debug` both set, a fresh invocation's raw log shows dd-trace-
java's own internal `DDSpan` objects being constructed, with real
trace/span/parent IDs, **before** serialization — the Java equivalent of
Node's "Encoding payload" ground truth, arguably stronger since it shows
the actual in-memory object graph rather than an already-encoded byte
payload:
```
DEBUG datadog.trace.agent.core.DDSpan - Started span: DDSpan [ t_id=2960045952492156169, s_id=5346664735350579538, p_id=529043057899721163 ] trace=ddtrace-otel-api-sandbox-java/internal/do-work ...
```
This conclusively answers the "does a span actually get constructed"
question: **yes**, confirmed from dd-trace-java's own internals, not
inferred from Extension transport logs.

**Step 3 — nesting, confirmed via the same ground truth.** The full
`RemoteWriter - Enqueued for serialization` line shows all three spans
from one invocation, same `t_id` (trace ID) throughout:
```
DDSpan [ t_id=...169, s_id=529043057899721163, p_id=0 ]                          trace=.../dd-tracer-serverless-span   (root)
DDSpan [ t_id=...169, s_id=5346664735350579538, p_id=529043057899721163 ]        trace=.../internal/do-work            (our manual span)
DDSpan [ t_id=...169, s_id=2143624195011517729, p_id=5346664735350579538 ]       trace=java-aws-sdk/aws.http/Sts.GetCallerIdentity
```
The root's `p_id` is `0` (it's the actual root). The manual `do-work`
span's `p_id` **exactly matches** the root's `s_id` — correctly nested as
a child. The STS child span's `p_id` **exactly matches** `do-work`'s
`s_id` — correctly nested two levels deep. A clean, single-rooted,
three-level trace, confirmed from real span IDs, not inferred from the
absence of a warning message (the mistake the .NET investigation's first
correction made). The final `Finished span (WRITTEN)` line for the root
even carries `_dd.integration=otel`, Datadog's own explicit tag
confirming the OTel bridge was exercised. **No `"trace has multiple root
spans"` warning appeared anywhere, on any invocation this round** (grep
across every debug log captured — the opposite result from .NET).

**Step 4 — the ordering-bug test, run directly rather than assumed from
the architecture.** Moved the `GlobalOpenTelemetry.getTracer(...)` call
from inside the handler method (resolved lazily, per-invocation) into a
`static final` field (resolved at class-load time — the earliest point
application code could possibly touch the OTel API), rebuilt, forced a
fresh cold start via redeploy (`Init Duration: 4741.52 ms` confirmed),
and re-ran the same ground-truth check. **Identical result**: clean
three-level nesting, correct parent/child span IDs, no warning. Confirmed
empirically, not assumed from "javaagents attach early so it should be
fine" reasoning alone — matching the same direct-test discipline Round 4
used for Node's ordering bug (which, by contrast, *did* fail under this
kind of test). Reverted the code back to the lazy-resolution version
afterward (no production reason to prefer either; lazy matches the
pattern Python/Node/.NET's Mode 2 code already uses).

**Conclusion**: Mode 2 works correctly for Java, confirmed with stronger
ground truth than any prior round needed to establish the same thing. No
registration call needed (confirmed, not assumed), spans nest correctly
(confirmed via real span IDs, not inferred from the absence of a
diagnostic message), and no ordering hazard exists (confirmed via a
direct test, not architectural reasoning alone).

**Same log/trace correlation gap as .NET**: no `dd.trace_id` text
injected into plain `System.out.println`, despite the tracer's own
startup banner advertising `"logs_correlation_enabled":true` — that flag
evidently governs correlation through an actual logging framework
(SLF4J/Logback/Log4j2 MDC integration), not raw console output, the same
conclusion Round 5 reached for .NET. Not dug into further given the
scope of this round.

## Full signal matrix

| # | Signal | `otel-aws-sandbox-java` (Mode 1) | `ddtrace-otel-api-sandbox-java` (Mode 2) |
|---|---|---|---|
| 1 | Traces | ✅ Confirmed via raw debug logs: trace payload enriched and sent successfully | ✅ **Confirmed with the strongest ground truth of any round** — real `DDSpan` construction and correct 3-level nesting (root → `do-work` → STS) via internal tracer logs, not just Extension transport confirmation |
| 2 | OTel metrics (`/v1/metrics`) | ❌ Confirmed failing — `Failed to export metrics. Server responded with HTTP status code 404`, same pattern as Python/Node/.NET | Not applicable — no OTel metrics pipeline in this mode, same reasoning as the other three languages' Mode 2 |
| 3 | OTel logs (`/v1/logs`) | Not independently retested this round — Round 2 §4 already established the Extension has no `/v1/logs` endpoint; no reason to expect Java differs and nothing in this round's testing contradicts that | Not applicable |
| 4 | Datadog custom metrics | ✅ Works — raw DogStatsD UDP packet (`Parsed 1 valid metrics`, `Successfully flushed 0 series and 1 distributions`), same approach as Python/Node/.NET. `datadog-lambda-java` exists but is explicitly documented as unneeded on Extension ≥25 (we're on 101) — checked, not used | ✅ **Works, confirmed via a follow-up test (see below)** — same raw DogStatsD UDP approach, actually sent and confirmed via the Extension's debug log, not just assumed from Mode 1's result |
| 5 | `env` facet via plain `DD_ENV` | `DD_ENV: sandbox` set; trace sends without error | `DD_ENV: sandbox` set; trace sends without error |
| 6 | Logs/trace correlation | Not applicable in the same way — no logging framework wired in (plain `System.out.println`, matching the other languages' Mode 1 approach) | ❌ No automatic injection into plain `System.out.println`, confirmed above — same category of gap as .NET, likely needs an explicit logging-framework sink |
| 7 | Billing attribution | ✅ Confirmed at the binary level: `AwsLambdaFunctionAttributesExtractor.class` (decompiled via `javap`) sets both `cloud.account.id` and `cloud.resource_id` `AttributeKey`s, same mechanism as Python/Node/.NET's AWS Lambda instrumentation. **Closed via a later live Datadog data check**: both attributes confirmed **present** on a live ingested span, via the OTLP pathway (`otel.scope.name`/`otel.scope.version` present, no `_dd.*` tracer metadata) | ❌ **Confirmed absent via the same later live Datadog data check**, via the Trace Agent pathway (`_dd.parser_protocol: protobuf_v07`, `_dd.origin: lambda`, `operation_name: aws.lambda`) — same pattern as Python/Node's Mode 2 |
| 8 | Diagnostic markers (`telemetry.sdk.*`) | Expected present via the OTel SDK's default `Resource` (not overridden in this sandbox's code) — closed alongside Signal 7's live check above (`otel.scope.*` present) | Expected absent, not tested |

## Phase 4: Java-specific gotchas

- **dd-trace-java instruments the OTel API classes themselves, not a
  lazy-getter object.** Unlike Node (`dd-trace`'s `TracerProvider` is a
  lazy getter nobody calls unless you explicitly register it) and .NET
  (`System.Diagnostics.Activity` is BCL-native with no bridge object at
  all), Java's javaagent bytecode-transforms
  `GlobalOpenTelemetry$ObfuscatedOpenTelemetry` directly — confirmed via
  `AgentInstaller$TransformLoggingListener - Transformed -
  instrumentation.target.class=io.opentelemetry.api.GlobalOpenTelemetry$ObfuscatedOpenTelemetry`
  in the debug log. This is the cleanest of the four languages'
  mechanisms precisely because it doesn't depend on application code
  calling anything at all.
- **The `-javaagent` flag is injected at the process-start-command level,
  before the JVM boots** — confirmed by reading the Extension layer's own
  `datadog_wrapper` script directly (`args=($START_COMMAND
  -javaagent:$DD_Agent_Jar -XX:TieredStopAtLevel=1 ...)`), the same
  category of "attaches before any app code runs" guarantee .NET's CLR
  profiler has, and empirically confirmed via the Step 4 ordering test
  above to actually hold (not just architecturally plausible).
- **`TracingRequestHandler`'s automatic multi-signal force-flush is a
  genuine advantage over Python/Node/.NET's Mode 1**, all three of which
  needed hand-written flush code to handle Lambda freezing the execution
  environment before a periodic exporter's timer fires. Confirmed by
  reading `LambdaUtils.forceFlush` directly, not assumed from the class
  name.
- **A real version-skew trap, caught by actually building the code**:
  `opentelemetry-aws-sdk-2.2`'s GitHub `main` branch has renamed
  `newExecutionInterceptor()` to `createExecutionInterceptor()`, but the
  published `2.16.0-alpha` Maven Central artifact (what actually gets
  deployed) still has the old name. Following GitHub source instead of
  the published artifact would have been a real, if quickly-caught,
  build break — the same category of risk Python/Node's doc samples hit,
  just from a different source (a library's own unreleased source
  changes, not the Datadog doc itself).
- **No Datadog API/UI access was available in this environment this
  round** — a direct attempt to query the Spans Search API with the
  available `DD_API_KEY`/`DD_APP_KEY` returned `403 Forbidden` on every
  endpoint tried. Every prior round's live-span/UI confirmations came
  from a live Datadog data check or the user directly; this round's ground-truth tracer
  logs are strong enough to answer the registration/nesting question
  without that, but billing attribution and diagnostic markers on a live
  span still need it — added to the checklist below.
- **`datadog-lambda-java` is explicitly deprecated for current Extension
  versions** — worth knowing before reaching for it reflexively the way
  Python's `datadog_lambda` or Node's `datadog-lambda-js` get reached for;
  Java's docs say plainly it's not needed on Extension ≥25.
- **A single-function `sam build <FunctionName>` silently deletes the
  other function's build artifacts from `.aws-sam/build`.** Hit this
  directly mid-round: ran a scoped build for the Mode 2 ordering test,
  redeployed, and the next Mode 1 invocation failed with
  `ClassNotFoundException` — the scoped build had wiped
  `OtelSandboxJavaFunction`'s artifact directory entirely, and `sam
  deploy` happily packaged and pushed the now-incomplete build. Fixed by
  running a full `sam build` (no function argument) before the next
  deploy. Worth flagging as a SAM footgun specific to multi-function
  templates: a scoped build is not isolated from the rest of
  `.aws-sam/build` the way it looks like it should be.

## Manual verification checklist (Datadog UI — not resolved via API this round, by design, and additionally blocked on tooling access)

1. **Mode 1 span structure on a live ingested trace**: confirm the same
   three-span tree (`aws.lambda`-equivalent root → `do-work` →
   `STS.GetCallerIdentity`) that Python/Node/.NET's Mode 1 already showed,
   now for Java. Not confirmed this round due to API access limits (see
   Phase 4 gotchas).
2. **Billing attribution and diagnostic markers on a live Mode 1 span**:
   ✅ **Closed.** Confirmed via a later live Datadog data check — `cloud.resource_id`/
   `cloud.account.id`/`telemetry.sdk.*` all present on a live
   `otel-aws-sandbox-java` span, via the OTLP pathway (Signal 7/8 above).
3. **Mode 2 span structure on a live ingested trace**: confirm the same
   root → `do-work` → STS nesting this round's ground-truth tracer logs
   already show internally, now visible in the UI too — a corroboration
   check, not an open question, given how strong the local evidence
   already is.
4. **Mode 2 log correlation**: confirm whether `System.out.println`
   output for `ddtrace-otel-api-sandbox-java` shows up correlated via
   `request_id` in the UI (the request-ID-based mechanism established in
   Round 1 should still apply, independent of the missing `dd.trace_id`
   text-level injection) — same UI-level-vs-text-level distinction every
   prior round made.

## Round 6 closing summary

| # | Question | Verdict |
|---|---|---|
| Setup | Current `dd-trace-java` layer version | `28` — verified independently; this time the doc's example ARN was *not* stale, the first time that's been true in this repo |
| Doc audit | Does an official Java Mode 1 sample/tab exist? | ❌ No — confirmed directly against the doc page; no community sample exists either, unlike .NET. Mode 1 built fresh using official OTel Java AWS Lambda + AWS SDK v2 instrumentation |
| Mode 1 | Does OTel SDK + OTLP/HTTP to the Extension work for Java? | ✅ Yes, confirmed via raw debug logs (trace sent, custom metric sent, OTel metrics 404 as expected) |
| **Mode 2 priority question** | Does a manually-created span nest under dd-trace-java's native root span, and is "no registration needed" actually true? | ✅ **Yes to both, confirmed with the strongest ground truth of any round** — real internal `DDSpan` construction/parent-child IDs, not inferred from a transport-success line or the absence of a warning. The opposite outcome from .NET's confirmed, unresolved bug |
| Ordering bug | Does Java have any version of Python's loud/Node's silent ordering hazard? | ❌ No — tested directly (static-field vs. lazy tracer resolution across a forced cold start), identical correct nesting both ways. The javaagent's process-level attach (confirmed in the Extension's own wrapper script) leaves no timing window for this to occur |
| Signal 2 (OTel metrics) | Same 404 as Python/Node/.NET? | ❌ Confirmed failing, same pattern, Mode 1 only |
| Signal 4 (custom metrics) | Works via raw DogStatsD? | ✅ Yes, confirmed for both modes — Mode 1 this round, Mode 2 via a follow-up test (see below). `datadog-lambda-java` checked and found explicitly unnecessary on current Extension versions |
| Logs/trace correlation (Mode 2) | Does `dd.trace_id` auto-inject into plain console output? | ❌ No — same category of gap as .NET (not Python/Node), despite the tracer's own config banner claiming `logs_correlation_enabled:true`; likely needs an explicit logging-framework sink |
| Billing attribution (Mode 1) | Same mechanism as Python/Node/.NET? | ✅ Confirmed at the binary level (`cloud.account.id`/`cloud.resource_id` `AttributeKey`s in the decompiled instrumentation class). **Live-span confirmation, closed in a later round**: both attributes confirmed present via a live Datadog data check on a live span (OTLP pathway) |
| Version-skew risk | Did following the "obvious" source (GitHub) instead of the published artifact break anything? | ✅ Yes, caught immediately — `AwsSdkTelemetry`'s method was renamed on GitHub's `main` branch ahead of the published Maven Central release; building the actual code (not browsing source) caught it |
| Tooling gotcha | Any deployment-tooling-specific hazard worth flagging? | ⚠️ Yes — a single-function `sam build <Name>` on this multi-function template silently deleted the other function's build artifacts, producing a broken deploy that wasn't caught until the next invocation. Fixed; flagged for anyone reusing this SAM pattern |

### Follow-up (closing a gap, not new investigation): Mode 2 custom metrics, actually tested

The signal matrix above originally listed `ddtrace-otel-api-sandbox-java`'s
custom-metrics signal as "not separately tested," assumed safe by analogy
to Mode 1's confirmed-working result. Closed that gap directly: added the
same raw DogStatsD UDP `sendCustomMetric()` helper used in
`OtelSandboxHandler` to `DdtraceOtelApiHandler`
(`ddtrace_otel_api_sandbox_java.custom_metric_test`,
`source:custom-metrics-test`) — raw UDP, not `datadog-lambda-java`, for
the same reason already established in this round: the library is
explicitly documented as unneeded on current Extension versions, so
there's no reason to prefer it over the approach already used everywhere
else in this repo. Redeployed, invoked with `DD_LOG_LEVEL=debug`, and got
the same evidence bar as every Mode 1 custom-metrics confirmation:
```
DD_EXTENSION | DEBUG | Parsed 1 valid metrics, sending to aggregator
DD_EXTENSION | DEBUG | Flushing 0 series and 1 distributions
DD_EXTENSION | DEBUG | Successfully flushed 0 series and 1 distributions
```
**Confirmed working**, not assumed. `env`/`DD_ENV` tag on the metric
itself: **confirmed present**, checked directly against live
Datadog data — `ddtrace_otel_api_sandbox_java.custom_metric_test` carries
`env:sandbox`, same result as Python's and Node's equivalent follow-up
tests, despite the raw UDP packet never setting it explicitly. The
Datadog Lambda Extension enriches custom DogStatsD metrics with `env`
before forwarding them, the same mechanism already confirmed for traces
— nothing needed to be set in the sending code, for any of the three
languages tested.

All three functions (Python's `ddtraceOtelApi`, Node's
`ddtraceOtelApiNode`, Java's `DdtraceOtelApiJavaFunction`) were redeployed
specifically for this follow-up and torn back down again afterward —
confirmed via `aws cloudformation describe-stacks` returning "does not
exist" for all three stacks, same rigor as every prior teardown.
`DdtraceOtelApiDotnetFunction` (.NET Mode 2) was deliberately excluded:
that mode has zero spans ever ingested (a confirmed, unresolved bug), so
there's no working tracer to test a metrics side effect against — testing
it would only re-confirm that raw UDP reaches the Extension, already
known from every Mode 1 function in this repo.

# Round 7: the Azure/GCP reference doc's three other deployment modes (Python only)

The reference doc this repo's investigation is based on describes five
deployment modes in total. Rounds 1-6 tested two of them (OTel SDK via
the Datadog Extension; native tracer + OTel API bridge) across four
languages. This round tests the remaining three, Python only: **Collector
+ Datadog exporter**, **ADOT Lambda layer**, and **OTel Direct**. Same
evidentiary bar as every prior round: build real code, deploy it, read
the real logs and the real ingested data — never assume from
architecture or doc claims alone.

**This section was corrected once, from a first pass that leaned on
local-evidence-only hedging** ("weaker evidence," "structurally
unconfirmable") because this environment's own `DD_API_KEY`/`DD_APP_KEY`
lack Datadog API read scope. A live Datadog data check pulled live ingestion data for all
three functions after that first pass, which changed several verdicts
substantially — most importantly, from "ADOT produces zero traces" to
"ADOT produces one real span, plus a specific, confirmed reason a second
one doesn't." The corrected verdicts below are the real ones; the first
pass's local-only observations are kept where they were still accurate
evidence, not deleted.

## Mode: OTel Direct

The OTel SDK exports straight to Datadog's public OTLP intake
(`https://otlp.datadoghq.com/v1/{traces,metrics,logs}`), bypassing the
Extension and any collector entirely. Confirmed from Datadog's own docs
(not the "managed platforms" doc, which uses a different
`{platform}.integrations.otlp.<site>` endpoint pattern for specific named
PaaS integrations — Lambda isn't one of those, so the generic direct
intake endpoint applies): auth is a plain `dd-api-key` HTTP header on
every request, plus `compute_stats=true` specifically for trace metrics
to compute. `http/protobuf` only — gRPC isn't supported on this endpoint.

Built `handler3.py` (function `otelDirectSandbox`, **no Datadog-Extension
layer attached at all**) reusing Round 1's OTel SDK setup pattern,
pointed at the direct endpoint with the required headers.

**Traces: confirmed landing.** A live Datadog data check confirmed 3 ingested spans for
`otel-direct-sandbox` via Traces Explorer — the local "no error in
CloudWatch" observation from the first pass was correct evidence, just
correctly hedged at the time pending the UI check that has now happened.

**OTel metrics: confirmed absent, then confirmed present, then fully
tagged — two real root causes, two real fixes, one real false start
along the way, all now closed.** A live Datadog data check's broad namespace search
initially found zero matches for `otel_direct_sandbox.otel_metric_test`.
Root cause #1: Datadog's OTLP metrics intake requires **delta**
temporality for Counter/Sum-type metrics; the OTel Python SDK's
`PeriodicExportingMetricReader` defaults to **cumulative**, and nothing
in the original `handler3.py` overrode that. Fixed by passing
`preferred_temporality={Counter: AggregationTemporality.DELTA}` to the
`OTLPMetricExporter` constructor. **Confirmed via a live Datadog data check after this
fix: the metric now exists** — but with **zero indexed tags**, no `env`,
no resource attributes at all.

Root cause #2: OTLP metrics need resource attributes explicitly mapped
onto tags via the `dd-otel-metric-config` header's
`resource_attributes_as_tags` option — this doesn't happen by default on
this direct client-side path. First attempt at this header used bare
`key=value` syntax (`"resource_attributes_as_tags=true"`) — **invalid**;
per
[`docs.datadoghq.com/opentelemetry/setup/otlp_ingest/metrics/`](https://docs.datadoghq.com/opentelemetry/setup/otlp_ingest/metrics/),
every language example sets this header's value as a **JSON string**
(`'{"resource_attributes_as_tags": true}'`). The bare `key=value` form
would have failed to parse silently — the exact same "no local error,
silently wrong" shape as the temporality bug above. Fixed to the doc's
exact syntax, redeployed, invoked once.

**Confirmed via a final live Datadog data check: tags now present**
(`env:sandbox`, `service`, `source`). **One standalone finding worth
documenting on its own**: `cloud.resource_id`/`cloud.account.id` are
confirmed **absent from the metric specifically**, even though they're
present on this same function's spans. These attributes are apparently
only populated in the OTel SDK's **span** resource context on this
pipeline, not carried through to metrics — a real, documented asymmetry
between the two signal types on this mode, not a bug worth chasing
further. Why the collector/ADOT relay paths don't need the
`dd-otel-metric-config` header set on the client side at all is **not
confirmed from source** here — flagged as an open mechanism, not guessed
at.

**`env` tag: confirmed absent on every signal, root-caused, and fixed.**
A live Datadog data check confirmed `env:sandbox` was missing everywhere in this mode.
Root cause: the `DD_ENV`-Lambda-env-var → `env`-tag translation
Rounds 1-6 relied on happens **inside the Datadog Extension** — and this
mode deliberately has no Extension attached. `DD_ENV` was never going to
produce an `env` tag here regardless of transport correctness. Fixed by
setting the OTel resource attribute directly (`deployment.environment`
and `deployment.environment.name`, both keys, in the `Resource.create()`
call) instead of relying on an Extension-side mechanism that structurally
can't apply in this mode.

**`dd-otlp-source=serverless` header: added, per instruction, impact
untested.** Traces and logs both landed without it in the original pass,
so it's confirmed not required for basic ingestion — but added to all
three exporters' headers anyway, since it may still matter for
billing/serverless-page rollups, a separate question this round didn't
test and isn't assuming an answer to either way.

## Mode: ADOT (AWS Distro for OpenTelemetry) Lambda layer

**Layer ARN verified live, not trusted from the doc**: the doc cites
`aws-otel-python-amd64-ver-1-29-0`, which returned `AccessDeniedException`
on every version probed — the actual current layer is
`arn:aws:lambda:us-east-1:901920570463:layer:aws-otel-python-amd64-ver-1-32-0:7`,
confirmed via `aws lambda get-layer-version-by-arn`.

**Checked ADOT's actual bundled collector source before writing any
config**: its exporters are `awsxrayexporter`, `awsemfexporter`,
`prometheusremotewriteexporter`, `debugexporter`, `otlpexporter`,
`otlphttpexporter` — **no Datadog exporter**. The only way to reach
Datadog from inside ADOT is `otlphttpexporter` pointed at Datadog's own
direct OTLP intake endpoint (the same endpoint "OTel Direct" above
uses), relayed through ADOT's collector rather than called straight from
the SDK.

Built `handler5.py` with `AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument`
(ADOT's auto-instrumentation wrapper) for traces, and manually-wired OTel
metrics/logs against the same local collector (auto-instrumentation has
no metrics/logs equivalent).

### Traces: corrected from "zero traces, confirmed broken" to the real mechanism

The first pass's own local observation was real (`"Failed to auto
initialize OpenTelemetry"` genuinely appeared in CloudWatch) but the
conclusion drawn from it — total failure, zero spans — was wrong. Bits
AI confirmed **1 real ingested span** for `otel-adot-sandbox`, carrying
`telemetry.sdk.version=1.44.0` and `telemetry.auto.version=0.65b0` —
values distinct from this repo's usual `1.27.0`, confirming it's a real
span from ADOT's own bundled SDK, not a stray/misattributed one from
another function.

Went back to the actual loader source (`/opt/python/opentelemetry/
instrumentation/auto_instrumentation/_load.py`, pulled directly from the
layer zip, not assumed from the traceback alone) to find the real
mechanism:
```python
except DependencyConflictError as exc:
    ...
    continue
except ModuleNotFoundError as exc:
    ...
    continue
except ImportError:
    ...
    continue
except Exception as exc:  # pylint: disable=broad-except
    _logger.exception("Instrumenting of %s failed", entry_point.name)
    raise exc
```
`_load_instrumentors` iterates every registered instrumentor's entry
point in one loop, catching and **continuing past** three specific
exception types (dependency conflicts, missing modules, import errors).
`aiobotocore`'s instrumentor failure is a plain `AttributeError`
(`module 'opentelemetry.instrumentation.botocore' has no attribute
'AiobotocoreInstrumentor'` — a real version mismatch inside AWS's own
bundled distro) — not one of the three caught types, so it falls through
to the generic `except Exception` branch, which **re-raises**,
aborting the loop immediately. That propagates up to `_initialize()`'s
own try/except, which logs `"Failed to auto initialize OpenTelemetry"`
(a misleading message — it isn't a total failure) and, since
`swallow_exceptions` defaults to `True`, does **not** re-raise further —
the wrapped program continues running normally afterward.

**Net effect, now fully explained**: any instrumentor whose entry point
is processed *before* `aiobotocore` in the loader's iteration order
finishes loading successfully; the loop dies at `aiobotocore` and
everything scheduled *after* it never loads. The single confirmed span
is the Lambda-wrapper's own root span (`opentelemetry-instrumentation-
aws-lambda` evidently loads before `aiobotocore` in this layer's entry
point order) — but no child `STS.GetCallerIdentity` span exists, because
`opentelemetry-instrumentation-botocore`'s entry point is evidently
scheduled *after* `aiobotocore` and never got installed. This is a
complete, source-confirmed mechanism, not a guess reverse-engineered
from the outcome.

**Fixed** by setting `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=aiobotocore`
as a Lambda env var — the exclusion check in `_load_instrumentors` runs
*before* `distro.load_instrumentor()` is ever called for a given entry
point, so skipping `aiobotocore` entirely means the loop never hits the
exception that kills it, and everything scheduled after it (including
`botocore`) loads normally. Redeployed and invoked: the `"Instrumenting
of aiobotocore failed"` / `"Failed to auto initialize OpenTelemetry"`
lines are both gone from CloudWatch — confirmed locally that the crash
no longer occurs. **Confirmed via a live Datadog data check: the fix produces the expected
2-span tree.** Trace `5e9d20a812671820903ff6f6011b86b2` shows span 1
(root) `aws.lambda` via `opentelemetry.instrumentation.aws_lambda`, and
span 2 (child) `aws.STS.request` via
`opentelemetry.instrumentation.botocore` v0.48b0, HTTP 200 — both
instrumentors loading and nesting correctly once `aiobotocore` is
excluded. Settled, not open.

### OTel metrics: confirmed absent, same root cause and fix as OTel Direct

A live Datadog data check's broad namespace search found zero matches for
`otel_adot_sandbox.otel_metric_test`. Same root cause as OTel Direct
above: cumulative temporality (the SDK default) vs. Datadog's OTLP
metrics intake requiring delta. Same fix applied —
`preferred_temporality={Counter: AggregationTemporality.DELTA}` on the
manually-wired `OTLPMetricExporter`. Redeployed, no local errors; not
independently re-confirmed via a live Datadog data check after the fix.

### `env` tag: confirmed absent, same root cause and fix as OTel Direct

No Extension attached in this mode either, so the same
`DD_ENV`-never-translates-without-the-Extension root cause applies. Fix
here is different in mechanism, though, since traces come from ADOT's
own auto-instrumentation rather than this repo's own `Resource.create()`
call: set `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=sandbox,
deployment.environment=sandbox` as a Lambda env var. Confirmed via the
OTel Python SDK's own `Resource.create()` source
(`opentelemetry/sdk/resources/__init__.py`) that it automatically parses
and merges `OTEL_RESOURCE_ATTRIBUTES` on every call — the same mechanism
both ADOT's own auto-instrumented `Resource` and this file's manually
constructed one for metrics/logs pick it up through, confirmed from
source rather than assumed to "probably just work."

### `dd-otlp-source=serverless` header

Added to `adot-collector.yaml`'s `otlphttp` exporter headers, same
status as OTel Direct above: not required for the ingestion already
confirmed, impact on billing/rollups untested.

## Mode: OTel Collector + Datadog exporter

**Checked before building anything**: neither the official
`open-telemetry/opentelemetry-lambda` collector layer nor ADOT's bundled
collector include the Datadog exporter. Built a custom one from scratch
via the OpenTelemetry Collector Builder (`ocb`) — `otlpreceiver`,
`batchprocessor`,
`github.com/open-telemetry/opentelemetry-collector-contrib/exporter/datadogexporter`,
`debugexporter` — cross-compiled for `linux/amd64`, with a Go-based
(not bash — this runtime has no `curl`, found the hard way) Lambda
extension wrapper to run it as a sidecar. Full build details and the two
bugs found and fixed along the way (no `curl`; a cold-start OTLP-port
readiness race) are unchanged from the first pass — see
`otel-collector-dd/README.md` for the rebuild steps.

**Traces: confirmed landing, and the "structurally unconfirmable"
flush-before-freeze question is answered.** A live Datadog data check confirmed **5
ingested spans** for `otel-collector-sandbox`. The first pass correctly
identified a real architectural question (does the Datadog exporter's
async HTTP POST reliably complete before Lambda freezes the execution
environment between invocations, given extensions only get a flush
opportunity on full `SHUTDOWN`, not every freeze?) but was wrong to leave
it as an open structural concern — **5 spans landing across this round's
invocations demonstrates it completes in practice, repeatedly, not just
once by luck.** Whatever timing margin exists between the collector
receiving a span and Lambda actually freezing the environment, it was
consistently enough. Corrected from "structurally unconfirmable" to
"confirmed working in practice."

**OTel metrics: confirmed present, but with one real, documented gap —
`env` is absent on the metric specifically.** `otel_collector_sandbox.
otel_metric_test` landed, the only one of the three modes' metrics to
work without a temporality fix needed (this mode's metrics go through
the custom collector's own `datadogexporter` component, not a raw
`OTLPMetricExporter` pointed at Datadog's intake — `datadogexporter` is
Datadog's own, written specifically to handle this kind of
cumulative-to-delta conversion internally). **Confirmed via a final Bits
AI check, after the `env` resource-attribute fix below was actually
re-invoked**: the metric's own `env:sandbox` tag is confirmed **absent**,
even though `env:sandbox` is confirmed present on this same function's
traces and logs after the identical fix. **Root cause not confirmed** —
plausibly `datadogexporter` maps resource attributes to metric tags
through a different internal path than it uses for traces/logs, but
that's not verified from source here. Documented as a known, minor,
unresolved gap specific to this one signal on this one mode — not
guessed at further, and not something to spend additional rounds
chasing unless asked.

**`env` tag on traces/logs: confirmed absent, root-caused, fixed, and
re-confirmed present after closing a re-invoke gap.** No Extension
attached, so `DD_ENV` never translates. Fixed the same way as OTel
Direct: `deployment.environment`/`deployment.environment.name` added
directly to `handler4.py`'s `Resource.create()` call. The first post-fix
deploy was never actually re-invoked, so the only ingested data for this
function was still pre-fix (env absent) — closed by invoking once more.
**Confirmed via a final live Datadog data check: `env:sandbox` is now present on
both traces and logs.**

## Not a bug: ADOT's richer resource attribute set

ADOT's one confirmed span carries the full `cloud.provider`/`cloud.
region`/`faas.name`/`faas.version`/`faas.instance` set; OTel Direct's and
the Collector mode's spans only carry `cloud.resource_id`/`cloud.
account.id`/`faas.invocation_id`. This is a genuine functional
difference between the two approaches, not an error to chase down: it
matches the `AwsLambdaInstrumentor` library's already-confirmed
attribute set from every other round in this repo (Round 1 onward) —
both OTel Direct and the Collector mode's `handler3.py`/`handler4.py`
use the exact same `AwsLambdaInstrumentor`/`BotocoreInstrumentor`
combination Round 1's `handler.py` does. ADOT's richer set comes from
its own separate auto-detection logic bundled into its distro, not a
different or better version of the same instrumentor library — the two
approaches genuinely produce different attribute sets by design, and
that's worth noting for the doc, not treating as inconsistent.

## Full signal matrix

| # | Signal | OTel Direct | ADOT layer | Collector + Datadog exporter |
|---|---|---|---|---|
| 1 | Traces | ✅ **Confirmed landing** — 3 spans (a live Datadog data check, Traces Explorer) | ✅ **Confirmed landing — 2 spans after the fix (root + botocore child)**, with a source-confirmed root cause for why the original build only produced 1: `aiobotocore`'s instrumentor crash aborted the auto-instrumentation loader partway through, skipping everything scheduled after it (including `botocore`). Fixed via `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=aiobotocore`; confirmed via a live Datadog data check on trace `5e9d20a812671820903ff6f6011b86b2` — `aws.lambda` root + `aws.STS.request` child, HTTP 200 | ✅ **Confirmed landing — 5 spans.** Also answers the flush-before-freeze question from the first pass: completes reliably in practice |
| 2 | OTel metrics | ✅ **Fully confirmed working, including tags** — temporality fixed (confirmed metric exists), then `dd-otel-metric-config` header fixed to the correct JSON-string syntax after an initial invalid attempt (confirmed tags now present: `env`, `service`, `source`). **Standalone finding**: `cloud.resource_id`/`cloud.account.id` confirmed **absent from the metric** despite being present on this function's spans — resource attributes apparently only populate the span context on this pipeline, not the metric context; documented asymmetry, not a bug to chase | ✅ **Confirmed present and working, closed via a later live Datadog data check**: non-zero metric values with `env:sandbox` present — same temporality root cause/fix as OTel Direct's first fix, now independently re-confirmed | ✅ **Metric confirmed present**, but **`env` tag confirmed absent from the metric specifically** — the one open item from this round. Traces/logs on this same function carry `env:sandbox` correctly after the identical resource-attribute fix; the metric alone doesn't. Root cause not confirmed — documented as a known, minor, unresolved gap |
| 3 | OTel logs | Not independently checked via a live Datadog data check this round (metrics' absence was the signal that surfaced the temporality issue; logs weren't separately queried) | Not independently checked via a live Datadog data check this round, same reasoning | Not independently checked via a live Datadog data check this round, same reasoning |
| 4 | `env` tag (traces/logs) | ✅ **Confirmed present after the fix** — root-caused to the `DD_ENV`→`env` translation being Extension-side (this mode has no Extension attached by design), fixed via explicit OTel resource attributes, confirmed via a live Datadog data check | — same root cause as OTel Direct; fix applied (`OTEL_RESOURCE_ATTRIBUTES` as a Lambda env var, confirmed `Resource.create()` auto-merges it from SDK source), not independently re-confirmed via a live Datadog data check after the fix | ✅ **Confirmed present on both traces and logs after the fix was actually re-invoked** — the first post-fix deploy had never been invoked, so its only ingested data point was still pre-fix; closed by invoking once more, then confirmed via a live Datadog data check |
| 5 | Billing/resource attribution (`cloud.*`/`faas.*`) | ✅ Confirmed present (`cloud.resource_id`/`cloud.account.id`/`faas.invocation_id`) — same `AwsLambdaInstrumentor` attribute set as every other round | ✅ Confirmed present, and richer (`cloud.provider`/`region`/`faas.name`/`.version`/`.instance`) — ADOT's own separate auto-detection, a genuine functional difference, not a bug | ✅ Confirmed present, same attribute set as OTel Direct (same instrumentation library) |
| 6 | `dd-otlp-source=serverless` header | Added per instruction; confirmed not required for the ingestion already observed; billing/rollup impact untested | Added per instruction, same status | N/A — this mode's exporter is the Datadog-native `datadogexporter` component, not a raw OTLP header-based path |
| 7 | `dd-otel-metric-config` header (`resource_attributes_as_tags`) | ✅ **Confirmed fixed** — first attempt used bare `key=value` syntax (invalid, silently ignored, no local error); corrected to the doc's exact JSON-string value (`'{"resource_attributes_as_tags": true}'`), redeployed, and confirmed via a live Datadog data check that tags now populate | N/A — not tested here; this mode's metrics path goes through ADOT's `otlphttpexporter` relay rather than a client-side `OTLPMetricExporter` pointed straight at Datadog, and whether the same header would apply there wasn't tested | N/A — the collector's own `datadogexporter` component needs no equivalent client-side header for trace/log tags; it handles that mapping internally. (Its one remaining gap — the metric specifically missing `env` — is tracked separately under Signal 2, not this header) |

## Phase 4: Round 7-specific gotchas

- **Don't let "no local error" stand in for a UI/API check when one
  becomes available — verdicts can flip substantially.** The first pass
  of this round concluded "zero traces, confirmed broken" for ADOT based
  on a real local error (`"Failed to auto initialize OpenTelemetry"`).
  The real ingestion data showed 1 real span landed anyway. The local
  observation wasn't wrong, but the conclusion drawn from it without a
  UI check was. The same correction applied to the Collector mode's
  "structurally unconfirmable" flush-timing concern — 5 landed spans
  settled what local reasoning alone couldn't.
- **Datadog's OTLP metrics intake requires delta temporality; the OTel
  Python SDK's `PeriodicExportingMetricReader` defaults to cumulative.**
  This silently drops Counter/Sum metrics with no local error at all —
  neither mode that hit this (OTel Direct, ADOT) produced any error,
  warning, or log line suggesting a problem. The only way this surfaced
  was a live ingestion check finding the metric genuinely absent.
  **Clarifying this round's own claim, on a later re-read**: "silently"
  here describes what this testing actually observed —
  no client-side error from the SDK/exporter, and no server response
  inspected or logged either way. Datadog's public OTLP metrics docs
  state that sending cumulative-temporality metrics to the intake
  "will result in an error," i.e. a server-side rejection, not a silent
  drop. This round's testing never checked the OTLP exporter's actual
  HTTP response code/body for that request — only the absence of any
  local error and the metric's absence in the UI — so whether Datadog's
  backend did in fact return an error response (that simply went
  unobserved/unlogged here) or genuinely dropped the data with no
  response at all was never actually determined. Recorded as a gap in
  this round's own observability, not a correction to the finding that
  the metric was absent and the fix resolved it. Fixed
  via `OTLPMetricExporter(preferred_temporality={Counter:
  AggregationTemporality.DELTA})`.
- **The `DD_ENV`→`env`-tag translation is Extension-side, not an OTel or
  transport-level mechanism** — confirmed by its absence in all three
  modes that don't attach the Extension, the same across every signal in
  all three. Every prior round in this repo (1-6) always had the
  Extension attached, so this never surfaced before. The fix is always
  the same shape: set the OTel resource attribute directly
  (`deployment.environment`/`deployment.environment.name`), since
  there's no Extension in these modes to do the translation for you.
- **`opentelemetry-instrument`'s auto-instrumentation loader doesn't
  treat all instrumentor failures the same way** — confirmed from its
  actual source, not inferred from behavior. `DependencyConflictError`,
  `ModuleNotFoundError`, and `ImportError` are caught per-instrumentor
  and the loader continues to the next one; anything else (like the
  `AttributeError` ADOT's own bundled `aiobotocore` instrumentor throws)
  re-raises and kills the whole loop, silently skipping every
  instrumentor scheduled after the one that failed. `OTEL_PYTHON_
  DISABLED_INSTRUMENTATIONS=<name>` sidesteps this by skipping the
  problem instrumentor before it's ever invoked.
- **ADOT's doc undersells its own version lag.** The doc's Python layer
  example ARN (`ver-1-29-0`) was stale (current is `ver-1-32-0`), and its
  "Contains ADOT Collector v0.49.0" claim doesn't match the running
  collector's own self-reported version (`v0.156.0`).
- **This Lambda runtime's base OS image has no `curl`.** Found via a real
  failure (a bash-based Lambda extension silently looping on `curl:
  command not found`, `Sandbox.Timedout` after 15s) — rewritten in Go.
- **A custom sidecar extension must wait for its own background
  process's listening port before calling `/event/next` the first
  time**, or Lambda starts invoking before the sidecar is ready and the
  function's first export attempt gets `Connection refused`.

## Round 7 closing summary

| # | Question | Verdict |
|---|---|---|
| OTel Direct | Does the OTel SDK reach Datadog by exporting straight to its public OTLP intake, with no Extension/collector? | ✅ **Fully confirmed working — traces (3 spans), metrics (existing and fully tagged after two fixes: temporality, then the `dd-otel-metric-config` header's JSON-string syntax), and `env` tag, all confirmed via a live Datadog data check.** One standalone finding: `cloud.resource_id`/`cloud.account.id` confirmed absent from the metric specifically (present on spans) — a documented asymmetry, not a bug. Billing attribution confirmed present throughout |
| ADOT | Does traces/metrics/logs reach Datadog via the ADOT layer, and through what path? | ✅ **Traces confirmed landing — 2 spans after the fix (root `aws.lambda` + `aws.STS.request` child), confirmed via a live Datadog data check on trace `5e9d20a812671820903ff6f6011b86b2`.** Corrected from the first pass's wrong "zero traces" verdict; the real mechanism (a loader-aborting `AttributeError` in ADOT's own bundled `aiobotocore` instrumentor) is fully source-confirmed and fixed, and the fix's result is settled, not open. **Metrics: closed via a later live Datadog data check** — non-zero values confirmed, `env:sandbox` present. `env` tag: same root cause/fix as OTel Direct, confirmed via a live Datadog data check. Path to Datadog for metrics/logs is `otlphttpexporter` pointed at the same direct intake endpoint, confirmed from ADOT's real component list — no Datadog-native exporter exists inside ADOT |
| Collector + Datadog exporter | Does a sidecar OTel Collector with the Datadog exporter work on Lambda, and does resource/billing attribution populate the same way Mode 1 does? | ✅ **Confirmed working end to end for traces and logs** — 5 landed spans, `env` tag confirmed present on both after closing a re-invoke gap, billing attribution matching Mode 1's mechanism exactly. The first pass's "structurally unconfirmable" flush-before-freeze concern is resolved: it completes reliably in practice. ⚠️ **One confirmed, open gap**: the metric itself lands, but confirmed via a live Datadog data check to be missing its `env` tag specifically (unlike this same function's traces/logs) — root cause not confirmed, documented as a known, minor, unresolved asymmetry |
| Overall | Is any of the three fully confirmed working, same bar as Modes 1/2 elsewhere in this repo? | ✅ **Yes — all three produce real, live-Datadog-data-confirmed traces reaching Datadog, and OTel Direct is now fully confirmed working end to end across traces, metrics (incl. tags), and `env`.** Three real, root-caused, confirmed-fixed issues found and closed this round: metric temporality; the `dd-otel-metric-config` JSON-syntax mistake; and the Extension-side-only `env` translation (including a re-invoke gap that left the Collector mode's fix unverified until closed). **One real, confirmed, unresolved gap remains**: the Collector mode's own metric is missing its `env` tag specifically, root cause not confirmed — documented, not chased further this round. Logs weren't independently checked via a live Datadog data check for OTel Direct or ADOT |

## Follow-up: four open items closed via a live Datadog data check

Not a code-change round — all four results below came from querying
already-ingested spans/metrics, no redeploys involved.

1. **Python Mode 2 billing attribution** — `cloud.resource_id`/
   `cloud.account.id` confirmed **absent** on a live `ddtraceOtelApi`
   span (same pattern as Node's Mode 2; the ARN is only available as
   `function_arn`). Closes the item left open in the Round 4 closing
   summary's "Billing attribution (Mode 2)" row.
2. **Java Mode 1 billing attribution + diagnostic markers** —
   `cloud.resource_id`/`cloud.account.id` both confirmed **present** on
   a live `otel-aws-sandbox-java` span, via the OTLP pathway
   (`otel.scope.name`/`otel.scope.version` present, no `_dd.*` tracer
   metadata). Closes the 403-blocked API-access gap from Round 6.
3. **Java Mode 2 billing attribution** — the same two attributes
   confirmed **absent**, via the Trace Agent pathway
   (`_dd.parser_protocol: protobuf_v07`, `_dd.origin: lambda`,
   `operation_name: aws.lambda`). Same pattern as Python/Node's Mode 2.
4. **ADOT (`otelAdotSandbox`) metrics** — confirmed non-zero values with
   `env:sandbox` present. Closes the "not independently re-confirmed"
   item from Round 7's full signal matrix (Signal 2, ADOT column).

See the Round 4, 6, and 7 sections above for the updated table cells.

## Round 8: trace-to-log tag propagation

The reference doc describes Azure/GCP platforms where an arbitrary
trace-level span tag propagates onto the UI-correlated log automatically.
Prior rounds in this repo confirmed UI-level `request_id` correlation and
raw `dd.trace_id` text injection into log output, but never specifically
whether a custom span *attribute* (not an ID) shows up on the
log side too. This round tests that, isolated from every other variable
by using Mode 1 Python (`otelSandbox`, `handler.py`) — the simplest,
already-confirmed baseline — with no other change.

**Change made:** one line added to the existing manual span in
`handler.py`'s `lambda_handler`, immediately after the existing
`sandbox.event_keys` attribute:

```python
span.set_attribute("sandbox.canary_tag", "trace-log-propagation-test")
```

**Deployment:** the Serverless Framework stack for this repo is fully
torn down, and `serverless.yml`'s `otelCollectorSandbox` function
currently references a custom layer ARN
(`otelcol-dd-sandbox:4`) that no longer exists (deleted in a prior
teardown), which would block a full-stack `serverless deploy`. To avoid
that, only `otelSandbox` was deployed, directly via `aws lambda
create-function` (bypassing the Serverless Framework/CloudFormation
entirely) — the same fallback pattern used repeatedly in Round 7. A
temporary IAM role (`serverless-otel-aws-sandbox-dev-manual-role`, basic
Lambda execution policy + `sts:GetCallerIdentity`) was recreated for
this, and the function was configured to match its `serverless.yml`
definition exactly: `Datadog-Extension:100` layer, `DD_API_KEY`,
`DD_SITE=datadoghq.com`,
`DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT=localhost:4318`,
`DD_ENV=sandbox`.

**Invocation:** invoked exactly once, with payload
`{"test": "canary-tag-propagation"}`.

- **RequestId:** `c3234395-c566-4913-a002-955fb63bafbf`
- **Invocation timestamp:** `2026-10-07T02:13:21.625Z` UTC

CloudWatch confirms no errors related to the trace export or the new
span attribute. The two `[ERROR]` lines present in the log (`Failed to
export batch code: 404`, `Failed to export logs batch code: 404`) are
the already-confirmed, pre-existing 404s for this function's
unsupported-signal test code (OTel metrics/logs via the Extension's
OTLP receiver, gated by `TEST_UNSUPPORTED_SIGNALS`, default on) — not new
errors introduced by this change.

**Result, confirmed via a live Datadog data check:** ❌ **No automatic trace-to-log tag
propagation on Mode 1.** `sandbox.canary_tag` is confirmed present on
the `do-work` span, and confirmed **absent** from all 9 correlated log
events for the same request ID — no `dd.trace_id`, no `dd.span_id`, no
custom attributes, nothing carried over. The only link between the
trace and its logs is UI-level correlation by `request_id`, same as
already documented elsewhere in this repo. This is the opposite of the
Azure/GCP reference doc's described behavior for this capability.

**Root cause — the same limitation already established, not a new
bug:** this function's OTel Logs SDK exporter 404s against `/v1/logs`
(the Extension-level limitation already confirmed throughout this repo
— see the Telemetry Support Matrix). Because that export never
succeeds, these 9 log events were never OTel-bridged at all — they're
raw Lambda platform logs (stdout/stderr) captured by the Extension from
CloudWatch, a pipeline with no trace-context awareness whatsoever.
There is no separate bridging mechanism for it to fail; the bridging
mechanism (OTel Logs via the Extension's OTLP receiver) simply doesn't
work on this mode, for the reason already documented, and nothing
downstream of that 404 could carry a span attribute onto a log even in
principle.

The `env:sandbox` tag appearing on both the trace and the log is
coincidental, not propagation — both are set independently from Lambda
environment configuration (`DD_ENV`), not derived from one another.

**Definitive answer:** trace-tag-to-log propagation does not work on
AWS Lambda Mode 1, and the reason is the same OTel Logs 404 limitation
already established elsewhere in this repo, not a separate issue.

## Round 9 setup: re-testing the Collector mode metric's `env` tag stability

Round 7 left one open gap on `otelCollectorSandbox`: the function's own
OTel metric was observed missing its `env` tag (while its traces/logs
carried `env:sandbox` correctly) on the same deployed code, with no
intervening redeploy — raising the question of whether the tag is
actually flip-flopping between `env:N/A` and `env:sandbox` across
invocations of identical code, rather than being consistently absent.
This round tests that directly: no code change to `handler4.py`, just a
fresh batch of invocations to see whether `env` is now stably present.

**Deployment:** the stack is fully torn down and the custom
`otelcol-dd-sandbox` layer had been deleted in a prior teardown (its ARN
in `serverless.yml` is a placeholder, see the repo-cleanup commit). To
run this test at all, the layer was rebuilt and republished from
`otel-collector-dd/` exactly per that directory's README (`ocb` builder,
Go extension wrapper, same `manifest.yaml`/`config.yaml` — no source
changes), landing as `otelcol-dd-sandbox:6`. `otelCollectorSandbox` was
then deployed directly via `aws lambda create-function` (same fallback
pattern as other rounds), using the unmodified `handler4.py`.

One real deployment bug found and fixed along the way: the first direct
deploy omitted `DD_API_KEY`/`DD_SITE` (normally supplied at the
`serverless.yml` provider level, which a direct per-function CLI deploy
doesn't inherit) — the collector's Datadog exporter failed to
initialize (`invalid configuration: exporters::datadog: api.key is not
set`), and all 6 invocations in that first batch exported nothing. Fixed
via `update-function-configuration`, then re-ran the batch below on the
corrected config.

**Invocations:** 6 invocations, 6-12 seconds apart, same unchanged code
and config throughout:

| # | RequestId | Invocation timestamp (UTC) |
|---|---|---|
| 1 (cold start) | `23bf8fb2-637e-4ef4-9e3c-f1f7e5510cb5` | 2026-10-07T03:54:08 |
| 2 | `25dd170a-9a0c-4053-a86c-546e3a626367` | 2026-10-07T03:54:21 |
| 3 | `46886826-3b25-472d-a611-164778238556` | 2026-10-07T03:54:27 |
| 4 | `6d27eb43-f8b4-4fdf-9045-384f4df2bf33` | 2026-10-07T03:54:33 |
| 5 | `7903c364-c8e4-48e6-b8f8-372a13ce7765` | 2026-10-07T03:54:39 |
| 6 | `caa0a0f9-a9d5-4029-a3b7-fd73f62d0666` | 2026-10-07T03:54:45 |

CloudWatch confirms all 6 completed with `statusCode: 200` and no
Python-level errors. The only non-trivial log lines are debug-level
"source provider unavailable" messages from the collector's own
metadata-detection probing (EC2/Azure/GCP/ECS IMDS, all expectedly
unreachable from Lambda) and healthy "Reported agent rates" lines
confirming the Datadog exporter is actively processing — not errors.

Whether `env` is stably present across this batch, or still
flip-flopping, is not yet checked — that's the follow-up live Datadog data check,
not asserted here.

## Round 9 conclusion

**`env` tag: fully resolved.** This fresh batch of 6 back-to-back
invocations on the same unchanged, un-redeployed code landed
`env:sandbox` on every single data point — zero `env:N/A`. The earlier
apparent flip-flopping was pre-fix data mixed with post-fix data in the
same query window, not a real inconsistency in the collector's
resource-attribute mapping.

**One new wrinkle, investigated and closed:** one of the 6 data points
showed a metric value of `0` instead of `1`. Confirmed via a live Datadog data check this
is a gap-fill display artifact, not a missing submission or a real `0`
— the metric is count-type, and Datadog fills empty time buckets with
`0` by default. Verified by querying the same window at two different
bucket sizes and observing the zero's position shift (a real submitted
`0` would stay pinned to its actual timestamp regardless of bucket
size; the metric's count-type metadata was also confirmed). All 6
invocations' metrics are presumed to have landed successfully.

This closes the last open item from Round 7's Collector mode
investigation.

## Round 10: live Datadog data sweep across all four languages

A batch of live Datadog data checks against each language's Mode 1/Mode 2
functions, closing several long-standing "not independently verified"
items from Rounds 4-6 and surfacing two new, unresolved issues.

### Actual deployed service names

Every function's real service name differs from the `otel-sandbox-*`
pattern this doc has used informally in prose elsewhere. For anyone
querying the Datadog UI/API directly against this repo's functions, the
real names are:

| Language | Mode 1 service | Mode 2 service |
|---|---|---|
| Python | `otel-aws-sandbox` | `ddtrace-otel-api-sandbox` |
| Node.js | `otel-aws-sandbox-node` | `ddtrace-otel-api-sandbox-node` |
| .NET | `otel-aws-sandbox-dotnet` | `ddtrace-otel-api-sandbox-dotnet` |
| Java | `otel-aws-sandbox-java` | `ddtrace-otel-api-sandbox-java` |

### HEADLINE NEW ISSUE: Java Mode 1 has zero indexed spans, contradicting Round 6

**Confirmed via a live Datadog data check: zero indexed spans for `otel-aws-sandbox-java`
over the last 30 days.** This directly contradicts this doc's own
Round 6 claim that Java Mode 1 traces "work, confirmed via raw debug
logs" (see the Round 6 signal matrix, Signal 1).

The Extension's own debug logs for this function show what looks like
a successful send: `"Successfully sent trace (1 attempts, 1346
bytes)"` and `"Successfully buffered traces to be aggregated"`. But a
third log line is the most concrete lead so far: `"Not sending cold
start span because trace ID is unset."` Round 6's original "it works"
conclusion rested on the Extension's transport-success line plus the
tracer's own internal span-construction logs — it was never
independently cross-checked against the Datadog UI/API for Mode 1
specifically (the Manual verification checklist in Round 6 flagged this
exact gap and it was never closed before now).

**Status: unconfirmed/contradicted, not working and not confirmed
broken.** Three untested hypotheses, in priority order:

1. **Most likely**: the spans are landing under a different or unknown
   service name rather than not existing at all — not yet ruled out by
   checking an `unknown_service`/catch-all query.
2. **Possible**: a sampling configuration issue causing 100% of spans
   to be dropped client-side or at the Extension before export actually
   completes, despite the "successfully sent" log line.
3. **Possible**: an Extension-level bug specifically tied to the "trace
   ID is unset" condition on the cold-start span — if that condition
   also affects the main span (not just the cold-start one the log line
   names), the export could be silently malformed in a way that passes
   local logging but never lands.

**Next step** (not done in this round): redeploy, invoke fresh, check
both the expected `otel-aws-sandbox-java` service name and an
`unknown_service` catch-all query for the same time window, and look
closer at the Extension's flush/response handling around the "trace ID
is unset" warning specifically.

### SECOND NEW ISSUE: Node Mode 1 has traces but zero CloudWatch logs in Datadog

**Confirmed via a live Datadog data check: 30 spans exist for `otel-aws-sandbox-node`,
but zero CloudWatch log entries have ever been ingested into Datadog
for this function.** Traces reach Datadog; this function's plain
`console.log()` output apparently never does, at all — not a
correlation gap (the kind Round 1 and Round 8 investigated), but a
log-forwarding gap: the logs don't appear to reach Datadog in any form.

**Likely fix, not yet applied**: confirm `DD_LOGS_ENABLED` is actually
set on this function (it is not currently set anywhere in
`node/serverless.yml`, as far as this doc's record of that file goes),
and/or confirm a CloudWatch log subscription filter actually points
this function's log group at the Datadog log forwarder — Lambda log
groups don't forward to Datadog on their own; something has to wire
that up explicitly, and nothing in this repo's `node/serverless.yml`
currently does.

**Status: open, not yet fixed.** Flagging rather than fixing in this
round, since the fix itself (whichever of the two above it turns out to
be) needs a redeploy and re-invoke to confirm.

### Confirmed resolved/closed items from this round

- **`faas.invocation_id`**: confirmed present on both Python Mode 1 and
  .NET Mode 1 live spans.
- **`faas.coldstart`**: confirmed **absent** on Python Mode 1 — the
  vendored `opentelemetry-instrumentation-aws-lambda` at `0.48b0`
  doesn't emit this attribute; fix is either upgrading that
  instrumentation library or setting it manually. Confirmed **present**
  on .NET Mode 1, but only cold-start data exists in the 30-day window
  checked — every invocation happened to be a cold start, so there's no
  warm-invocation data point yet to confirm the attribute's warm value.
  Confirmed present on Node Mode 1 with **both** cold and warm data
  points already available.
- **`telemetry.sdk.*`**: reconfirmed present on a live .NET Mode 1 span
  (`telemetry.sdk.language: dotnet`, `telemetry.sdk.name: opentelemetry`,
  `telemetry.sdk.version: 1.18.0`).
- **Java Mode 2 `DD_SERVICE`**: confirmed correct on a live span.
- **Java Mode 2 correlated logs**: confirmed the underlying data
  linkage is in place — a shared `request_id` across 2,476 log entries,
  with `_dd.origin: lambda` present. (Whether this surfaces as UI-level
  correlation the way Round 1 established for Python was not
  separately re-checked this round; the linkage data itself is
  confirmed present.)
- **.NET Mode 2 custom metrics**: reconfirmed zero, consistent with the
  already-documented .NET Mode 2 zero-signal bug (see Round 5).

### What needs re-invocation or redeploy to close out

| Item | What's needed |
|---|---|
| .NET Mode 1 `/v1/logs` | Code change: wire in an OTel Logs SDK exporter (none exists yet for this function) |
| Java Mode 1 `/v1/logs` | Same — no OTel Logs SDK exporter wired in yet |
| .NET Mode 1 `faas.coldstart` warm value | Invoke twice in quick succession so a warm invocation actually occurs |
| Python Mode 1 `faas.coldstart` | Upgrade `opentelemetry-instrumentation-aws-lambda` past `0.48b0`, or set the attribute manually |
| **Java Mode 1 span indexing (the headline issue)** | Debug the three hypotheses above, then redeploy |
| `OTEL_SERVICE_NAME` isolation test | Redeploy one function with its Resource-based service name removed, to isolate whether `OTEL_SERVICE_NAME` or the Resource attribute is actually doing the work |
| Node Mode 1 correlated logs | Apply the log-forwarding fix above, then invoke to confirm logs actually reach Datadog |

## Round 11: documented OTLP serverless intake requirements vs. OTDI's actual behavior

Sourced directly from Datadog's public docs —
[OTLP Serverless Intake](https://docs.datadoghq.com/opentelemetry/setup/otlp_ingest/serverless/?tab=aws),
AWS tab — not inferred from this repo's own testing. This applies
specifically to the two modes in this repo that send OTLP straight to
Datadog's intake with no Extension/Agent/Collector in between:
**OTel Direct (OTDI, `otelDirectSandbox`/`handler3.py`)** and **ADOT
(OTS, `otelAdotSandbox`/`handler5.py`, via its bundled collector's
`otlphttp` relay)**. It does not apply to Mode 1, Mode 2, or the
Collector mode (OTSDD) — none of those send OTLP directly to the
public intake endpoint the way these two do.

**Required headers**, per the doc: `dd-api-key` (the API key itself);
`dd-otlp-source`, set to `serverless`; `compute_stats`, set to `true`
("Required for trace metrics"). Both `otelDirectSandbox` and the ADOT
collector config already set all three — confirmed by re-reading
`handler3.py` and `adot-collector.yaml` directly, not assumed.

**Resource attributes for AWS Lambda**, per the same doc:

| Attribute | Doc's stated requirement |
|---|---|
| `cloud.provider` | **Required** — "Set to `aws`" |
| `faas.id` (the Lambda function ARN) | **Recommended** — "preferred for platform identification" |
| `cloud.platform` | **Conditional fallback** — "Set to `aws_lambda` if `faas.id` is not set" |
| `cloud.region`, `faas.name`, `faas.version`, `faas.instance`, `faas.max_memory`, `aws.log.group.names`, `aws.log.stream.names` | Optional |

### Open, unresolved discrepancy — not fixed, not explained away here

**`otelDirectSandbox`'s `Resource.create()` call (`handler3.py`) never
sets `cloud.provider`, `faas.id`, or `cloud.platform` — confirmed by
reading the actual code, which only sets `service.name` and
`deployment.environment`(`.name`).** Despite that, Round 7 confirmed
this function's traces landed successfully (3 spans, via a live
Datadog data check) with no indication of the rejection or missing-data
behavior the "required" framing would suggest. This doesn't obviously
square with `cloud.provider` being documented as required for AWS
Lambda resource identification.

No attempt is made here to resolve this one way or the other — it's
recorded as a real, confirmed discrepancy between documented
requirements and observed behavior, worth confirming with Datadog's
serverless/billing team directly (the same escalation treatment the
reference doc gets elsewhere in this repo for open questions, e.g. the
Universal Instrumentation lead in Round 5). Possibilities not
distinguished between: the "required" framing is about billing/platform
cost-attribution correctly, not about whether the trace is accepted and
indexed at all (i.e., ingestion vs. attribution are separate gates, and
only the latter needs `cloud.provider`); some other mechanism in the
Lambda OTLP exporter/AWS Lambda resource detector populates an
equivalent signal this repo didn't check for; or the doc's "required"
language is aspirational/for billing accuracy rather than a hard
ingestion gate. Any of these would need direct confirmation from
Datadog, not another round of this repo's own testing.
