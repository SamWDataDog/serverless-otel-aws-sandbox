# Java sandbox

How to deploy and run this language's two functions. For the full
investigation, evidence, and how each of these conclusions was reached,
see the root [`FINDINGS.md`](../FINDINGS.md).

| | Function | Mode |
|---|---|---|
| **1** | `OtelSandboxJavaFunction` (`otel-aws-sandbox-java`) | OTel SDK via the Extension's OTLP receiver, using the official OpenTelemetry Java AWS Lambda instrumentation. No Datadog tracing layer. No official or community sample exists for this; the function is built directly against the OpenTelemetry Java libraries. |
| **2** | `DdtraceOtelApiJavaFunction` (`ddtrace-otel-api-sandbox-java`) | Native `dd-trace-java` + `GlobalOpenTelemetry` bridge (`DD_TRACE_OTEL_ENABLED=true`), via the `dd-trace-java` layer. |

Both functions deploy from one `template.yaml` (AWS SAM + Maven), as one
stack.

## Telemetry signal support

| Function | Traces | OTel Logs | OTel Metrics | Custom Metrics (DogStatsD) |
|---|---|---|---|---|
| `OtelSandboxJavaFunction` (Mode 1) | ✅ Works | Not separately tested | ❌ `404` from the Extension | ✅ Works |
| `DdtraceOtelApiJavaFunction` (Mode 2) | ✅ Works | N/A — no OTel SDK pipeline in this mode | N/A — no OTel SDK pipeline in this mode | ✅ Works |

The Datadog Lambda Extension's OTLP receiver only implements the traces
route — OTel metrics get a `404`, by design, not a bug in this sandbox.

## Makefile shortcut

`make build`/`deploy`/`invoke`/`verify`/`destroy` wrap the commands
below. `build`/`deploy`/`invoke` cover `OtelSandboxJavaFunction`
specifically; `verify` checks both `otel-aws-sandbox-java` and
`ddtrace-otel-api-sandbox-java` (Mode 1 and Mode 2) via
`../scripts/verify.sh`, which queries the Datadog Spans Search API.
**Read that script's header before trusting a failure from
`make verify`**: without a 100% retention filter configured for these
services, a "not found" result is the *expected* outcome most of the
time under this org's Intelligent Retention sampling, not evidence
anything is broken. **Both the Makefile and that script are untested**,
written defensively but never run end-to-end in this environment (no
live credentials available). The manual commands below remain the
source of truth; the Makefile is a shortcut once you've confirmed they
work for your setup.

## Prerequisites

- AWS CLI configured with credentials for the target account/region —
  `template.yaml`'s layer ARNs derive region automatically
  (`${AWS::Region}`) from wherever you deploy, so no hardcoded region to
  edit.
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).
- Maven (target runtime is `java21`):
  ```bash
  brew install maven
  export PATH="$PATH:$(brew --prefix maven)/bin"
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
aws logs delete-log-group --log-group-name /aws/lambda/otel-aws-sandbox-java --region us-east-1
aws logs delete-log-group --log-group-name /aws/lambda/ddtrace-otel-api-sandbox-java --region us-east-1
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
cd java
sam build
```

**Gotcha**: always run a plain `sam build` with no function name
argument. A scoped `sam build <FunctionName>` silently deletes the
*other* function's artifacts from `.aws-sam/build`, and the next `sam
deploy` will happily push the now-broken build with no error — it only
shows up as a `ClassNotFoundException` on the next invoke.

## Deploy

```bash
export DD_API_KEY=...   # if not already set
sam deploy --stack-name serverless-otel-aws-sandbox-java \
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
sam deploy --stack-name serverless-otel-aws-sandbox-java \
  --parameter-overrides "DatadogApiKey=$DD_API_KEY" "DatadogSite=datadoghq.eu" \
  --capabilities CAPABILITY_IAM --resolve-s3
```

`ExtensionVersion`/`JavaTracerVersion` are also SAM parameters — see
the root README's "Layer versions" section before overriding either.

## Invoke

```bash
aws lambda invoke --function-name otel-aws-sandbox-java \
  --cli-binary-format raw-in-base64-out --payload '{"source":"manual-test"}' /tmp/out.json
aws lambda invoke --function-name ddtrace-otel-api-sandbox-java \
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
- **A trace-sent confirmation**: `TRACES | Successfully sent trace`.
- **Function 2, the ground-truth check**: look for
  `datadog.trace.agent.core.DDSpan - Started span: DDSpan [ t_id=...,
  s_id=..., p_id=... ]` lines — dd-trace-java's own internal span
  construction log. A clean trace shows one root (`p_id=0`), `do-work`
  with `p_id` matching the root's `s_id`, and the STS child span with
  `p_id` matching `do-work`'s `s_id`. This is real proof a span was
  constructed and correctly nested, not just that the Extension accepted
  an HTTP POST.
- **Function 2 log correlation**: plain `System.out.println` carries no
  `dd.trace_id` prefix, despite the tracer's own startup banner claiming
  `logs_correlation_enabled: true` — needs an explicit logging-framework
  sink (SLF4J/Logback/Log4j2), not raw console output.

## Teardown

```bash
sam delete --stack-name serverless-otel-aws-sandbox-java --no-prompts
```

If you're not sure of the exact deployed stack name, list it first:

```bash
aws cloudformation list-stacks \
  --query "StackSummaries[?contains(StackName, 'java')].{Name:StackName,Status:StackStatus}" \
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
  --query "logGroups[?contains(logGroupName,'otel-aws-sandbox-java') || contains(logGroupName,'ddtrace-otel-api-sandbox-java')].logGroupName" --output table
```

Should return nothing once torn down. **Not verified against a live
stack** — in particular, whether the explicit `LogGroup` resource
actually gets deleted on `sam delete` the same way the function does.
