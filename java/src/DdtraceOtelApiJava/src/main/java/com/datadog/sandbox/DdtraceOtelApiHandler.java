package com.datadog.sandbox;

import com.amazonaws.services.lambda.runtime.Context;
import com.amazonaws.services.lambda.runtime.RequestHandler;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Scope;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import software.amazon.awssdk.services.sts.StsClient;
import software.amazon.awssdk.services.sts.model.GetCallerIdentityRequest;
import software.amazon.awssdk.services.sts.model.GetCallerIdentityResponse;

/**
 * Round 6, Mode 2: "OpenTelemetry API support within Datadog SDKs" --
 * plain {@code GlobalOpenTelemetry.getTracer(...)} calls, no manual
 * TracerProvider/SDK setup. Datadog's own doc claims no explicit
 * registration is needed once {@code DD_TRACE_OTEL_ENABLED=true} is set --
 * this round did not take that on faith (see FINDINGS.md "Round 6": the
 * same claim for .NET turned into a confirmed, unresolved bug). Confirmed
 * here via real DDSpan construction logs showing correct parent/child
 * trace IDs, and the ordering-bug test (also in FINDINGS.md) confirmed
 * timing doesn't matter either -- dd-trace-java's javaagent instruments
 * {@code GlobalOpenTelemetry} itself via bytecode transformation before
 * any application class loads, so there is no "resolved too early"
 * failure mode here the way Node had one.
 */
public class DdtraceOtelApiHandler implements RequestHandler<Map<String, Object>, Map<String, Object>> {

  private static final StsClient STS_CLIENT = StsClient.create();

  @Override
  public Map<String, Object> handleRequest(Map<String, Object> input, Context context) {
    System.out.println("ddtrace-otel-api-sandbox-java: handler invoked, event=" + input);

    Tracer tracer = GlobalOpenTelemetry.getTracer("ddtrace-otel-api-sandbox-java");
    Span span = tracer.spanBuilder("do-work").startSpan();
    try (Scope scope = span.makeCurrent()) {
      GetCallerIdentityResponse identity =
          STS_CLIENT.getCallerIdentity(GetCallerIdentityRequest.builder().build());
      System.out.println(
          "ddtrace-otel-api-sandbox-java: sts.getCallerIdentity account="
              + identity.account()
              + " arn="
              + identity.arn());
    } finally {
      span.end();
    }

    sendCustomMetric("ddtrace_otel_api_sandbox_java.custom_metric_test", 1, "source:custom-metrics-test");

    Map<String, Object> response = new HashMap<>();
    response.put("body", "ddtrace-otel-api-sandbox-java: OK");
    return response;
  }

  /**
   * Follow-up gap-closing test (see FINDINGS.md): Mode 2's custom-metrics
   * signal was never actually sent anywhere in this repo, only assumed
   * safe by analogy to Mode 1. Raw DogStatsD UDP, same approach as Mode 1
   * -- datadog-lambda-java exists but is documented as unneeded on
   * current Extension versions, so there's no reason to prefer it over
   * the approach already used everywhere else in this repo.
   */
  private static void sendCustomMetric(String name, long value, String tag) {
    try {
      byte[] packet = (name + ":" + value + "|d|#" + tag).getBytes(StandardCharsets.UTF_8);
      try (DatagramSocket socket = new DatagramSocket()) {
        socket.send(new DatagramPacket(packet, packet.length, InetAddress.getLoopbackAddress(), 8125));
      }
    } catch (Exception e) {
      System.out.println("ddtrace-otel-api-sandbox-java: failed to send custom metric: " + e);
    }
  }
}
