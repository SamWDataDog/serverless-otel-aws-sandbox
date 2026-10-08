# Node.js sandbox

How to deploy and run this language's two functions. For the full
investigation, evidence, and how each of these conclusions was reached,
see the root [`FINDINGS.md`](../FINDINGS.md).

| | Function | Mode |
|---|---|---|
| **1** | `otelSandboxNode` (`src/handler.js` + `src/instrument.js`) | OTel SDK `NodeTracerProvider`, exporting via OTLP to the Extension. No Datadog tracing layer. |
| **2** | `ddtraceOtelApiNode` (`src/handler2.js`) | Native `dd-trace-js` + OTel API bridge (`DD_TRACE_OTEL_ENABLED=true`), via the Datadog Node tracing layer. |

Both functions deploy from one `serverless.yml`, as one stack, own
`package.json` isolated from the Python tooling.

## Telemetry signal support

| Function | Traces | OTel Logs | OTel Metrics | Custom Metrics (DogStatsD) |
|---|---|---|---|---|
| `otelSandboxNode` (Mode 1) | ✅ Works | ❌ `404` from the Extension | ❌ `404` from the Extension | ✅ Works |
| `ddtraceOtelApiNode` (Mode 2) | ✅ Works | N/A — no OTel SDK pipeline in this mode | N/A — no OTel SDK pipeline in this mode | ✅ Works |

The Datadog Lambda Extension's OTLP receiver only implements the traces
route — OTel metrics and OTel logs both get a `404`, by design, not a bug
in this sandbox.

## Makefile shortcut

`make build`/`deploy`/`invoke`/`verify`/`destroy` wrap the commands
below. `build`/`deploy`/`invoke` cover `otelSandboxNode` specifically;
`verify` checks both `otelSandboxNode` and `ddtraceOtelApiNode` (Mode 1
and Mode 2) via `../scripts/verify.sh`, which queries the Datadog Spans
Search API. **Read that script's header before trusting a failure from
`make verify`**: without a 100% retention filter configured for these
services, a "not found" result is the *expected* outcome most of the
time under this org's Intelligent Retention sampling, not evidence
anything is broken. **Both the Makefile and that script are untested**,
written defensively but never run end-to-end in this environment (no
live credentials available). The manual commands below remain the
source of truth; the Makefile is a shortcut once you've confirmed they
work for your setup.

## Prerequisites

- AWS CLI configured with credentials for the target account/region
  (`us-east-1` by default — see "Region and Datadog site" below to
  target a different one).
- Node.js (runtime target is `nodejs24.x`) + npm.
- `DD_API_KEY` set in your shell environment.
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

## Build

```bash
cd node
npm install   # installs deps for both functions, and the Serverless Framework CLI
```

## Deploy

```bash
export DD_API_KEY=...   # if not already set
npx serverless deploy
```

Deploys one stack (`serverless-otel-aws-sandbox-node-dev`) with both
functions.

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
`DD_NODE_LAYER_VERSION`), defaulting to what this repo actually
verified — see the root README's "Layer versions" section before
assuming a different version will behave the same way.

## Invoke

```bash
npx serverless invoke -f otelSandboxNode -d '{"source":"manual-test"}'
npx serverless invoke -f ddtraceOtelApiNode -d '{"source":"manual-test"}'
```

If the CLI's `--log`/invoke output doesn't show what you need, pull the
raw CloudWatch log stream directly:

```bash
aws logs describe-log-streams --log-group-name /aws/lambda/<function-name> \
  --order-by LastEventTime --descending --limit 1 \
  --query 'logStreams[0].logStreamName' --output text
aws logs get-log-events --log-group-name /aws/lambda/<function-name> \
  --log-stream-name <stream-from-above>
```

## What to look for

- **Extension startup lines**: `Starting Datadog Extension`, `OTLP HTTP |
  Starting collector on 127.0.0.1:4318`.
- **A trace-sent confirmation**: `TRACES | Successfully sent trace`. Set
  `DD_LOG_LEVEL: debug` temporarily in `serverless.yml` for this, remove
  it again afterward.
- **Function 1** (`otelSandboxNode`): for the actual exported span
  structure, not just whether it sent, set `DD_TRACE_DEBUG=true` and look
  for the `"Encoding payload"` debug line — it shows the real,
  msgpack-bound span JSON with `trace_id`/`span_id`/`parent_id`.
- **Function 2** (`ddtraceOtelApiNode`): `dd.trace_id`/`dd.span_id`
  auto-inject into `console.log` output, no extra config needed.

## Teardown

```bash
npx serverless remove
```

Removes the whole stack (both functions) — `otelSandboxNode` and
`ddtraceOtelApiNode` only, no effect on any other language's stack. Any
ad-hoc Lambda function deployed manually outside this stack (not managed
by `serverless.yml`) won't be removed by this command — check
`aws lambda list-functions` separately for anything like that.

### Teardown verification

Serverless Framework generates a log-group resource per function as
part of this stack's own CloudFormation template, so `serverless remove`
should delete them along with everything else. Confirm nothing survived:

```bash
aws logs describe-log-groups --region us-east-1 \
  --query "logGroups[?contains(logGroupName,'serverless-otel-aws-sandbox-node')].logGroupName" --output table
```

Should return nothing once torn down. **Not verified against a live
stack** — treat this as a starting point, not a guarantee.
