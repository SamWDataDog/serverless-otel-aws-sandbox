'use strict';

// Node Mode 1: mirrors Python's handler.py (OTel SDK through the
// Extension's local OTLP receiver, no dd-trace). Lives in its own file,
// force-loaded via `NODE_OPTIONS: --require instrument`: its require-hook
// must register before Node's loader loads handler.js -- the same
// ordering hazard Python hit with AwsLambdaInstrumentor. FINDINGS.md "Round 4".

const { diag, DiagConsoleLogger, DiagLogLevel } = require('@opentelemetry/api');
// Without this, OTel JS SDK export failures (e.g. the Extension's 404s on
// /v1/metrics and /v1/logs, same as Python) are swallowed silently --
// unlike Python, where the SDK logs errors to stderr by default. Node's
// SDK needs an explicit diag logger registered to see them at all.
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.ERROR);

const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { resourceFromAttributes, defaultResource } = require('@opentelemetry/resources');
const { SemanticResourceAttributes } = require('@opentelemetry/semantic-conventions');
const { SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { AwsInstrumentation } = require('@opentelemetry/instrumentation-aws-sdk');
const { AwsLambdaInstrumentation } = require('@opentelemetry/instrumentation-aws-lambda');
const { registerInstrumentations } = require('@opentelemetry/instrumentation');

// Passing a custom `resource` to NodeTracerProvider's constructor
// REPLACES the SDK's default resource entirely, unlike Python's SDK --
// without this explicit .merge(), telemetry.sdk.* never lands on any
// span. Confirmed by direct test. See FINDINGS.md "Round 4".
const resource = defaultResource().merge(
  resourceFromAttributes({
    [SemanticResourceAttributes.SERVICE_NAME]: 'otel-aws-sandbox-node',
  }),
);

const provider = new NodeTracerProvider({
  resource,
  spanProcessors: [
    new SimpleSpanProcessor(
      new OTLPTraceExporter({ url: 'http://localhost:4318/v1/traces' }),
    ),
  ],
});
provider.register();

registerInstrumentations({
  instrumentations: [
    new AwsInstrumentation({ suppressInternalInstrumentation: true }),
    new AwsLambdaInstrumentation({ disableAwsContextPropagation: true }),
  ],
});
