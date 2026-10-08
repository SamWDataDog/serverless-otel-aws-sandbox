# UNTESTED wrapper around the commands in README.md. Written defensively
# but never run end-to-end in this session (no AWS/Datadog credentials
# available) -- treat any failure here as equally likely to be a
# Makefile bug as a real problem until someone runs it once.
#
# Deploys functions 1, 2, 3, 5 only (function 4 is opt-in -- see README.md).

SERVICE      ?= otel-aws-sandbox
SERVICE_MODE2 ?= ddtrace-otel-api-sandbox
STACK        ?= serverless-otel-aws-sandbox-dev

.PHONY: build deploy invoke verify destroy

build:
	npm install
	./build.sh
	./build-ddtrace.sh

deploy:
	npx serverless deploy

invoke:
	npx serverless invoke -f otelSandbox -d '{"source":"manual-test"}' --log

# Both services are expected to work (unlike .NET Mode 2 -- see
# dotnet/Makefile) -- a non-zero exit here is real signal, left strict
# on purpose. Do not copy dotnet/Makefile's `-@ ... || echo` exception
# into this target.
verify:
	../scripts/verify.sh "$(SERVICE)"
	../scripts/verify.sh "$(SERVICE_MODE2)"

destroy:
	npx serverless remove
