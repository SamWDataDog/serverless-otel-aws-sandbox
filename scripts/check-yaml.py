#!/usr/bin/env python3
"""Parse every YAML/SAM-template config file this repo actually deploys
from, and exit non-zero if any fails to parse.

Requires PyYAML (the one non-stdlib dependency in this repo's own
scripts -- check-anchors.py and verify.sh are stdlib-only). The two SAM
templates use CloudFormation short tags (!Sub, !Ref, ...) that plain
yaml.safe_load doesn't know; those are parsed with a small tag-tolerant
loader instead of shelling out to `sam validate` (which would need the
AWS SAM CLI installed and is slower for a simple syntax check).

This only confirms the files are well-formed YAML (post-CFN-tag
tolerance) -- it does not validate CloudFormation/SAM semantics, and it
does not run `sam validate`'s schema checks.
"""
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML is required to run this check (pip install pyyaml).", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent

PLAIN_YAML_FILES = [
    "python/serverless.yml",
    "node/serverless.yml",
    "python/adot-collector.yaml",
    "python/otel-collector-dd/config.yaml",
]

CFN_TEMPLATE_FILES = [
    "dotnet/template.yaml",
    "java/template.yaml",
]


class _CfnTolerantLoader(yaml.SafeLoader):
    pass


def _cfn_tag_constructor(loader, _tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnTolerantLoader.add_multi_constructor("!", _cfn_tag_constructor)


def main() -> int:
    failures = []

    for rel_path in PLAIN_YAML_FILES:
        path = REPO_ROOT / rel_path
        try:
            yaml.safe_load(path.read_text())
            print(f"OK: {rel_path}")
        except Exception as exc:  # noqa: BLE001 -- report any parse error
            failures.append((rel_path, exc))

    for rel_path in CFN_TEMPLATE_FILES:
        path = REPO_ROOT / rel_path
        try:
            yaml.load(path.read_text(), Loader=_CfnTolerantLoader)
            print(f"OK: {rel_path} (CloudFormation-tag-tolerant)")
        except Exception as exc:  # noqa: BLE001
            failures.append((rel_path, exc))

    if failures:
        print(f"\nFAILED: {len(failures)} file(s) did not parse:", file=sys.stderr)
        for rel_path, exc in failures:
            print(f"  {rel_path}: {exc}", file=sys.stderr)
        return 1

    print(f"\nOK: all {len(PLAIN_YAML_FILES) + len(CFN_TEMPLATE_FILES)} config files parse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
