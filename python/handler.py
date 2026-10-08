import logging
import os
import socket
import sys

# Vendored dependencies (installed by build.sh) aren't on sys.path by
# default -- add ./vendor before importing anything that lives there. boto3
# comes from the Lambda runtime either way, but the opentelemetry packages
# below are vendored and need this first.
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

# OpenTelemetry SDK through the Datadog Lambda Extension's local OTLP
# receiver (http://localhost:4318) -- no Datadog tracing layer, no
# ddtrace. Contrast with handler2.py's native ddtrace + OTel API bridge.

# Lambda's runtime pre-attaches a handler to the root logger before this
# module runs, so logging.basicConfig() here is a no-op. Set the level
# directly instead, or INFO-level logger.info() calls silently vanish.
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Gate for the 2 telemetry signals confirmed NOT to work through the
# Extension (OTel metrics, OTel logs -- 404 on every invocation, see
# FINDINGS.md "All 4 forms of telemetry"). Defaults to on: this sandbox
# exists to demonstrate and keep reproducing that finding, not hide it.
# Set to "false" for a clean run with only the 2 working signals (traces,
# custom metrics).
TEST_UNSUPPORTED_SIGNALS = os.environ.get("TEST_UNSUPPORTED_SIGNALS", "true").lower() == "true"

# deployment.environment is deliberately NOT set here -- see
# FINDINGS.md "Round 3" for whether DD_ENV alone is enough for this mode.
resource = Resource.create({SERVICE_NAME: "otel-aws-sandbox"})

# --- Signal 1: traces --------------------------------------------------
# TracerProvider + OTLP HTTP exporter pointed at the Datadog Lambda
# Extension's local OTLP receiver (not the native ddtrace path).
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces"))
)
trace.set_tracer_provider(tracer_provider)

# Instrument boto3/botocore calls now (safe: no handler lookup involved).
BotocoreInstrumentor().instrument(tracer_provider=tracer_provider)

tracer = trace.get_tracer(__name__)

# --- Signal 2: OTel metrics (via the same OTLP HTTP receiver) ----------
# Datadog's docs state OTLP metrics ingestion is NOT supported by the
# Lambda Extension. Wired up anyway (gated behind TEST_UNSUPPORTED_SIGNALS)
# to keep demonstrating exactly what happens -- a 404, not a silent drop --
# rather than take that on faith.
if TEST_UNSUPPORTED_SIGNALS:
    metric_exporter = OTLPMetricExporter(endpoint="http://localhost:4318/v1/metrics")
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter, export_interval_millis=1000
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    otel_counter = meter_provider.get_meter(__name__).create_counter(
        "otel_aws_sandbox.otel_metric_test",
        description="Test counter sent via OTel Metrics SDK -> OTLP -> Extension",
    )
else:
    meter_provider = None
    otel_counter = None

# --- Signal 3: OTel logs (via the same OTLP HTTP receiver) -------------
# Separate from the plain logger.info() calls below, which go through the
# normal AWS Lambda -> CloudWatch path. This path instead uses the OTel
# Logs SDK's own exporter to send an OTLP log record directly. Same
# confirmed-404 status as Signal 2 above; same gate.
if TEST_UNSUPPORTED_SIGNALS:
    log_exporter = OTLPLogExporter(endpoint="http://localhost:4318/v1/logs")
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    otel_logs_test_logger = logging.getLogger("otel-logs-test")
    otel_logs_test_logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    otel_logs_test_logger.setLevel(logging.INFO)
else:
    logger_provider = None
    otel_logs_test_logger = None

# --- Signal 4: Datadog custom metrics via raw DogStatsD (UDP 8125) -----
# Not using `datadog_lambda`'s lambda_metric() here: importing it
# unconditionally imports ddtrace and patches botocore, contaminating
# this sandbox's OTel-only instrumentation. Raw DogStatsD packet instead.
def send_custom_metric(name, value, tags=None):
    tag_str = ("|#" + ",".join(tags)) if tags else ""
    packet = f"{name}:{value}|d{tag_str}".encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(packet, ("127.0.0.1", 8125))
    finally:
        sock.close()


def lambda_handler(event, context):
    logger.info("otel-aws-sandbox: handler invoked, event=%s", event)

    with tracer.start_as_current_span("do-work") as span:
        span.set_attribute("sandbox.event_keys", list(event.keys()) if isinstance(event, dict) else "n/a")
        # Trace-to-log tag propagation test: does an arbitrary span
        # attribute show up on the UI-correlated log too, not just
        # request_id-level correlation? See FINDINGS.md for the check.
        span.set_attribute("sandbox.canary_tag", "trace-log-propagation-test")

        sts = boto3.client("sts")
        identity = sts.get_caller_identity()

        logger.info(
            "otel-aws-sandbox: sts.get_caller_identity account=%s arn=%s",
            identity.get("Account"),
            identity.get("Arn"),
        )

        if TEST_UNSUPPORTED_SIGNALS:
            otel_counter.add(1, {"source": "otel-metrics-test"})
            otel_logs_test_logger.info("otel-aws-sandbox: OTel Logs SDK signal test")

        send_custom_metric(
            "otel_aws_sandbox.custom_metric_test", 1, tags=["source:custom-metrics-test"]
        )

    if TEST_UNSUPPORTED_SIGNALS:
        # Force-flush both providers before the function can freeze/return --
        # Lambda may freeze the execution environment between invocations, and
        # neither provider's background export timer is guaranteed to fire
        # before that happens.
        meter_provider.force_flush()
        logger_provider.force_flush()

    return {
        "statusCode": 200,
        "body": "otel-aws-sandbox: OK",
    }


# AwsLambdaInstrumentor wraps the handler it finds via the Lambda runtime's
# `_HANDLER` env var (e.g. "handler.lambda_handler") by name-lookup on this
# module. It must run *after* lambda_handler is defined above, or the lookup
# hits a partially-initialized module and raises AttributeError.
AwsLambdaInstrumentor().instrument(tracer_provider=tracer_provider)
