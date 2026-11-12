#!/usr/bin/env python3
"""Every keystone-oidc image this repo points a reader at is the same OpenStack series as the
keystone chart's own images.

Why this exists (#229). OIDC federation needs mod_auth_openidc in the keystone-api container, and
the only image that ships it is ghcr.io/krateo-blueprints/keystone-oidc. Since 0.3.46 every keystone
image in the chart -- db-sync and bootstrap included -- is the 2026.1 series, but the schema's
example and the docs still named keystone-oidc:2025.1-ubuntu_jammy. Following them ran keystone-api
27.0.1 against a schema that keystone-manage db_sync from 29.1.1 had migrated, two releases apart.
Nothing refused it.

One source: images.tags.keystone_api in blueprints/keystone/chart/values.yaml, whose tag is
<series>-ubuntu_<distro> (e.g. 2026.1-ubuntu_noble). Every `keystone-oidc:<tag>` reference in the
chart, its schema, the docs, the README and the examples must carry that exact tag. A line that
records a MEASUREMENT ("Measured", "measured") is history and is left alone.

It fails, rather than passing, when it finds no keystone-oidc reference at all: the docs must name
the image a reader needs, and a scan that matches nothing is a broken gate.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
VALUES = ROOT / "blueprints/keystone/chart/values.yaml"
SCAN = ["README.md", "docs", "examples", "blueprints/keystone/chart"]
REF = re.compile(r"keystone-oidc:([0-9]{4}\.[12]-ubuntu_[a-z]+)")
MEASURED = re.compile(r"\bmeasured\b", re.I)


def main():
    tag = ((yaml.safe_load(VALUES.read_text()) or {}).get("images") or {}).get("tags", {}).get("keystone_api", "")
    m = re.search(r":([0-9]{4}\.[12]-ubuntu_[a-z]+)$", tag)
    if not m:
        print(f"FAIL: images.tags.keystone_api is {tag!r}; expected <repo>:<series>-ubuntu_<distro>")
        return 1
    want = m.group(1)
    files = []
    for s in SCAN:
        p = ROOT / s
        files += [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file() and f.suffix in (".md", ".yaml", ".yml", ".json", ".tpl")]
    refs, problems = 0, []
    for f in sorted(set(files)):
        for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if MEASURED.search(line):
                continue
            for mm in REF.finditer(line):
                refs += 1
                if mm.group(1) != want:
                    problems.append(f"{f.relative_to(ROOT)}:{i}: names keystone-oidc:{mm.group(1)}, but the chart's "
                                    f"keystone images are {want}")
    if refs == 0:
        print("FAIL: no keystone-oidc reference found in the chart, schema, docs, README or examples: a "
              "reader setting up OIDC is not told which image ships mod_auth_openidc")
        return 1
    if problems:
        print(f"FAIL: {len(problems)} keystone-oidc reference(s) on another series than the chart ({want}):")
        for p in problems:
            print("  - " + p)
        return 1
    print(f"OK: {refs} keystone-oidc reference(s), all {want}, the series of the chart's keystone images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
