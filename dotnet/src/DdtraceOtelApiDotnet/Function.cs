// .NET equivalent of handler2.py/handler2.js: .NET's OTel API IS
// System.Diagnostics.Activity/ActivitySource (built into the BCL), so
// there's no TracerProvider-equivalent to construct here. Whether
// dd-trace-dotnet picks up manually-created Activities automatically via
// DD_TRACE_OTEL_ENABLED=true is the question -- see FINDINGS.md "Round 5".

using System.Diagnostics;
using Amazon.Lambda.Core;
using Amazon.SecurityToken;
using Amazon.SecurityToken.Model;

[assembly: LambdaSerializer(typeof(Amazon.Lambda.Serialization.SystemTextJson.DefaultLambdaJsonSerializer))]

namespace DdtraceOtelApiDotnet;

public sealed class Function
{
    private static readonly ActivitySource Source = new("ddtrace-otel-api-sandbox-dotnet");

    public async Task<Response> FunctionHandler(Request request, ILambdaContext context)
    {
        Console.WriteLine($"ddtrace-otel-api-sandbox-dotnet: handler invoked, event={System.Text.Json.JsonSerializer.Serialize(request)}");

        using var activity = Source.StartActivity("do-work");

        using var sts = new AmazonSecurityTokenServiceClient();
        var identity = await sts.GetCallerIdentityAsync(new GetCallerIdentityRequest());

        Console.WriteLine(
            $"ddtrace-otel-api-sandbox-dotnet: sts.GetCallerIdentity account={identity.Account} arn={identity.Arn}");

        return new Response("ddtrace-otel-api-sandbox-dotnet: OK");
    }
}

public sealed record Request(string? source);

public sealed record Response(string body);
