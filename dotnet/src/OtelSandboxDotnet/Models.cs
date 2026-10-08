namespace OtelStdoutSample;

// Local addition (not in the original ported sample): the ported sample's
// original OrderRequest/OrderResponse order-processing shape has been
// replaced with the Request(source)/Response(body) shape every other
// language's Mode 1 handler uses, so this function takes the same
// {"source":"manual-test"} invoke payload as the rest of the repo and the
// apples-to-apples comparison premise actually holds. See Function.cs for
// the matching change (the added STS call/child span) and FINDINGS.md
// "Round 5" for what was verified against the original shape.
public sealed record Request(string? source);

public sealed record Response(string body);
