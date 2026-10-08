import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import boto3
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

# "Collector + Datadog exporter" mode: exports to a sidecar OTel
# Collector extension (localhost:4318) with the datadogexporter compiled
# in (see otel-collector-dd/README.md) -- not the Datadog Extension.

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# No Extension here, so DD_ENV never becomes `env` -- set the OTel
# resource attribute directly. Doesn't reach the metric signal
# specifically (traces/logs tag correctly); see FINDINGS.md "Round 7".
resource = Resource.create(
    {
        SERVICE_NAME: "otel-collector-sandbox",
        "deployment.environment": "sandbox",
        "deployment.environment.name": "sandbox",
    }
)

# --- Signal 1: traces ----------------------------------------------------
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces"))
)
trace.set_tracer_provider(tracer_provider)
BotocoreInstrumentor().instrument(tracer_provider=tracer_provider)
tracer = trace.get_tracer(__name__)

# --- Signal 2: OTel metrics ----------------------------------------------
metric_exporter = OTLPMetricExporter(endpoint="http://localhost:4318/v1/metrics")
metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=1000)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
otel_counter = meter_provider.get_meter(__name__).create_counter(
    "otel_collector_sandbox.otel_metric_test",
    description="Test counter sent via OTel Metrics SDK -> sidecar Collector -> Datadog exporter",
)

# --- Signal 3: OTel logs ---------------------------------------------------
log_exporter = OTLPLogExporter(endpoint="http://localhost:4318/v1/logs")
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
otel_logs_test_logger = logging.getLogger("otel-collector-logs-test")
otel_logs_test_logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
otel_logs_test_logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    logger.info("otel-collector-sandbox: handler invoked, event=%s", event)

    with tracer.start_as_current_span("do-work") as span:
        span.set_attribute("sandbox.event_keys", list(event.keys()) if isinstance(event, dict) else "n/a")

        sts = boto3.client("sts")
        identity = sts.get_caller_identity()

        logger.info(
            "otel-collector-sandbox: sts.get_caller_identity account=%s arn=%s",
            identity.get("Account"),
            identity.get("Arn"),
        )

        otel_counter.add(1, {"source": "otel-metrics-test"})
        otel_logs_test_logger.info("otel-collector-sandbox: OTel Logs SDK signal test")

    # Force-flush our own SDK providers so the data leaves the OTel SDK and
    # reaches the sidecar collector's OTLP receiver before Lambda can
    # freeze -- the collector then owns its own internal batching/export
    # timing to Datadog separately (its extension wrapper gives it a
    # couple of seconds on SHUTDOWN to flush that side too).
    tracer_provider.force_flush()
    meter_provider.force_flush()
    logger_provider.force_flush()

    return {
        "statusCode": 200,
        "body": "otel-collector-sandbox: OK",
    }


AwsLambdaInstrumentor().instrument(tracer_provider=tracer_provider)
