#!/usr/bin/env bash
#
# UNTESTED. Written defensively, but has never successfully queried a
# live Datadog backend. Treat any failure as equally likely to be a
# script bug as a real "no data landed" result until someone runs it
# for the first time and reports back.
#
# Queries the Datadog Spans Search API for at least one span from the
# given service within the last N minutes. Exits 0 if found, 1 if not
# found or on any request error, 2 on usage error.
#
# Usage:
#   ./scripts/verify.sh <service-name> [lookback-minutes]
#
# Requires:
#   DD_API_KEY, DD_APP_KEY  -- DD_APP_KEY is a new prerequisite this
#   script introduces; it is not needed anywhere else in this repo.
#   DD_SITE defaults to datadoghq.com, same as every other script here.
#   curl, python3 (for JSON parsing -- no jq dependency assumed).
#
# Credential handling: both keys are only ever interpolated into curl's
# -H header arguments below, never echoed or included in any error
# message (error paths only ever name the missing env var, not its
# value). `set -x` is never enabled. They remain visible to `ps` while
# curl is running, the same way any command-line argument containing a
# secret would be -- inherent to invoking curl this way, and accepted
# as a tradeoff for this sandbox script, not a bug to fix here.
#
# -----------------------------------------------------------------------
# DATADOG-SIDE PREREQUISITE -- read this before trusting a NOT FOUND:
#
# This org runs Intelligent Retention (roughly a 1% baseline plus
# outliers -- errors, latency anomalies). Spans Search only returns
# INDEXED spans. A single happy-path sandbox invocation is exactly what
# retention sampling is designed to drop, so without a retention filter
# covering these services, a NOT FOUND result here is the *expected*
# outcome roughly 99% of the time and tells you nothing about whether
# the span actually landed.
#
# To make this check deterministic, configure a 100% retention filter
# for `otel-aws-sandbox*` and `ddtrace-otel-api-sandbox*` (the volume
# from a sandbox is negligible). This script does not and cannot
# configure that for you -- exactly where retention filters live in the
# Datadog UI depends on your org's version/navigation, so this script
# does not attempt to give exact click-path instructions; search your
# org's APM/Retention Filters settings for "retention filter" and scope
# one to these service names at 100%.
#
# A NON-ZERO EXIT FROM THIS SCRIPT IS NOT EVIDENCE OF FAILURE unless
# that filter is in place. This mirrors FINDINGS.md's own evidentiary
# discipline elsewhere in this repo: the Extension's "successfully sent
# trace" log line is not proof a span was ingested; symmetrically, a
# search miss here is not proof one wasn't.
# -----------------------------------------------------------------------

set -euo pipefail

SERVICE="${1:-}"
LOOKBACK_MINUTES="${2:-15}"

if [[ -z "$SERVICE" ]]; then
  echo "Usage: $0 <service-name> [lookback-minutes]" >&2
  exit 2
fi

if [[ -z "${DD_API_KEY:-}" ]]; then
  echo "DD_API_KEY is not set." >&2
  exit 2
fi

if [[ -z "${DD_APP_KEY:-}" ]]; then
  echo "DD_APP_KEY is not set -- this script needs it in addition to" >&2
  echo "DD_API_KEY (read access to the Spans Search API), unlike every" >&2
  echo "other script in this repo." >&2
  exit 2
fi

DD_SITE="${DD_SITE:-datadoghq.com}"

FROM_TS="now-${LOOKBACK_MINUTES}m"

REQUEST_BODY=$(python3 -c "
import json, sys
print(json.dumps({
    'data': {
        'type': 'search_request',
        'attributes': {
            'filter': {
                'query': f'service:{sys.argv[1]}',
                'from': sys.argv[2],
                'to': 'now',
            },
            'page': {'limit': 1},
        },
    },
}))
" "$SERVICE" "$FROM_TS")

RESPONSE_BODY="$(mktemp)"
trap 'rm -f "$RESPONSE_BODY"' EXIT

# curl's own failure (DNS/network/TLS, before any HTTP response exists)
# is checked via its exit code, separately from the HTTP status code --
# combining both into one captured value (the earlier version of this
# script did, via `$(curl ... || echo "000")`) can produce a mangled,
# effectively two-part value if curl writes partial output before
# failing. set +e/set -e around just this call keeps `set -e` from
# exiting the script before this script's own error handling runs.
set +e
STATUS="$(curl -s -o "$RESPONSE_BODY" -w '%{http_code}' \
  -X POST "https://api.${DD_SITE}/api/v2/spans/events/search" \
  -H "DD-API-KEY: ${DD_API_KEY}" \
  -H "DD-APPLICATION-KEY: ${DD_APP_KEY}" \
  -H "Content-Type: application/json" \
  -d "$REQUEST_BODY")"
CURL_EXIT=$?
set -e

if [[ $CURL_EXIT -ne 0 ]]; then
  echo "curl itself failed (exit $CURL_EXIT) -- a network/DNS/TLS error" >&2
  echo "before any HTTP response, not a Datadog API response. Check" >&2
  echo "connectivity and DD_SITE (currently '${DD_SITE}')." >&2
  exit 1
fi

if [[ "$STATUS" != "200" ]]; then
  echo "Datadog API request failed (HTTP $STATUS):" >&2
  cat "$RESPONSE_BODY" >&2
  exit 1
fi

COUNT=$(python3 -c "
import json, sys
try:
    body = json.load(sys.stdin)
    print(len(body.get('data', [])))
except Exception as e:
    print('PARSE_ERROR:' + str(e), file=sys.stderr)
    print(0)
" < "$RESPONSE_BODY")

if [[ "$COUNT" -ge 1 ]]; then
  echo "OK: found at least one span for service='${SERVICE}' in the last ${LOOKBACK_MINUTES}m."
  exit 0
else
  echo "NOT FOUND: no spans for service='${SERVICE}' in the last ${LOOKBACK_MINUTES}m." >&2
  echo "This does NOT mean ingestion failed. In likelihood order:" >&2
  echo "  1. Most likely: not retained. Without a 100% retention filter" >&2
  echo "     for this service (see the header comment above), Intelligent" >&2
  echo "     Retention samples down to ~1% + outliers -- a single" >&2
  echo "     happy-path invocation is exactly what gets dropped." >&2
  echo "  2. Possible: indexing lag. Try a longer lookback window" >&2
  echo "     (this run used ${LOOKBACK_MINUTES}m) -- newly-ingested spans" >&2
  echo "     aren't always immediately searchable." >&2
  echo "  3. Possible, lowest likelihood if (1) is addressed: genuinely" >&2
  echo "     not ingested -- check CloudWatch logs for local errors first." >&2
  exit 1
fi
