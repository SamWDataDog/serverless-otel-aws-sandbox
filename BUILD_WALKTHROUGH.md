# Build Walkthrough: how this sandbox was actually built

**A note on identifiers below**: real 12-digit AWS account IDs have been
replaced with `<AWS_ACCOUNT_ID_A>`/`<AWS_ACCOUNT_ID_B>` (distinct
placeholders where the original text distinguishes between two
different accounts — collapsing both to the same placeholder would
destroy that distinction). This placeholdering is intentional, not an
indication the original walkthrough was sloppy about identifiers.

This is the "if you had to do this from scratch, with no AI, here's every
wall you'd hit" doc. `README.md` is the clean end-state instructions;
`FINDINGS.md` is the OTel/Datadog technical answer for the internal doc.
This file is the chronological record of what broke and why, so you (or a
support engineer debugging a customer's near-identical setup) can recognize these
failure signatures immediately instead of re-discovering them.

---

## 1. What's in this repo

All of the files below (except `FINDINGS.md`) live in `/python` — moved
there as pre-Java housekeeping (see `FINDINGS.md`) to match the `/node`
and `/dotnet` pattern added in later rounds; they floated at repo root
when this walkthrough was originally written, back when the repo was
Python-only.

| File | Purpose |
|---|---|
| `handler.py` | The Lambda function. Sets up an OTel `TracerProvider` pointed at the Extension's local OTLP receiver, instruments `boto3`/botocore calls and the Lambda invocation itself, and does the actual work. |
| `serverless.yml` | IaC config: runtime, region, env vars, the Datadog Extension layer ARN, IAM permissions, and packaging rules. |
| `requirements.txt` | Pinned Python dependency versions. Pinned (not floating) because OTel's `-api`/`-sdk`/`-semantic-conventions`/`-instrumentation-*` packages are version-locked to each other — mixing versions is its own failure mode (see §5). |
| `build.sh` | Vendors those dependencies into `./vendor` as Lambda-compatible (manylinux, cp311) wheels, **without Docker**. Exists specifically to route around the packaging bug in §3. |
| `.gitignore` | Excludes `vendor/`, `node_modules/`, `.serverless/` (all regenerable build output) and `.env`. |
| `package.json` / `package-lock.json` | Node devDependency manifest — originally added for the `serverless-python-requirements` plugin, which we ended up **not using** (see §3). Left in only because `npm install` still pulls in the Serverless Framework's own tooling; the plugin itself is no longer referenced in `serverless.yml`. |
| `FINDINGS.md` | The actual OTel/Datadog answer: does it work, is logs/trace correlation automatic, where the Datadog doc's Python snippet is wrong. Stays at repo root. |

## 2. Environment checks (before writing any code)

Before scaffolding anything, verify:

```bash
aws sts get-caller-identity        # confirms which AWS account/region you're about to deploy into
aws configure get region
serverless --version               # v4 requires a Serverless Dashboard login by default
python3 --version                  # match to the runtime you'll declare (3.11 here)
```

**Why this matters**: we later found the Serverless Framework was logged in
under a dashboard account, and separately discovered the DD_API_KEY the
user initially proposed lived in a Secrets Manager secret **in a different
AWS account** than the deploy target (`<AWS_ACCOUNT_ID_A>` vs. `<AWS_ACCOUNT_ID_B>`).
Cross-account `secretsmanager:GetSecretValue` fails with
`AccessDeniedException` unless the secret's resource policy explicitly
grants the other account access — check `aws sts get-caller-identity`
*and* which account a referenced secret/ARN actually lives in before
wiring either into IaC. We sidestepped it by reading `DD_API_KEY` from the
deployer's own shell env instead (`${env:DD_API_KEY}` in `serverless.yml`).

## 3. Getting the layer ARN right

The Datadog doc's copy-paste example shows:
```
arn:aws:lambda:sa-east-1:464622532012:layer:Datadog-Extension:53
```
The account ID (`464622532012`) is the same Datadog-owned account across
commercial AWS regions, but the **version number is per-region and moves
over time** — using the doc's literal ARN in a different region either
fails to resolve or silently pins you to a stale/wrong-region build.

How we found the real one, without a Datadog account/UI:
```bash
aws lambda get-layer-version-by-arn \
  --arn "arn:aws:lambda:us-east-1:464622532012:layer:Datadog-Extension:99" \
  --region us-east-1
```
Datadog's Lambda layers grant public `lambda:GetLayerVersion` on their
resource policy, so **any** AWS account can call this — you don't need to
own the layer. We started from a plausible version (found by asking a web
search / doc scrape for a recent number), confirmed it resolved, then
probed version+1, +2, ... until we got `AccessDeniedException` (meaning
that version doesn't exist yet) to find the actual latest: `100`.

**Customer-support relevance**: if a customer's deploy fails with something
like `Layer version does not exist` or `ResourceNotFoundException` on the
Extension layer, 9 times out of 10 it's a stale ARN (copied from a doc,
another region's Terraform, or an old runbook) — same class of bug.

## 4. First deploy: succeeded. First invoke: failed.

```
"errorType": "StopIteration"
...
INIT_REPORT Init Duration: 612.11 ms  Phase: init  Status: error  Error Type: Runtime.Unknown
```

If you look at this in the AWS Lambda console (Monitor tab / CloudWatch
Logs Insights) rather than raw JSON, **this is what shows up labeled
"Unhandled exception."** That label just means: an exception propagated out
of module-level code (import time), not from inside a try/except in the
handler — the Lambda Python runtime has no handler to call yet because the
module failed to even finish loading. `Phase: init` in the report confirms
it died during cold-start initialization, before the first invocation ever
reached `lambda_handler`. This is a generic bucket — the actual cause is
always in the traceback above it, never in "Unhandled exception" itself.

**The traceback:**
```
File "/var/task/opentelemetry/context/__init__.py", line 59, in _load_runtime_context
    return next(  # type: ignore
StopIteration
```

**Root cause**: `opentelemetry-api` discovers its context implementation
(`ContextVarsRuntimeContext`) via Python's `importlib.metadata.entry_points()`
— a mechanism that reads `entry_points.txt` inside a package's
`.dist-info/` metadata directory. Our deployment zip had **no `.dist-info`
directories at all** for any vendored package. With zero entry points found,
`next(iter(...))` on an empty iterator raises `StopIteration` instead of a
helpful "package metadata missing" error.

**Why the metadata was missing**: we were using the
`serverless-python-requirements` plugin (declared in `serverless.yml` under
`custom.pythonRequirements`). Serverless Framework v4 bundles a newer,
`uv`-based installer for this that — in this environment — stripped
`.dist-info` regardless of the plugin's `slim: true/false` setting, and
(separately) installed macOS (`darwin`) compiled binaries instead of
Linux/Lambda-compatible ones, because `dockerizePip: false` means "just use
whatever `pip` is on this machine" and this machine is a Mac.

**First fix attempt (didn't work)**: set `slim: false` and redeploy.
Same error, byte-for-byte. Turned out the plugin caches its build output on
disk, keyed by a hash of `requirements.txt`, at
`~/Library/Caches/serverless-python-requirements/<hash>_x86_64_slspyc`.
Since `requirements.txt` hadn't changed, the config change was silently
ignored and the stale, broken cache was reused. **Lesson: if you change a
packaging-related config and get an identical error, suspect a build
cache before suspecting your config change.**

**Second fix attempt (blocked)**: set `dockerizePip: true` to build inside
a Lambda-like Docker container (which normally *does* preserve `.dist-info`
correctly). Failed differently:
```
Error: `docker run --rm -v .../slspyc:/test alpine stat -c %u /bin/sh` Exited with code 1
```
Root cause: `docker run --rm alpine echo hello` also failed:
```
failed to connect to the docker API at unix:///Users/.../.orbstack/run/docker.sock:
connect: no such file or directory
```
Docker CLI was installed (pointing at OrbStack) but the OrbStack daemon
wasn't actually running. This is an environment problem, not a code
problem — the fix would normally be "start OrbStack/Docker Desktop," but
we deliberately avoided that (didn't want to launch a GUI app as a side
effect) and instead removed the Docker dependency entirely.

**Actual fix**: stopped using the plugin altogether. Wrote `build.sh`,
which just calls plain `pip install --target vendor` with explicit platform
flags:
```bash
python3 -m pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  --target vendor \
  -r requirements.txt
```
Plain `pip install --target` (unlike whatever the plugin's installer did)
keeps `.dist-info` intact by default, and the explicit `--platform` +
`--only-binary=:all:` flags force pip to download prebuilt `manylinux`
wheels instead of resolving to your host OS's wheels — this works without
Docker because we're not compiling anything, just fetching an already-built
wheel for a different platform than the host. (This only works because
every dependency here ships manylinux wheels; a package with no prebuilt
Linux wheel would still need Docker or a Lambda-like build environment.)

Then `handler.py` adds `vendor/` to `sys.path` at the top, before any OTel
imports:
```python
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
```
and `serverless.yml`'s `package.patterns` explicitly includes `vendor/**`
(with `node_modules/**`, `.git/**`, etc. excluded) so the zip contains the
real dependency code, not the empty/broken plugin output.

**Customer-support relevance**: any support ticket where a Python Lambda
fails at cold start with `StopIteration` in
`opentelemetry/context/__init__.py`, or any "works locally, fails in
Lambda" report for a package with C extensions, is almost always this same
family of bug: **packaging tool installed the wrong platform's wheels or
stripped package metadata.** Ask what packaging method they used
(`sam build`, `serverless-python-requirements`, a manual `pip install -t`,
a Lambda Layer built elsewhere) and whether it targeted `manylinux`/Docker.

## 5. Second invoke attempt: new error

With packaging fixed, the `StopIteration` was gone, but invoke failed with:
```
AttributeError: partially initialized module 'handler' has no attribute
'lambda_handler' (most likely due to a circular import)
  File "/var/task/handler.py", line 33, in <module>
    AwsLambdaInstrumentor().instrument(tracer_provider=tracer_provider)
  ...
  File ".../wrapt/patches.py", line 46, in lookup_attribute
    return getattr(parent, attribute)
```

**Root cause**: `AwsLambdaInstrumentor().instrument()` finds "the real
handler" by reading the Lambda runtime's `_HANDLER` env var (which AWS sets
automatically from your configured handler string, `handler.lambda_handler`),
importing that module, and monkey-patching the named attribute onto it. Our
original `handler.py` called `.instrument()` near the *top* of the file,
**before** `def lambda_handler(...)` had been defined further down. At that
point in execution, the `handler` module exists but is only *partially*
built — `lambda_handler` isn't an attribute on it yet — so the lookup fails.

This is exactly the kind of bug the Datadog doc's flat, top-to-bottom code
snippet doesn't warn you about, because the doc's own example is a
simplified fragment, not a full runnable file with this ordering
constraint spelled out.

**Fix**: moved `AwsLambdaInstrumentor().instrument(...)` to *after* the
`lambda_handler` function definition, at the bottom of the file. Left
`BotocoreInstrumentor().instrument()` where it was (near the top), since
it patches the `botocore` library itself, not something inside our own
still-loading module — no ordering constraint there.

**Customer-support relevance**: if a customer's Lambda using
`opentelemetry-instrumentation-aws-lambda` throws a circular-import
`AttributeError` naming their own handler module, check whether
`AwsLambdaInstrumentor().instrument()` is called before or after their
handler function is defined in the same file.

## 6. It ran (200 OK) — but the log lines were missing

Invoke succeeded, but neither of the two `logger.info(...)` calls in
`handler.py` showed up in CloudWatch — only an unrelated OTel `WARNING`
about `MeterProvider`.

**Root cause**: the code called `logging.basicConfig(level=logging.INFO)`
at the top of `handler.py`. `logging.basicConfig()` only takes effect if
the root logger has **no handlers attached yet**; on the Lambda Python
runtime, `awslambdaric` (the runtime's own bootstrap) already attaches a
handler to the root logger *before* your module ever runs. So our
`basicConfig()` call silently did nothing, and the root logger's default
level (`WARNING`) stayed in effect — every `logger.info(...)` call was
below that threshold and got dropped.

**Fix**: set the level directly instead of using `basicConfig`:
```python
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
```

**Customer-support relevance**: "my Lambda's `logger.info` calls never
show up in CloudWatch" is a very common ticket, and `logging.basicConfig()`
being a no-op inside Lambda is the near-universal cause. Same root issue
also bites anyone who expects `logging.basicConfig(format=...)` to change
the log line format inside a Lambda — it won't, for the same reason.

## 7. Confirming the actual trace flow (debug logging)

Once things ran cleanly, CloudWatch on its own didn't show explicit
"trace sent" confirmation — the Extension only logs that verbosely at
debug level. Temporarily added:
```yaml
environment:
  DD_LOG_LEVEL: debug
```
to `serverless.yml`, redeployed, invoked once, and pulled the raw log
stream directly from CloudWatch (`aws logs get-log-events`, not the
Serverless CLI's `logs` command, which was truncating/reordering some
lines):
```
DD_EXTENSION | DEBUG | OTLP HTTP | Starting collector on 127.0.0.1:4318
DD_EXTENSION | DEBUG | OTLP | Successfully buffered traces to be aggregated.   (x3)
DD_EXTENSION | DEBUG | TRACES | Successfully sent trace (1 attempts, 1277 bytes)
```
This is the actual proof the sandbox works — the 3 buffered-trace lines
correspond to the 3 spans (Lambda invocation, `do-work`, `STS.GetCallerIdentity`).
Once confirmed, removed `DD_LOG_LEVEL: debug` again to keep the final
config matching Datadog's documented defaults (debug logging is
noisy/costly to leave on permanently).

## 8. Proving reproducibility

Ran a full clean cycle before calling it done:
```bash
cd python                      # all Python source/config lives here
npx serverless remove          # tear down the stack completely
rm -rf vendor .serverless       # remove all generated build output
./build.sh                      # rebuild dependencies from scratch
npx serverless deploy
npx serverless invoke -f otelSandbox -d '{"source":"repro-check"}'
```
All succeeded from a clean state — confirms nothing depended on leftover
local cache/state from the earlier failed attempts.

## 9. Summary table

| Symptom | Root cause | Fix |
|---|---|---|
| `Layer version does not exist` / wrong-region ARN | Doc's example ARN is region-specific and version-specific | Look up the real ARN with `aws lambda get-layer-version-by-arn` against the target region; don't copy-paste across regions |
| Cross-account `secretsmanager:GetSecretValue` `AccessDeniedException` | Secret lived in a different AWS account than the deploy target | Either grant cross-account resource policy, or source the value another way (we used a shell env var) |
| `StopIteration` in `opentelemetry/context/__init__.py`, `Phase: init`, shows as "Unhandled exception" in console | Packaging tool stripped `.dist-info` metadata and/or installed macOS binaries instead of Linux ones | Vendor deps with `pip install --target --platform manylinux2014_x86_64 --only-binary=:all:` instead of relying on the plugin's non-Docker installer |
| Identical error after changing plugin config | Plugin's on-disk build cache (keyed by requirements hash) wasn't invalidated | Clear `~/Library/Caches/serverless-python-requirements` (or whatever the tool's cache dir is) before assuming a config change had no effect |
| `docker run` fails with "no such file or directory" on a `.sock` path | Docker CLI installed, but the Docker/OrbStack daemon wasn't running | Either start the daemon, or avoid the Docker dependency (we did the latter) |
| `AttributeError: partially initialized module ... has no attribute 'lambda_handler'` | `AwsLambdaInstrumentor().instrument()` called before the handler function was defined in the same file | Call `.instrument()` after the handler function definition |
| `logger.info()` lines never appear in CloudWatch | `logging.basicConfig()` is a no-op inside Lambda (root logger already has a handler from the runtime) | Use `logging.getLogger().setLevel(logging.INFO)` instead |
| No visible confirmation the Extension sent a trace | Extension only logs that at debug level | Temporarily set `DD_LOG_LEVEL: debug`, confirm, then remove it |
