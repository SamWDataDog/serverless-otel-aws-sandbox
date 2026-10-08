import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import boto3
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import Counter, MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource

# ADOT Lambda layer mode. Traces are auto-instrumented (see serverless.yml
# for the OTEL_PYTHON_DISABLED_INSTRUMENTATIONS workaround that requires).
# Metrics/logs have no auto-instrumentation equivalent, so they're wired
# up manually against ADOT's bundled collector, relayed via
# adot-collector.yaml since that collector has no Datadog exporter.

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Resource.create() auto-merges OTEL_RESOURCE_ATTRIBUTES (set in
# serverless.yml), the same mechanism ADOT's auto-instrumented Resource
# uses -- so the `env` tag lands on traces with no extra code here.
resource = Resource.create({SERVICE_NAME: "otel-adot-sandbox"})

# Datadog's OTLP metrics intake requires delta temporality for
# Counter/Sum metrics; the SDK's PeriodicExportingMetricReader defaults
# to cumulative, so it's overridden explicitly below.
metric_exporter = OTLPMetricExporter(
    endpoint="http://localhost:4318/v1/metrics",
    preferred_temporality={Counter: AggregationTemporality.DELTA},
)
metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=1000)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
otel_counter = meter_provider.get_meter(__name__).create_counter(
    "otel_adot_sandbox.otel_metric_test",
    description="Test counter sent via OTel Metrics SDK -> ADOT's bundled collector -> otlphttp -> Datadog",
)

log_exporter = OTLPLogExporter(endpoint="http://localhost:4318/v1/logs")
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
otel_logs_test_logger = logging.getLogger("otel-adot-logs-test")
otel_logs_test_logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
otel_logs_test_logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    logger.info("otel-adot-sandbox: handler invoked, event=%s", event)

    sts = boto3.client("sts")
    identity = sts.get_caller_identity()

    logger.info(
        "otel-adot-sandbox: sts.get_caller_identity account=%s arn=%s",
        identity.get("Account"),
        identity.get("Arn"),
    )

    otel_counter.add(1, {"source": "otel-metrics-test"})
    otel_logs_test_logger.info("otel-adot-sandbox: OTel Logs SDK signal test")

    meter_provider.force_flush()
    logger_provider.force_flush()

    return {
        "statusCode": 200,
        "body": "otel-adot-sandbox: OK",
    }
