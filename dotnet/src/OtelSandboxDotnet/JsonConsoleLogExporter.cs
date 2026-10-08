using System.Globalization;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Logs;

namespace OtelStdoutSample;

internal sealed class JsonConsoleLogExporter : BaseExporter<LogRecord>
{
    private static readonly JsonSerializerOptions SerializerOptions = new()
    {
        PropertyNamingPolicy = null,
    };

    private readonly TextWriter _output;
    private readonly object _gate = new();

    public JsonConsoleLogExporter(TextWriter? output = null)
    {
        _output = output ?? Console.Out;
    }

    public override ExportResult Export(in Batch<LogRecord> batch)
    {
        foreach (var record in batch)
        {
            var line = Format(record);
            lock (_gate)
            {
                _output.WriteLine(line);
            }
        }

        return ExportResult.Success;
    }

    internal static string Format(LogRecord record)
    {
        var attributes = new Dictionary<string, object?>();
        if (record.Attributes is not null)
        {
            foreach (var attribute in record.Attributes)
            {
                attributes[attribute.Key] = attribute.Value;
            }
        }

        var payload = new Dictionary<string, object?>
        {
            ["message"] = record.FormattedMessage ?? record.Body?.ToString() ?? string.Empty,
            ["status"] = ToDatadogStatus(record.LogLevel),
        };

        var traceIdHex = record.TraceId.ToHexString();
        var spanIdHex = record.SpanId.ToHexString();
        if (!IsEmptyId(traceIdHex))
        {
            payload["dd.trace_id"] = ToDatadogId(traceIdHex);
            payload["dd.span_id"] = ToDatadogId(spanIdHex);
        }

        foreach (var attribute in attributes)
        {
            if (attribute.Key == "{OriginalFormat}")
            {
                continue;
            }

            payload[attribute.Key] = attribute.Value;
        }

        payload["otel"] = new Dictionary<string, object?>
        {
            ["trace_id"] = IsEmptyId(traceIdHex) ? null : traceIdHex,
            ["span_id"] = IsEmptyId(spanIdHex) ? null : spanIdHex,
            ["severity"] = record.LogLevel.ToString(),
            ["body"] = record.Body?.ToString() ?? record.FormattedMessage,
            ["attributes"] = attributes,
        };

        return JsonSerializer.Serialize(payload, SerializerOptions);
    }

    private static string ToDatadogStatus(LogLevel level) =>
        level switch
        {
            LogLevel.Trace or LogLevel.Debug => "debug",
            LogLevel.Information => "info",
            LogLevel.Warning => "warn",
            LogLevel.Error or LogLevel.Critical => "error",
            _ => "info",
        };

    private static string ToDatadogId(string hex)
    {
        var lower = hex.Length > 16 ? hex[^16..] : hex;
        return ulong.Parse(lower, NumberStyles.HexNumber, CultureInfo.InvariantCulture)
            .ToString(CultureInfo.InvariantCulture);
    }

    private static bool IsEmptyId(string hex) =>
        string.IsNullOrEmpty(hex) || hex.Trim('0').Length == 0;
}
