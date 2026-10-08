import logging
import socket

import boto3
from opentelemetry import trace

# Entry point is datadog_lambda.handler.handler (set in serverless.yml),
# which imports this module dynamically -- too late to sys.path.insert
# vendor-ddtrace/ here, so serverless.yml sets PYTHONPATH instead.

# OpenTelemetry API support within Datadog SDKs: plain OTel API calls,
# but the tracer behind them is ddtrace's (DD_TRACE_OTEL_ENABLED=true in
# serverless.yml), not an OTel SDK TracerProvider set up here.

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

tracer = trace.get_tracer(__name__)


# Raw DogStatsD UDP, same as Mode 1: datadog_lambda's import-time
# patch_all() would contaminate the native-tracer test this function runs.
def send_custom_metric(name, value, tags=None):
    tag_str = ("|#" + ",".join(tags)) if tags else ""
    packet = f"{name}:{value}|d{tag_str}".encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(packet, ("127.0.0.1", 8125))
    finally:
        sock.close()


def lambda_handler(event, context):
    logger.info("ddtrace-otel-api-sandbox: handler invoked, event=%s", event)

    with tracer.start_as_current_span("do-work") as span:
        span.set_attribute("sandbox.event_keys", list(event.keys()) if isinstance(event, dict) else "n/a")

        sts = boto3.client("sts")
        identity = sts.get_caller_identity()

        logger.info(
            "ddtrace-otel-api-sandbox: sts.get_caller_identity account=%s arn=%s",
            identity.get("Account"),
            identity.get("Arn"),
        )

        send_custom_metric(
            "ddtrace_otel_api_sandbox.custom_metric_test", 1, tags=["source:custom-metrics-test"]
        )

    return {
        "statusCode": 200,
        "body": "ddtrace-otel-api-sandbox: OK",
    }
