#!/usr/bin/env python3
"""Report which images actually move when the OpenStack series changes.

Why this exists. On 2026-10-31 the image series moved 2025.1 -> 2025.2 -> 2026.1, and each move
was verified with an identity-core install ending in a real Keystone token. I then wrote that
MariaDB's unchanged start-to-ready time showed the move "costs nothing measurable". That was
wrong, and wrong in a way that reads as evidence:

    mariadb    quay.io/airshipit/mariadb:ubuntu_noble-<date>   date-tagged, does NOT move
    memcached  quay.io/airshipit/memcached:1.6.32              version-tagged, does NOT move
    keystone   quay.io/airshipit/keystone:<series>-ubuntu_...  series-tagged, moves

MariaDB runs the identical image on every series, so its timing could not change and was never
evidence about anything. The identity-core install exercises the series through two images --
keystone and openstack-client -- not through the identity plane.

The trap generalises: a series bump edits hundreds of refs, an install passes, and the install
touched a handful of the images that moved. This prints the real scope so a bump's claim can be
stated accurately instead of optimistically.

Usage:
    hack/series-scope.py [repo-root] [--component NAME]

Exit status is 0 unless the tree is unreadable; this is a report, not a gate.
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

IMG = re.compile(
    r"(?P<repo>(?:quay\.io|docker\.io)/[a-z0-9][a-z0-9._/-]*):(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]*)"
)
SERIES = re.compile(r"^(?P<series>\d{4}\.\d)-(?P<base>ubuntu_[a-z]+)$")

args = [a for a in sys.argv[1:] if not a.startswith("--")]
only = None
if "--component" in sys.argv:
    i = sys.argv.index("--component")
    if i + 1 < len(sys.argv):
        only = sys.argv[i + 1]

root = Path(args[0] if args else ".")
values = sorted(root.glob("blueprints/*/chart/values.yaml"))
if not values:
    print(f"FAILED MEASUREMENT: no blueprints/*/chart/values.yaml under {root}")
    sys.exit(2)

moves = defaultdict(set)      # repo -> {series seen}
static = defaultdict(set)     # repo -> {tags seen}
per_component = defaultdict(lambda: [0, 0])  # component -> [moving, static]

for path in values:
    component = path.parts[-3]
    if only and component != only:
        continue
    for m in IMG.finditer(path.read_text(errors="replace")):
        repo, tag = m.group("repo"), m.group("tag")
        s = SERIES.match(tag)
        if s:
            moves[repo].add(s.group("series") + "-" + s.group("base"))
            per_component[component][0] += 1
        else:
            static[repo].add(tag)
            per_component[component][1] += 1

print("IMAGES THAT MOVE WITH THE SERIES")
print("  (a series bump changes these, so a test must exercise them to say anything)")
for repo in sorted(moves):
    print(f"    {repo:52s} {', '.join(sorted(moves[repo]))}")
print(f"  {len(moves)} repositories")

print()
print("IMAGES THAT DO NOT MOVE")
print("  (identical before and after a bump: their behaviour is not evidence about the series)")
for repo in sorted(static):
    tags = sorted(static[repo])
    shown = ", ".join(tags[:3]) + (f", +{len(tags) - 3} more" if len(tags) > 3 else "")
    print(f"    {repo:52s} {shown}")
print(f"  {len(static)} repositories")

print()
print("BY COMPONENT  (moving / static refs)")
for component in sorted(per_component):
    mv, st = per_component[component]
    flag = "" if mv else "   <- nothing here moves with the series"
    print(f"    {component:16s} {mv:4d} / {st:4d}{flag}")

ident = ("mariadb", "memcached", "keystone")
im = sum(per_component[c][0] for c in ident if c in per_component)
ist = sum(per_component[c][1] for c in ident if c in per_component)
if im or ist:
    print()
    print("WHAT AN IDENTITY-CORE INSTALL ACTUALLY TESTS ABOUT A SERIES MOVE")
    print(f"    mariadb + memcached + keystone: {im} moving refs, {ist} static")
    print("    A passing identity-core install is evidence about the moving refs above and")
    print("    nothing else. Glance, horizon and the compute tier are not exercised at all.")
