// Round 2 §4 / Round 5 "Mode 1": ported, with attribution, from Siddhitha
// Bhoopathy's dotnet-lambda-otel-stdout-sample --
// https://github.com/SiddhithaBhoopathy/otel-dotnet-lambda-extension-samples
// Brought into this repo (not just a separate external clone) so it's
// deployed and reproducible from here, the same way otelSandbox/
// otelSandboxNode are. See FINDINGS.md "Round 5" for the re-verification.

using System.Diagnostics.Metrics;
using System.Net.Sockets;
using System.Text;
using Amazon.Lambda.Core;
using Amazon.SecurityToken;
using Amazon.SecurityToken.Model;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Instrumentation.AWSLambda;
using OpenTelemetry.Metrics;

[assembly: LambdaSerializer(typeof(Amazon.Lambda.Serialization.SystemTextJson.DefaultLambdaJsonSerializer))]

namespace OtelStdoutSample;

public sealed class Function
{
    private static readonly Lazy<TelemetryRuntime> Shared = new(() => TelemetryRuntime.Create());
    private readonly TelemetryRuntime _runtime;
    private readonly Counter<long> _otelCounter;

    public Function()
        : this(Shared.Value)
    {
    }

    internal Function(TelemetryRuntime runtime)
    {
        _runtime = runtime;
        _otelCounter = _runtime.Meter.CreateCounter<long>("otel_aws_sandbox_dotnet.otel_metric_test");
    }

    public Task<Response> FunctionHandler(Request request, ILambdaContext context)
        => AWSLambdaWrapper.TraceAsync(_runtime.TracerProvider, HandleAsync, request, context);

    internal async Task<Response> HandleAsync(Request request, ILambdaContext context)
    {
        using var activity = _runtime.ActivitySource.StartActivity("do-work");
        _runtime.Logger.LogInformation("otel-aws-sandbox-dotnet: handler invoked, source={Source}", request.source);

        // Local addition (not in the original ported sample): an
        // sts:GetCallerIdentity call inside this span, mirroring the
        // do-work -> STS-child pattern python/handler.py, node/src/
        // handler.js and OtelSandboxHandler.java already use, so this
        // function tests the same thing those three do -- whether an
        // AWS SDK child span gets created automatically (via
        // AddAWSInstrumentation() in TelemetryRuntime.cs) under a
        // manually-created parent activity. No IAM policy for this is
        // needed or granted -- see template.yaml's comment on this
        // function; sts:GetCallerIdentity requires no permission at all.
        using var sts = new AmazonSecurityTokenServiceClient();
        var identity = await sts.GetCallerIdentityAsync(new GetCallerIdentityRequest());
        _runtime.Logger.LogInformation(
            "otel-aws-sandbox-dotnet: sts.GetCallerIdentity account={Account} arn={Arn}",
            identity.Account,
            identity.Arn);

        // Round 5 additions (not in the original ported sample): OTel
        // metric + a Datadog custom metric via raw DogStatsD, to complete
        // the signal matrix the same way Python/Node tested it. See
        // FINDINGS.md "Round 5".
        _otelCounter.Add(1, new KeyValuePair<string, object?>("source", "otel-metrics-test"));
        SendCustomMetric("otel_aws_sandbox_dotnet.custom_metric_test", 1, "source:custom-metrics-test");

        // Lambda may freeze the execution environment before the metric
        // reader's periodic export timer fires -- force it now.
        _runtime.MeterProvider.ForceFlush();

        return new Response("otel-aws-sandbox-dotnet: OK");
    }

    private static void SendCustomMetric(string name, long value, string tag)
    {
        var packet = Encoding.UTF8.GetBytes($"{name}:{value}|d|#{tag}");
        using var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
        socket.SendTo(packet, new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, 8125));
    }
}
