#!/usr/bin/env python3
"""Check that every *.md#anchor link in this repo resolves to a heading
that actually exists in the target file.

Dependency-free (stdlib only). Computes GitHub's heading-slug algorithm:
lowercase, strip backticks/link syntax, drop every character that isn't
alphanumeric/space/hyphen/underscore, then replace each remaining space
with a hyphen (consecutive spaces -> consecutive hyphens, since each
space is replaced independently rather than collapsed first).

Exits 0 if every link resolves, 1 otherwise (with the specific failures
printed to stderr).
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

LINK_RE = re.compile(r'\]\(([^)\s]+\.md)#([^)\s]+)\)')
HEADING_RE = re.compile(r'^(#{1,6})\s+(.+?)\s*$')


def slugify(heading_text: str) -> str:
    text = heading_text.strip()
    # Strip inline code backticks but keep their contents.
    text = text.replace('`', '')
    # Strip markdown link syntax [text](url) -> text
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = text.lower()
    # Drop everything not alnum/space/hyphen/underscore.
    text = re.sub(r'[^a-z0-9 _-]', '', text)
    # Each remaining space becomes a hyphen (no collapsing of runs).
    text = text.replace(' ', '-')
    return text


def headings_in(md_path: Path) -> set[str]:
    slugs = set()
    seen_counts: dict[str, int] = {}
    for line in md_path.read_text().splitlines():
        m = HEADING_RE.match(line)
        if not m:
            continue
        base = slugify(m.group(2))
        count = seen_counts.get(base, 0)
        seen_counts[base] = count + 1
        slug = base if count == 0 else f"{base}-{count}"
        slugs.add(slug)
    return slugs


def links_in(md_path: Path):
    text = md_path.read_text()
    for m in LINK_RE.finditer(text):
        yield m.group(1), m.group(2)


def main() -> int:
    md_files = sorted(REPO_ROOT.rglob('*.md'))
    heading_cache: dict[Path, set[str]] = {}
    failures = []
    total_links = 0

    for md_file in md_files:
        for target_rel, anchor in links_in(md_file):
            total_links += 1
            target_path = (md_file.parent / target_rel).resolve()
            if target_path not in heading_cache:
                if not target_path.exists():
                    failures.append((md_file, target_rel, anchor, 'target file does not exist'))
                    heading_cache[target_path] = set()
                    continue
                heading_cache[target_path] = headings_in(target_path)
            if anchor not in heading_cache[target_path]:
                failures.append((md_file, target_rel, anchor, 'no matching heading'))

    rel = lambda p: p.relative_to(REPO_ROOT)
    if failures:
        print(f"FAILED: {len(failures)} of {total_links} markdown links do not resolve:\n", file=sys.stderr)
        for md_file, target_rel, anchor, reason in failures:
            print(f"  {rel(md_file)} -> {target_rel}#{anchor}  ({reason})", file=sys.stderr)
        return 1

    print(f"OK: all {total_links} markdown links resolve.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
