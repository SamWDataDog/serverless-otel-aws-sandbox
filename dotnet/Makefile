# UNTESTED wrapper around the commands in README.md. Written defensively
# but never run end-to-end in this session (no AWS/Datadog credentials
# available) -- treat any failure here as equally likely to be a
# Makefile bug as a real problem until someone runs it once.

SERVICE       ?= otel-aws-sandbox-dotnet
SERVICE_MODE2 ?= ddtrace-otel-api-sandbox-dotnet
STACK         ?= serverless-otel-aws-sandbox-dotnet

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
	aws lambda invoke --function-name otel-aws-sandbox-dotnet \
	  --cli-binary-format raw-in-base64-out --payload file://events/event.json /tmp/out.json
	cat /tmp/out.json

# Mode 2 (SERVICE_MODE2) is expected to report NOT FOUND -- confirmed
# zero spans ever ingested for this function, see FINDINGS.md. The `-@`
# and `|| echo ...` below keep that expected failure from making this
# target permanently red while still saying so in the output a TSE
# actually sees, not just in this comment.
verify:
	../scripts/verify.sh "$(SERVICE)"
	-@../scripts/verify.sh "$(SERVICE_MODE2)" || \
	  echo "(expected NOT FOUND: .NET Mode 2 has never ingested a span -- see FINDINGS.md)"

destroy:
	sam delete --stack-name $(STACK) --no-prompts
