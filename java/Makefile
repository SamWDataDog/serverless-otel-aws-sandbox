# UNTESTED wrapper around the commands in README.md. Written defensively
# but never run end-to-end in this session (no AWS/Datadog credentials
# available) -- treat any failure here as equally likely to be a
# Makefile bug as a real problem until someone runs it once.
#
# Always runs a plain `sam build` with no function argument -- a scoped
# build silently deletes the sibling function's artifacts (see README.md).

SERVICE       ?= otel-aws-sandbox-java
SERVICE_MODE2 ?= ddtrace-otel-api-sandbox-java
STACK         ?= serverless-otel-aws-sandbox-java

.PHONY: build deploy invoke verify destroy

build:
	sam build

# The @ below suppresses make's recipe echo, and the doubled dollar sign
# before DD_API_KEY makes make emit a literal single-dollar reference for
# the shell to expand at run time -- the key never passes through make
# itself, so it can't get printed to stdout/terminal scrollback/CI logs.
# Do not "simplify" this back to a make-variable reference or drop the @.
# (This comment is deliberately OUTSIDE the recipe body below, not a
# recipe comment line -- make macro-expands recipe text, including
# comments inside it, before handing it to the shell. A comment with a
# real make-variable reference inside the recipe body would itself leak
# the secret the same way the bug being fixed here did.)
deploy:
	@sam deploy --stack-name $(STACK) \
	  --parameter-overrides "DatadogApiKey=$$DD_API_KEY" \
	  --capabilities CAPABILITY_IAM --resolve-s3

invoke:
	aws lambda invoke --function-name otel-aws-sandbox-java \
	  --cli-binary-format raw-in-base64-out --payload '{"source":"manual-test"}' /tmp/out.json
	cat /tmp/out.json

# Both services are expected to work (unlike .NET Mode 2 -- see
# dotnet/Makefile) -- a non-zero exit here is real signal, left strict
# on purpose. Do not copy dotnet/Makefile's `-@ ... || echo` exception
# into this target.
verify:
	../scripts/verify.sh "$(SERVICE)"
	../scripts/verify.sh "$(SERVICE_MODE2)"

destroy:
	sam delete --stack-name $(STACK) --no-prompts
