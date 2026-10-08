# Python sandbox

How to deploy and run this language's five functions. For the full
investigation, evidence, and how each of these conclusions was reached,
see the root [`FINDINGS.md`](../FINDINGS.md) and
[`BUILD_WALKTHROUGH.md`](../BUILD_WALKTHROUGH.md).

| | Function | Mode |
|---|---|---|
| 1 | `otelSandbox` (`handler.py`) | OTel SDK `TracerProvider`, exporting via OTLP to the Datadog Lambda Extension's local receiver. No Datadog tracing layer. |
| 2 | `ddtraceOtelApi` (`handler2.py`) | Native `ddtrace` + OTel API bridge (`DD_TRACE_OTEL_ENABLED=true`), via the Datadog Python tracing layer. |
| 3 | `otelDirectSandbox` (`handler3.py`) | OTel SDK exports straight to Datadog's public OTLP intake endpoint (`otlp.datadoghq.com`) — no Extension, no collector, no tracing layer at all. |
| 4 | `otelCollectorSandbox` (`handler4.py`) — **opt-in, commented out by default** | OTel SDK exports to a sidecar OTel Collector running as a Lambda extension (custom build with the Datadog exporter — see [`otel-collector-dd/README.md`](otel-collector-dd/README.md)), instead of the Datadog Extension. |
| 5 | `otelAdotSandbox` (`handler5.py`) | AWS Distro for OpenTelemetry (ADOT) Lambda layer instead of the Datadog Extension — traces via ADOT's auto-instrumentation, metrics/logs manually wired through its bundled collector. |

All five functions deploy from one `serverless.yml`, as **one
CloudFormation stack — all or nothing**. Function 4 (`otelCollectorSandbox`)
is commented out by default in `serverless.yml` for exactly this reason:
it needs a `LayerVersionArn` that doesn't exist until you build and
publish it yourself, and leaving a placeholder/nonexistent ARN there
would fail the whole stack's deploy (a CloudFormation error on that one
resource, rolling back all five functions), not just function 4. See
"Build" and "Deploy" below for the default (functions 1, 2, 3, 5) and
the opt-in (adding function 4 back).

Functions 1-2 send to the Datadog Extension; functions 3-5 never attach
it at all, so the `DD_ENV`→`env`-tag translation (which happens inside
the Extension) doesn't apply to them — each sets the equivalent OTel
resource attribute directly instead.

## Telemetry signal support

