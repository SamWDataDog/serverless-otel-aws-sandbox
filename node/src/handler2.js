'use strict';

// Node equivalent of handler2.py: plain @opentelemetry/api calls, but the
// tracer behind them is dd-trace-js (DD_TRACE_OTEL_ENABLED=true in
// serverless.yml), not an OTel SDK TracerProvider set up here.

const { trace } = require('@opentelemetry/api');
const { STSClient, GetCallerIdentityCommand } = require('@aws-sdk/client-sts');

// Using datadog-lambda-js's sendDistributionMetric(), not raw UDP: this
// function's entry point is already datadog-lambda-js's handler wrapper,
// so there's no import-side-effect to avoid the way Python's
// datadog_lambda has -- see FINDINGS.md.
const { sendDistributionMetric } = require('datadog-lambda-js');

// DD_TRACE_OTEL_ENABLED alone doesn't register dd-trace's OTel bridge as
// the global TracerProvider (confirmed in dd-trace's proxy.js) -- without
// this explicit call, trace.getTracer() silently no-ops. See FINDINGS.md
// "Round 4".
const { TracerProvider } = require('dd-trace');
new TracerProvider().register();

const tracer = trace.getTracer('ddtrace-otel-api-sandbox-node');

exports.handler = async (event, context) => {
  console.log('ddtrace-otel-api-sandbox-node: handler invoked, event=', JSON.stringify(event));

  // startActiveSpan (not startSpan + manual setSpan/context.with) --
  // the manual version produced a span with its own fresh trace_id,
  // disconnected from dd-trace's own active "aws.lambda" root span,
  // instead of nesting as its child. See FINDINGS.md "Round 4".
  return tracer.startActiveSpan('do-work', async (span) => {
    const sts = new STSClient({});
    const identity = await sts.send(new GetCallerIdentityCommand({}));

    console.log(
      `ddtrace-otel-api-sandbox-node: sts.getCallerIdentity account=${identity.Account} arn=${identity.Arn}`,
    );

    sendDistributionMetric(
      'ddtrace_otel_api_sandbox_node.custom_metric_test',
      1,
      'source:custom-metrics-test',
    );

    span.end();
    return { statusCode: 200, body: 'ddtrace-otel-api-sandbox-node: OK' };
  });
};
