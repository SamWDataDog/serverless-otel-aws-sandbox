package com.datadog.sandbox;

import io.opentelemetry.api.metrics.LongCounter;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.exporter.otlp.http.metrics.OtlpHttpMetricExporter;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.instrumentation.awssdk.v2_2.AwsSdkTelemetry;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.metrics.SdkMeterProvider;
import io.opentelemetry.sdk.metrics.export.PeriodicMetricReader;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.time.Duration;
import software.amazon.awssdk.core.client.config.ClientOverrideConfiguration;
import software.amazon.awssdk.services.sts.StsClient;

/**
 * Round 6, Mode 1 setup. No official Datadog doc sample exists for Java
 * (see FINDINGS.md "Round 6"), so this wires the official OpenTelemetry
 * Java SDK + AWS Lambda/AWS SDK v2 instrumentation libraries directly,
 * mirroring the Python/Node/.NET Mode 1 pattern: OTLP/HTTP to the
 * Extension's local receiver, no Datadog tracing layer.
 */
final class TelemetryRuntime {

  static final String TRACER_NAME = "otel-aws-sandbox-java";

  final OpenTelemetrySdk openTelemetrySdk;
  final Tracer tracer;
  final LongCounter otelCounter;
  final StsClient stsClient;

  private TelemetryRuntime(OpenTelemetrySdk openTelemetrySdk, StsClient stsClient) {
    this.openTelemetrySdk = openTelemetrySdk;
    this.tracer = openTelemetrySdk.getTracer(TRACER_NAME);
    this.otelCounter =
        openTelemetrySdk
            .getMeter(TRACER_NAME)
            .counterBuilder("otel_aws_sandbox_java.otel_metric_test")
            .build();
    this.stsClient = stsClient;
  }

  static TelemetryRuntime create() {
    SdkTracerProvider tracerProvider =
        SdkTracerProvider.builder()
            .addSpanProcessor(
                SimpleSpanProcessor.create(
                    OtlpHttpSpanExporter.builder()
                        .setEndpoint("http://localhost:4318/v1/traces")
                        .build()))
            .build();

    // Round 6 addition, matching Python/Node/.NET's signal matrix: an OTel
    // metric, expected to 404 against the Extension (same reasoning as
    // every prior round -- the Extension's OTLP receiver only implements
    // /v1/traces).
    SdkMeterProvider meterProvider =
        SdkMeterProvider.builder()
            .registerMetricReader(
                PeriodicMetricReader.builder(
                        OtlpHttpMetricExporter.builder()
                            .setEndpoint("http://localhost:4318/v1/metrics")
                            .build())
                    .setInterval(Duration.ofSeconds(1))
                    .build())
            .build();

    OpenTelemetrySdk sdk =
        OpenTelemetrySdk.builder()
            .setTracerProvider(tracerProvider)
            .setMeterProvider(meterProvider)
            .build();

    StsClient sts =
        StsClient.builder()
            .overrideConfiguration(
                ClientOverrideConfiguration.builder()
                    .addExecutionInterceptor(
                        AwsSdkTelemetry.create(sdk).newExecutionInterceptor())
                    .build())
            .build();

    return new TelemetryRuntime(sdk, sts);
  }
}