| Function | Traces | OTel Logs | OTel Metrics | Custom Metrics (DogStatsD) |
|---|---|---|---|---|
| `otelSandbox` (Mode 1) | ✅ Works | ❌ `404` from the Extension | ❌ `404` from the Extension | ✅ Works |
| `ddtraceOtelApi` (Mode 2) | ✅ Works | N/A — no OTel SDK pipeline in this mode | N/A — no OTel SDK pipeline in this mode | ✅ Works |
| `otelDirectSandbox` | ✅ Works | Not independently verified | ✅ Works (needs delta temporality + a `dd-otel-metric-config` header for tags — both set in `handler3.py`) | Not applicable — no DogStatsD/Extension in this mode |
| `otelCollectorSandbox` — **opt-in, commented out by default** | ✅ Works | Not independently verified | ✅ Works, but its `env` tag is specifically missing (the collector's Datadog exporter handles trace/log tagging but not this one) | Not applicable |
| `otelAdotSandbox` | ✅ Works (2 spans: root + a child for the `STS.GetCallerIdentity` call, once `aiobotocore`'s instrumentor is excluded — see `serverless.yml`) | Not independently verified | Needs the same delta-temporality override as `otelDirectSandbox`; tag population not independently verified | Not applicable |

The Datadog Lambda Extension's OTLP receiver only implements the traces
route — OTel metrics and OTel logs both get a `404` from it, by design,
not a bug in this sandbox. Functions 3-5 don't go through the Extension
at all, so that specific limitation doesn't apply to them; each has its
own signal-support notes above instead.

## Makefile shortcut

`make build`/`deploy`/`invoke`/`verify`/`destroy` wrap the commands
below. `build`/`deploy`/`invoke` cover function 1 (`otelSandbox`)
specifically; `verify` checks both `otelSandbox` and `ddtraceOtelApi`
(Mode 1 and Mode 2) via `../scripts/verify.sh`, which queries the
Datadog Spans Search API. **Read that script's header before trusting
a failure from `make verify`**: without a 100% retention filter
configured for these services, a "not found" result is the *expected*
outcome most of the time under this org's Intelligent Retention
sampling, not evidence anything is broken. **Both the Makefile and that
script are untested**, written defensively but never run end-to-end in
this environment (no live credentials available). The manual commands
below remain the source of truth; the Makefile is a shortcut once
you've confirmed they work for your setup.

## Prerequisites

- AWS CLI configured with credentials for the target account/region
  (`us-east-1` by default — see "Region and Datadog site" below to
  target a different one).
- Python 3.11 with `pip`.
- Node.js + npm (for the Serverless Framework CLI).
- `DD_API_KEY` set in your shell environment — read via `${env:DD_API_KEY}`
  in `serverless.yml`, never hardcoded or committed.
- `DD_APP_KEY` set in your shell environment — only needed for
  `make verify`/`../scripts/verify.sh` (Datadog Application Key, read
  access to the Spans Search API). Not needed for build/deploy/invoke.
- **Serverless Framework v4 requires authentication** — `npx serverless
  login` (opens a browser) or a `SERVERLESS_ACCESS_KEY` env var, even
  though this stack only deploys to your own AWS account with no
  Serverless-hosted features. Free for individual use; organization/team
  use is subject to Serverless's own licensing terms — see
  [serverless.com/pricing](https://www.serverless.com/pricing). Do this
  once before your first deploy, or it fails as an opaque first-run error.
- No Docker required — dependencies are vendored directly with
  `pip --platform manylinux2014_x86_64` (see `BUILD_WALKTHROUGH.md` for
  why this matters on macOS).
- **Not needed for the default deploy** (functions 1, 2, 3, 5): a Go
  toolchain. Go is only needed if you're also building and publishing
  function 4's custom collector layer — see "Opting in to function 4"
  below and `otel-collector-dd/README.md`. Having *some* Go 1.21+
  installed is enough — it fetches the exact toolchain that module
  needs automatically, no manual version matching required.

## Build

```bash
cd python
npm install          # installs the Serverless Framework CLI (devDependency)
./build.sh            # vendors Function 1's deps into ./vendor
./build-ddtrace.sh    # vendors Function 2's deps into ./vendor-ddtrace
```

Functions 3 and 5 reuse the same `./vendor` directory `build.sh`
produces — no separate step needed. This builds everything needed for
the default deploy (functions 1, 2, 3, 5); function 4 needs an
additional build step, see below.

### Opting in to function 4 (`otelCollectorSandbox`)

Function 4 is commented out in `serverless.yml` by default (see "All
five functions deploy... as one CloudFormation stack" above for why).
To include it:

1. Build and publish its custom collector layer — see
   [`otel-collector-dd/README.md`](otel-collector-dd/README.md). This is
   the one step in this whole README that needs a Go toolchain.
2. Uncomment the `otelCollectorSandbox` block in `serverless.yml`.
3. Replace its placeholder `LayerVersionArn` with the one you just
   published.
4. Deploy as normal (below) — function 4 now deploys alongside the rest.

## Deploy

```bash
export DD_API_KEY=...   # if not already set
npx serverless deploy
```

Deploys one stack (`serverless-otel-aws-sandbox-dev`) with functions 1,
2, 3, and 5 by default (function 4 is opt-in — see "Opting in to
function 4" above). All functions in the stack deploy together; if
function 4 is uncommented with a layer ARN that doesn't resolve, the
whole stack's deploy fails with a CloudFormation error on that one
resource, potentially rolling back all five functions, not just that one.

By default, `otelSandbox` exercises all 4 telemetry signals, including
the two that don't work (OTel metrics, OTel logs — see the table above).
For a clean, error-free run instead:

```bash
TEST_UNSUPPORTED_SIGNALS=false npx serverless deploy
```

### Region and Datadog site

`serverless.yml`'s region and `DD_SITE` are both overridable via
environment variable, defaulting to this repo's originally-verified
values (`us-east-1` / `datadoghq.com`). The region override is
`SANDBOX_AWS_REGION`, not the more commonly-exported `AWS_REGION` —
deliberately, so this doesn't silently pick up an ambient `AWS_REGION`
set in your shell/CI for unrelated reasons and deploy somewhere the
pinned layer versions below were never verified:

```bash
SANDBOX_AWS_REGION=eu-west-1 DD_SITE=datadoghq.eu npx serverless deploy
```

The layer ARNs' region segment and the Datadog-Extension/tracer layer
*versions* also pull from env vars (`DD_EXTENSION_VERSION`,
`DD_PYTHON_LAYER_VERSION`). **`DD_EXTENSION_VERSION` defaults to `101`
here, matching the other three language stacks for cross-language
consistency — but this function's own signal matrix above was verified
on `100`, not `101`.** The pin moved; the verification hasn't yet
followed it. Until someone deploys this stack on `101` and confirms the
same results hold, treat the matrix above as applying to `100`, and see
the root README's "Layer versions" section for the full explanation.
`DD_PYTHON_LAYER_VERSION` (the `Datadog-Python311` tracer layer used by
function 2) is unaffected by this — it's a separate artifact, still
pinned to what was actually verified.

**The ADOT layer (function 5) is a separate case**: AWS does not publish
`aws-otel-python-amd64-ver-1-32-0`'s version numbers consistently across
regions the way Datadog does its own layers. Version `7` is only
confirmed for `us-east-1`. Deploying function 5 to another region needs
its own lookup first:

```bash
aws lambda list-layer-versions --layer-name aws-otel-python-amd64-ver-1-32-0 \
  --region <region> --query 'LayerVersions[].Version'
```

then pass the result via `DD_ADOT_LAYER_VERSION`. This hasn't been
confirmed against any region other than `us-east-1` — the command above
is reasoned from AWS's general layer-publishing behavior, not verified
here.

## Invoke

```bash
npx serverless invoke -f otelSandbox -d '{"source":"manual-test"}' --log
npx serverless invoke -f ddtraceOtelApi -d '{"source":"manual-test"}' --log
npx serverless invoke -f otelDirectSandbox -d '{"source":"manual-test"}' --log
npx serverless invoke -f otelAdotSandbox -d '{"source":"manual-test"}' --log
# Only if you opted into function 4 above:
npx serverless invoke -f otelCollectorSandbox -d '{"source":"manual-test"}' --log
```

The Serverless CLI's `--log` output has shown `undefined` message bodies
in testing — if that happens, pull the raw CloudWatch log stream
directly instead:

```bash
aws logs describe-log-streams --log-group-name /aws/lambda/<function-name> \
  --order-by LastEventTime --descending --limit 1 \
  --query 'logStreams[0].logStreamName' --output text
aws logs get-log-events --log-group-name /aws/lambda/<function-name> \
  --log-stream-name <stream-from-above>
```

## What to look for

- **Extension startup lines** (functions 1-2 only): `Starting Datadog
  Extension`, `OTLP HTTP | Starting collector on 127.0.0.1:4318`.
- **A trace-sent confirmation** (functions 1-2 only): `TRACES |
  Successfully sent trace`. This only appears with debug-level Extension
  logging — set `DD_LOG_LEVEL: debug` temporarily in `serverless.yml`'s
  function environment, redeploy, invoke, then remove it again (it's
  noisy/costly to leave on).
- **Function 1** (`otelSandbox`): `logger.info()` output carries no trace
  context — correlation with a trace only happens at the UI level, via
  `request_id`.
- **Function 2** (`ddtraceOtelApi`): `dd.trace_id`/`dd.span_id` are
  injected directly into the log text automatically, no extra config
  needed.
- **Functions 3-5**: none of them go through the Extension, so there's
  no local "successfully sent" confirmation to check — the only way to
  confirm data actually landed is the Datadog UI/API directly. A clean
  `200` response and no local errors are necessary but not sufficient
  evidence on their own.
- **Function 4's collector**: its own debug output (visible in
  CloudWatch when `DD_LOG_LEVEL=debug`) shows the Datadog exporter's
  internal Agent components starting up and processing received spans —
  useful for confirming data reached the collector even before checking
  whether it reached Datadog.

## Teardown

```bash
npx serverless remove
```

Removes the whole deployed stack (functions 1, 2, 3, 5 by default; also
function 4 if you opted in). If `serverless remove` stalls after
"Removing objects from S3 bucket" without actually deleting the
CloudFormation stack, fall back to deleting it directly:

```bash
aws cloudformation delete-stack --stack-name serverless-otel-aws-sandbox-dev
aws cloudformation wait stack-delete-complete --stack-name serverless-otel-aws-sandbox-dev
```

Function 4's custom collector layer (`otelcol-dd-sandbox`) is a separate
resource, not part of this stack — delete it independently if you
published one:

```bash
aws lambda list-layer-versions --layer-name otelcol-dd-sandbox --region us-east-1
aws lambda delete-layer-version --layer-name otelcol-dd-sandbox --version-number <N> --region us-east-1
```

### Teardown verification

Serverless Framework generates a log-group resource per function as
part of this stack's own CloudFormation template, so `serverless remove`
should delete them along with everything else — unlike the SAM-based
.NET/Java stacks, which don't get a managed log group by default (see
those READMEs). The custom collector layer is the one resource this
command never touches either way (noted above). Confirm nothing survived:

```bash
aws logs describe-log-groups --region us-east-1 \
  --query "logGroups[?contains(logGroupName,'serverless-otel-aws-sandbox')].logGroupName" --output table
aws lambda list-layer-versions --layer-name otelcol-dd-sandbox --region us-east-1 \
  --query 'LayerVersions[*].Version' --output text
```

Both commands should return nothing once torn down. **Not verified
against a live stack** — treat this as a starting point, not a
guarantee, and flag it if a log group does show up here despite the
reasoning above.
