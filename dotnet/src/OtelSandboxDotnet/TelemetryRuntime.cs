using System.Diagnostics;
using System.Diagnostics.Metrics;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Instrumentation.AWSLambda;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;

namespace OtelStdoutSample;

internal sealed class TelemetryRuntime : IDisposable
{
    public const string ActivitySourceName = "OtelStdoutSample";

    // Round 5 addition (not in the original ported sample): OTel metrics
    // via OTLP, to complete the signal matrix the same way Python/Node
    // tested it -- expected to 404 against the Extension, same reasoning
    // as Rounds 1/4. See FINDINGS.md "Round 5".
    public const string MeterName = "OtelStdoutSample";

    private readonly ServiceProvider _services;

    private TelemetryRuntime(
        ServiceProvider services,
        TracerProvider tracerProvider,
        MeterProvider meterProvider,
        ILogger logger)
    {
        _services = services;
        TracerProvider = tracerProvider;
        MeterProvider = meterProvider;
        Logger = logger;
    }

    public TracerProvider TracerProvider { get; }

    public MeterProvider MeterProvider { get; }

    public ILogger Logger { get; }

    public ActivitySource ActivitySource { get; } = new(ActivitySourceName);

    public Meter Meter { get; } = new(MeterName);

    public static TelemetryRuntime Create(
        TextWriter? logOutput = null,
        bool exportTraces = true)
    {
        var services = new ServiceCollection();
        services.AddLogging(builder =>
        {
            builder.AddOpenTelemetry(options =>
            {
                options.IncludeFormattedMessage = true;
                options.ParseStateValues = true;
                options.AddProcessor(
                    new SimpleLogRecordExportProcessor(
                        new JsonConsoleLogExporter(logOutput ?? Console.Out)));
            });
        });

        var tracerBuilder = Sdk.CreateTracerProviderBuilder()
            .AddSource(ActivitySourceName)
            .AddAWSInstrumentation()
            .AddAWSLambdaConfigurations(options =>
            {
                options.DisableAwsXRayContextExtraction = true;
            });

        if (exportTraces)
        {
            var exporterOptions = new OtlpExporterOptions
            {
                Protocol = OtlpExportProtocol.HttpProtobuf,
            };
            tracerBuilder.AddProcessor(
                new SimpleActivityExportProcessor(new OtlpTraceExporter(exporterOptions)));
        }

        var tracerProvider = tracerBuilder.Build();

        var meterProvider = Sdk.CreateMeterProviderBuilder()
            .AddMeter(MeterName)
            .AddOtlpExporter((exporterOptions, metricReaderOptions) =>
            {
                exporterOptions.Protocol = OtlpExportProtocol.HttpProtobuf;
                metricReaderOptions.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds = 1000;
            })
            .Build();

        var provider = services.BuildServiceProvider();
        var logger = provider
            .GetRequiredService<ILoggerFactory>()
            .CreateLogger("OtelStdoutSample");

        return new TelemetryRuntime(provider, tracerProvider, meterProvider, logger);
    }

    public void Dispose()
    {
        TracerProvider.Dispose();
        MeterProvider.Dispose();
        ActivitySource.Dispose();
        _services.Dispose();
    }
}
