package com.datadog.sandbox;

import com.amazonaws.services.lambda.runtime.Context;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.instrumentation.awslambdacore.v1_0.TracingRequestHandler;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import software.amazon.awssdk.services.sts.model.GetCallerIdentityRequest;
import software.amazon.awssdk.services.sts.model.GetCallerIdentityResponse;

/**
 * Round 6, Mode 1: real OTel SDK TracerProvider, exporting via OTLP/HTTP to
 * the Datadog Lambda Extension's local receiver. No Datadog tracing layer.
 * {@code TracingRequestHandler} (official OpenTelemetry Java
 * instrumentation) creates the {@code aws.lambda} root span automatically
 * and force-flushes traces, metrics, and logs together on every
 * invocation -- see FINDINGS.md "Round 6" for why that matters (Lambda can
 * freeze the execution environment before a periodic exporter's timer
 * fires, the same concern Python/Node/.NET had to handle manually).
 */
public class OtelSandboxHandler
    extends TracingRequestHandler<Map<String, Object>, Map<String, Object>> {

  private static final TelemetryRuntime RUNTIME = TelemetryRuntime.create();

  public OtelSandboxHandler() {
    super(RUNTIME.openTelemetrySdk);
  }

  @Override
  protected Map<String, Object> doHandleRequest(Map<String, Object> input, Context context) {
    System.out.println("otel-aws-sandbox-java: handler invoked, event=" + input);

    Span span = RUNTIME.tracer.spanBuilder("do-work").startSpan();
    try (Scope scope = span.makeCurrent()) {
      GetCallerIdentityResponse identity =
          RUNTIME.stsClient.getCallerIdentity(GetCallerIdentityRequest.builder().build());
      System.out.println(
          "otel-aws-sandbox-java: sts.getCallerIdentity account="
              + identity.account()
              + " arn="
              + identity.arn());
      span.setAttribute(AttributeKey.stringKey("aws.sts.account"), identity.account());
    } finally {
      span.end();
    }

    RUNTIME.otelCounter.add(1, io.opentelemetry.api.common.Attributes.of(
        AttributeKey.stringKey("source"), "otel-metrics-test"));
    sendCustomMetric("otel_aws_sandbox_java.custom_metric_test", 1, "source:custom-metrics-test");

    Map<String, Object> response = new HashMap<>();
    response.put("body", "otel-aws-sandbox-java: OK");
    return response;
  }

  /**
   * Round 6 signal: Datadog custom metric via raw DogStatsD UDP, same
   * approach as Python/Node/.NET -- no Java equivalent of
   * datadog-lambda-js's handler-wrapping side effect to worry about (see
   * FINDINGS.md "Round 6" for the datadog-lambda-java check).
   */
  static void sendCustomMetric(String name, long value, String tag) {
    try {
      byte[] packet =
          (name + ":" + value + "|d|#" + tag).getBytes(StandardCharsets.UTF_8);
      try (DatagramSocket socket = new DatagramSocket()) {
        socket.send(new DatagramPacket(packet, packet.length, InetAddress.getLoopbackAddress(), 8125));
      }
    } catch (Exception e) {
      System.out.println("otel-aws-sandbox-java: failed to send custom metric: " + e);
    }
  }
}
