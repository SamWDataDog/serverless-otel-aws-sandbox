#!/usr/bin/env bash
# Vendors Lambda-compatible (manylinux, cp311) wheels into ./vendor so the
# deployment package doesn't depend on Docker or the local machine's platform.
set -euo pipefail

rm -rf vendor
python3 -m pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  --target vendor \
  -r requirements.txt

echo "Vendored dependencies into ./vendor"
