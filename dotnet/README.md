# .NET sandbox

How to deploy and run this language's two functions. For the full
investigation, evidence, and how each of these conclusions was reached,
see the root [`FINDINGS.md`](../FINDINGS.md).

| | Function | Mode |
|---|---|---|
| **1** | `OtelSandboxDotnetFunction` (`otel-aws-sandbox-dotnet`) | OTel SDK via the Extension's OTLP receiver. Ported, with attribution, from [Siddhitha Bhoopathy's sample](https://github.com/SiddhithaBhoopathy/otel-dotnet-lambda-extension-samples). No Datadog tracing layer. |
| **2** | `DdtraceOtelApiDotnetFunction` (`ddtrace-otel-api-sandbox-dotnet`) | Native `dd-trace-dotnet` + `System.Diagnostics.Activity`/`ActivitySource` bridge (`DD_TRACE_OTEL_ENABLED=true`), via the `dd-trace-dotnet` layer. |

**Warning: Mode 2 does not currently produce any verifiable traces.**
Zero spans are ever ingested, confirmed against both cold-start and
warm-invocation test requests directly in the Datadog UI/API. A clean
deploy and invoke — including the Extension logging "successfully sent
trace" — is not evidence this is working. See `FINDINGS.md` for the full
investigation if you want the detail.

Both functions deploy from one `template.yaml` (AWS SAM, not Serverless
Framework), as one stack.

## Telemetry signal support

| Function | Traces | OTel Logs | OTel Metrics | Custom Metrics (DogStatsD) |
|---|---|---|---|---|
| `OtelSandboxDotnetFunction` (Mode 1) | ✅ Works | Not separately tested | ❌ `404` from the Extension | ✅ Works |
| `DdtraceOtelApiDotnetFunction` (Mode 2) | ❌ Zero spans ever ingested (see warning above) | N/A — no OTel SDK pipeline in this mode | N/A — no OTel SDK pipeline in this mode | Not separately tested |

The Datadog Lambda Extension's OTLP receiver only implements the traces
route — OTel metrics get a `404`, by design, not a bug in this sandbox.

## Makefile shortcut

`make build`/`deploy`/`invoke`/`verify`/`destroy` wrap the commands
below. `build`/`deploy`/`invoke` cover `OtelSandboxDotnetFunction`
specifically; `verify` checks both `otel-aws-sandbox-dotnet` and
`ddtrace-otel-api-sandbox-dotnet` (Mode 1 and Mode 2) via
`../scripts/verify.sh`, which queries the Datadog Spans Search API. The
Mode 2 check is **expected to fail** — confirmed zero spans ever
ingested, see `FINDINGS.md` — that's the documented correct outcome for
this function, not evidence of a problem with the check itself. **Read
that script's header before trusting the Mode 1 result too**: without a
100% retention filter configured for these services, a "not found"
result there is the *expected* outcome most of the time under this
org's Intelligent Retention sampling. **Both the Makefile and that
script are untested**, written defensively but never run end-to-end in
this environment (no live credentials available). The manual commands
below remain the source of truth; the Makefile is a shortcut once
you've confirmed they work for your setup.

## Prerequisites

- AWS CLI configured with credentials for the target account/region —
  `template.yaml`'s layer ARNs derive region automatically
  (`${AWS::Region}`) from wherever you deploy, so no hardcoded region to
  edit.
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).
- .NET SDK (target runtime is `dotnet8`) with the `amazon.lambda.tools`
  global tool:
  ```bash
  dotnet tool install -g Amazon.Lambda.Tools
  export PATH="$PATH:$HOME/.dotnet/tools"
  ```
- `DD_API_KEY` set in your shell environment (passed as a SAM parameter
  at deploy time — never hardcoded or committed).
- `DD_APP_KEY` set in your shell environment — only needed for
  `make verify`/`../scripts/verify.sh` (Datadog Application Key, read
  access to the Spans Search API). Not needed for build/deploy/invoke.

## Before your first deploy on this template

`template.yaml` declares an explicit `AWS::Logs::LogGroup` per function
(7-day retention — see "Teardown verification" below for why). If
either function was **ever deployed before this template added those
resources**, Lambda already auto-created an unmanaged log group with
the same name, and CloudFormation's `CREATE` for the new, now-tracked
`LogGroup` resource will fail with "already exists" on your next
`sam deploy`.

This is exactly the scenario those resources exist to prevent going
forward — it's a one-time migration cost, not a recurring one. Delete
the pre-existing groups once, before deploying this template version
for the first time:

