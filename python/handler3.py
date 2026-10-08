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
from opentelemetry.sdk.metrics import Counter, MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

# "OTel Direct" mode: the OTel SDK exports straight to Datadog's OTLP
# intake over the public internet, with no Extension/collector in the
# loop. Auth is a plain `dd-api-key` header; traces also need
# `compute_stats=true` for APM trace metrics to compute.

DD_API_KEY = os.environ.get("DD_API_KEY")
if not DD_API_KEY:
    raise RuntimeError("DD_API_KEY env var is not set -- see python/README.md Prerequisites")
DD_SITE = os.environ.get("DD_SITE", "datadoghq.com")
OTLP_BASE = f"https://otlp.{DD_SITE}/v1"

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# No Extension attached in this mode, so the DD_ENV env var never
# becomes an `env` tag -- that translation happens inside the Extension.
# Set the OTel resource attribute directly instead (both the legacy and
# current semconv keys, for safety).
resource = Resource.create(
    {
        SERVICE_NAME: "otel-direct-sandbox",
        "deployment.environment": "sandbox",
        "deployment.environment.name": "sandbox",
    }
)

# --- Signal 1: traces ----------------------------------------------------
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    SimpleSpanProcessor(
        OTLPSpanExporter(
            endpoint=f"{OTLP_BASE}/traces",
            headers={
                "dd-api-key": DD_API_KEY,
                "compute_stats": "true",
                "dd-otlp-source": "serverless",
            },
        )
    )
)
trace.set_tracer_provider(tracer_provider)
BotocoreInstrumentor().instrument(tracer_provider=tracer_provider)
tracer = trace.get_tracer(__name__)

# --- Signal 2: OTel metrics ----------------------------------------------
# Datadog's OTLP intake requires delta temporality for Counter/Sum
# metrics (SDK default is cumulative). Resource attributes also need the
# dd-otel-metric-config header to land as metric tags -- its value must
# be a JSON string, not a bare key=value pair (silently ignored otherwise).
metric_exporter = OTLPMetricExporter(
    endpoint=f"{OTLP_BASE}/metrics",
    headers={
        "dd-api-key": DD_API_KEY,
        "dd-otlp-source": "serverless",
        "dd-otel-metric-config": '{"resource_attributes_as_tags": true}',
    },
    preferred_temporality={Counter: AggregationTemporality.DELTA},
)
metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=1000)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
otel_counter = meter_provider.get_meter(__name__).create_counter(
    "otel_direct_sandbox.otel_metric_test",
    description="Test counter sent via OTel Metrics SDK straight to Datadog's OTLP intake",
)

# --- Signal 3: OTel logs ---------------------------------------------------
log_exporter = OTLPLogExporter(
    endpoint=f"{OTLP_BASE}/logs",
    headers={"dd-api-key": DD_API_KEY, "dd-otlp-source": "serverless"},
)
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
otel_logs_test_logger = logging.getLogger("otel-direct-logs-test")
otel_logs_test_logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
otel_logs_test_logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    logger.info("otel-direct-sandbox: handler invoked, event=%s", event)

    with tracer.start_as_current_span("do-work") as span:
        span.set_attribute("sandbox.event_keys", list(event.keys()) if isinstance(event, dict) else "n/a")

        sts = boto3.client("sts")
        identity = sts.get_caller_identity()

        logger.info(
            "otel-direct-sandbox: sts.get_caller_identity account=%s arn=%s",
            identity.get("Account"),
            identity.get("Arn"),
        )

        otel_counter.add(1, {"source": "otel-metrics-test"})
        otel_logs_test_logger.info("otel-direct-sandbox: OTel Logs SDK signal test")

    # No Extension/collector in this mode to buffer and flush on our
    # behalf -- force-flush everything ourselves before Lambda can freeze
    # the execution environment between invocations.
    tracer_provider.force_flush()
    meter_provider.force_flush()
    logger_provider.force_flush()

    return {
        "statusCode": 200,
        "body": "otel-direct-sandbox: OK",
    }


AwsLambdaInstrumentor().instrument(tracer_provider=tracer_provider)
