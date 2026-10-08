'use strict';

const dgram = require('dgram');
const { trace } = require('@opentelemetry/api');
const logsAPI = require('@opentelemetry/api-logs');
const { STSClient, GetCallerIdentityCommand } = require('@aws-sdk/client-sts');

const { MeterProvider, PeriodicExportingMetricReader } = require('@opentelemetry/sdk-metrics');
const { OTLPMetricExporter } = require('@opentelemetry/exporter-metrics-otlp-http');
const { LoggerProvider, SimpleLogRecordProcessor } = require('@opentelemetry/sdk-logs');
const { OTLPLogExporter } = require('@opentelemetry/exporter-logs-otlp-http');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { SemanticResourceAttributes } = require('@opentelemetry/semantic-conventions');

// Same gate as handler.py: Signals 2/3 (OTel metrics, OTel logs) are
// confirmed 404 on every invocation (see FINDINGS.md "All 4 forms of
// telemetry" / "Round 4"). Default on so this keeps demonstrating the
// finding; set to "false" for a clean run.
const TEST_UNSUPPORTED_SIGNALS = (process.env.TEST_UNSUPPORTED_SIGNALS ?? 'true').toLowerCase() === 'true';

const resource = resourceFromAttributes({
  [SemanticResourceAttributes.SERVICE_NAME]: 'otel-aws-sandbox-node',
});

const tracer = trace.getTracer('otel-aws-sandbox-node');

// --- Signal 2: OTel metrics (via the same OTLP HTTP receiver) ----------
let meterProvider = null;
let otelCounter = null;
if (TEST_UNSUPPORTED_SIGNALS) {
  meterProvider = new MeterProvider({
    resource,
    readers: [
      new PeriodicExportingMetricReader({
        exporter: new OTLPMetricExporter({ url: 'http://localhost:4318/v1/metrics' }),
        exportIntervalMillis: 1000,
      }),
    ],
  });
  otelCounter = meterProvider.getMeter('otel-aws-sandbox-node').createCounter(
    'otel_aws_sandbox_node.otel_metric_test',
    { description: 'Test counter sent via OTel Metrics SDK -> OTLP -> Extension' },
  );
}

// --- Signal 3: OTel logs (via the same OTLP HTTP receiver) -------------
let loggerProvider = null;
let otelLogger = null;
if (TEST_UNSUPPORTED_SIGNALS) {
  loggerProvider = new LoggerProvider({
    resource,
    processors: [
      // SimpleLogRecordProcessor takes an options object ({ exporter }),
      // not the exporter directly as a positional arg -- found the hard
      // way (TypeError deep in @opentelemetry/core's internal _export
      // helper). See FINDINGS.md "Round 4".
      new SimpleLogRecordProcessor({ exporter: new OTLPLogExporter({ url: 'http://localhost:4318/v1/logs' }) }),
    ],
  });
  otelLogger = loggerProvider.getLogger('otel-logs-test');
}

// --- Signal 4: Datadog custom metrics via raw DogStatsD (UDP 8125) -----
// Same reasoning as Python: avoid datadog-lambda-js for this if it has the
// same ddtrace-import side effect (see FINDINGS.md "Round 4" for whether
// it actually does).
function sendCustomMetric(name, value, tags) {
  const tagStr = tags && tags.length ? `|#${tags.join(',')}` : '';
  const packet = Buffer.from(`${name}:${value}|d${tagStr}`);
  const sock = dgram.createSocket('udp4');
  sock.send(packet, 8125, '127.0.0.1', () => sock.close());
}

exports.handler = async (event, lambdaContext) => {
  console.log('otel-aws-sandbox-node: handler invoked, event=', JSON.stringify(event));

  // startActiveSpan, not startSpan + manual setSpan/context.with: converged
  // on this pattern for consistency with handler2.js, where the manual
  // pattern was initially flagged as producing a disconnected trace_id --
  // later corrected to a console-log display artifact for THAT file's
  // pattern, not a structural bug in either pattern (see FINDINGS.md
  // "Round 4"). That correction wasn't independently re-run against this
  // file's own span tree; this is a readability convergence, not a claim
  // that the manual pattern here was ever broken.
  return tracer.startActiveSpan('do-work', async (span) => {
    const sts = new STSClient({});
    const identity = await sts.send(new GetCallerIdentityCommand({}));

    console.log(
      `otel-aws-sandbox-node: sts.getCallerIdentity account=${identity.Account} arn=${identity.Arn}`,
    );

    if (TEST_UNSUPPORTED_SIGNALS) {
      otelCounter.add(1, { source: 'otel-metrics-test' });
      otelLogger.emit({
        body: 'otel-aws-sandbox-node: OTel Logs SDK signal test',
        severityNumber: logsAPI.SeverityNumber.INFO,
      });
    }

    sendCustomMetric('otel_aws_sandbox_node.custom_metric_test', 1, ['source:custom-metrics-test']);

    span.end();

    if (TEST_UNSUPPORTED_SIGNALS) {
      await meterProvider.forceFlush();
      await loggerProvider.forceFlush();
    }

    return { statusCode: 200, body: 'otel-aws-sandbox-node: OK' };
  });
};