```bash
aws logs delete-log-group --log-group-name /aws/lambda/otel-aws-sandbox-dotnet --region us-east-1
aws logs delete-log-group --log-group-name /aws/lambda/ddtrace-otel-api-sandbox-dotnet --region us-east-1
```

Safe to run even if a group doesn't exist — `ResourceNotFoundException`
is a successful no-op here, not an error to work around. If you're
deploying this stack for the first time ever (the common case for a
cold clone of this repo), skip this section entirely; there's nothing
to delete.

This is a SAM/CloudFormation-specific problem. Python and Node use
Serverless Framework's `provider.logRetentionInDays` instead, which
doesn't have this collision — don't apply this pre-delete step there.

## Build

```bash
cd dotnet
sam build
```

## Deploy

```bash
export DD_API_KEY=...   # if not already set
sam deploy --stack-name serverless-otel-aws-sandbox-dotnet \
  --parameter-overrides "DatadogApiKey=$DD_API_KEY" \
  --capabilities CAPABILITY_IAM --resolve-s3
```

Deploys one stack with both functions.

### Region and Datadog site

Region is derived automatically from wherever you deploy (`${AWS::Region}`
in `template.yaml`'s layer ARNs — no hand-editing needed to target a
different region). Datadog site is a SAM parameter, defaulting to
`datadoghq.com`:

```bash
sam deploy --stack-name serverless-otel-aws-sandbox-dotnet \
  --parameter-overrides "DatadogApiKey=$DD_API_KEY" "DatadogSite=datadoghq.eu" \
  --capabilities CAPABILITY_IAM --resolve-s3
```

`ExtensionVersion`/`DotnetTracerVersion` are also SAM parameters — see
the root README's "Layer versions" section before overriding either.

## Invoke

```bash
aws lambda invoke --function-name otel-aws-sandbox-dotnet \
  --cli-binary-format raw-in-base64-out --payload file://events/event.json /tmp/out.json
aws lambda invoke --function-name ddtrace-otel-api-sandbox-dotnet \
  --cli-binary-format raw-in-base64-out --payload '{"source":"manual-test"}' /tmp/out2.json
```

For raw logs:

```bash
aws logs describe-log-streams --log-group-name /aws/lambda/<function-name> \
  --order-by LastEventTime --descending --limit 1 \
  --query 'logStreams[0].logStreamName' --output text
aws logs get-log-events --log-group-name /aws/lambda/<function-name> \
  --log-stream-name <stream-from-above>
```

## What to look for

Set `DD_LOG_LEVEL=debug` and, for Mode 2, `DD_TRACE_DEBUG=true` via
`aws lambda update-function-configuration` before invoking, then remove
them again afterward.

- **Extension startup lines**: `Starting Datadog Extension`, `OTLP HTTP |
  Starting collector on 127.0.0.1:4318`.
- **A trace-sent confirmation**: `TRACES | Successfully sent trace`. For
  Mode 2 specifically, **this line alone is not proof a real span
  existed** — no equivalent tracer-internal span-construction output
  exists in dd-trace-dotnet's debug logging to confirm it independently.
  See `FINDINGS.md` for the full investigation.
- **Function 1**: `JsonConsoleLogExporter` injects `dd.trace_id`/
  `dd.span_id` directly into its JSON log output.
- **Function 2**: plain `Console.WriteLine` carries no `dd.trace_id`
  prefix — unlike Python/Node, this needs an explicit logging-framework
  sink, not just console output.

## Teardown

```bash
sam delete --stack-name serverless-otel-aws-sandbox-dotnet --no-prompts
```

If you're not sure of the exact deployed stack name, list it first:

```bash
aws cloudformation list-stacks \
  --query "StackSummaries[?contains(StackName, 'dotnet')].{Name:StackName,Status:StackStatus}" \
  --output table
```

### Teardown verification

`template.yaml` now declares an explicit `AWS::Logs::LogGroup` per
function (7-day retention) specifically so `sam delete` cleans them up
as tracked stack resources — `AWS::Serverless::Function` has no
retention/ownership property of its own, and Lambda's own auto-created
log group (if one exists from before this template change, or from any
deploy that predates it) is never removed by `sam delete`. Confirm
nothing survived:

```bash
aws logs describe-log-groups --region us-east-1 \
  --query "logGroups[?contains(logGroupName,'otel-aws-sandbox-dotnet') || contains(logGroupName,'ddtrace-otel-api-sandbox-dotnet')].logGroupName" --output table
```

Should return nothing once torn down. **Not verified against a live
stack** — in particular, whether the explicit `LogGroup` resource
actually gets deleted on `sam delete` the same way the function does.
