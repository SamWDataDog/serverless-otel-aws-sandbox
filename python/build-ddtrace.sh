#!/usr/bin/env bash
# Vendors the native ddtrace/datadog-lambda path into ./vendor-ddtrace,
# kept separate from ./vendor (the clean OTel-only sandbox) so the two
# functions' dependency trees never mix.
set -euo pipefail

rm -rf vendor-ddtrace
python3 -m pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  --target vendor-ddtrace \
  -r requirements-ddtrace.txt

echo "Vendored ddtrace/datadog-lambda deps into ./vendor-ddtrace"
