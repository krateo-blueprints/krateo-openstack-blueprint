#!/usr/bin/env python3
"""YAML this repo authors uses block style; a non-empty flow collection is a failure.

`{a: 1}` and `[a, b]` parse to the same data as their block forms, so this is about the source
and not the render. The reason it is enforced rather than preferred: the umbrella's component
DAG was 11 aligned inline entries and 24 block ones, and adding `requiresProvisioning` to an
inline entry meant either converting it or extending a 110-column line that already had its keys
padded into columns. A mixed file makes every edit a style decision, and the padding makes the
diff of a one-key change touch the whole row.

TWO THINGS THIS DELIBERATELY ALLOWS:

  * EMPTY collections. `emptyDir: {}`, `deps: []`, `policy: {}` have no block form at all --
    `emptyDir:` alone is the null scalar, which is not the same value and which Kubernetes does
    not accept in its place. A rule that banned them would be unsatisfiable.
  * VENDORED charts. Everything under blueprints/<component>/chart for the 34 components comes
    from upstream openstack-helm; 991 of the 1014 flow collections in this repo are upstream
    `emptyDir: {}` lines. Restyling vendored files would make every future rebase a conflict,
    and they are not ours to have an opinion about.

HOW IT DECIDES, and why not by regex: a line scan cannot tell `kinds: ["Openstack"]` (structure)
from a sentence inside a block scalar that happens to contain `enabled: [heat]` (text). The
kagent agent prompt contains exactly that, and an earlier regex version of this check reported
it. So the YAML event stream is the instrument: flow style is reported on MappingStartEvent and
SequenceStartEvent, which are emitted for structure only -- a bracket inside a string arrives as
a ScalarEvent and is invisible to this check, correctly.

Go-templated files cannot be parsed that way, so they get a line scan that skips any line
containing `{{` or `}}`. The parser is tried on EVERY file and the scan is the fallback for the
ones that genuinely do not parse -- not for the ones that merely contain `{{`. Routing on
templated-ness instead sent the kagent agent prompt (a valid YAML file whose block scalar quotes
`{{ }}` and `enabled: [heat]` as examples) down the text path, where the line scan reported its
prose. Parseability is the property that decides which instrument can read a file; containing a
brace is not.

Every file is reported as parsed or scanned, and a count of zero is a measurement failure rather
than a silent pass: an unread file is an UNCHECKED file, and the whole population here is files
this repo authors, so there is no legitimate case where nothing could be read.
"""
import os
import pathlib
import re
import sys

try:
    import yaml
except ImportError:
    print("MEASUREMENT FAILED: PyYAML is not importable, so no file could be read. "
          "Every check below would report zero violations for the wrong reason.")
    sys.exit(1)

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()

# Files this repo authors. blueprints/<component>/chart is vendored upstream and excluded by
# listing the umbrella explicitly rather than by excluding the others -- a new component added
# tomorrow is vendored too, and should not silently enter this population.
OURS = (
    "blueprints/openstack",
    "examples",
    "hack",
    ".github",
    "kagent",
    "tools",
)

problems = []
parsed = scanned = 0
violations = 0


def report(relpath, line, text, kind):
    global violations
    violations += 1
    problems.append(
        f"{relpath}:{line} uses non-empty flow style for a {kind}: {text.strip()[:88]}")


def flow_collections(path):
    """Every flow-style collection in the file, as (line, column, kind, is_empty).

    Keyed by line AND column, deliberately. Keying the empty-collection allowance by line alone
    made this gate pass the exact input it was written for: in

        - {name: mariadb, kind: Mariadb, deps: [], tier: identity}

    the `deps: []` is an empty flow sequence on the same line as the non-empty flow mapping that
    wraps it, so allowing "line 34" allowed both and the entry reported clean. One inline entry
    restored in a break test passed silently. A column distinguishes them because the two
    collections start at different offsets, which is the only thing that was ever true of them.
    """
    with open(path, errors="replace") as fh:
        events = list(yaml.parse(fh))
    out = []
    for i, ev in enumerate(events):
        if getattr(ev, "flow_style", None) is not True:
            continue
        nxt = events[i + 1] if i + 1 < len(events) else None
        if isinstance(ev, yaml.MappingStartEvent):
            kind, empty = "mapping", isinstance(nxt, yaml.MappingEndEvent)
        elif isinstance(ev, yaml.SequenceStartEvent):
            kind, empty = "sequence", isinstance(nxt, yaml.SequenceEndEvent)
        else:
            continue
        out.append((ev.start_mark.line + 1, ev.start_mark.column, kind, empty))
    return out


# A bracket that opens a collection, with at least one non-space character after it. Used only
# for Go-templated files, where the parser cannot be.
FLOW_TEXT = re.compile(r":\s*(\{\s*[^\s}]|\[\s*[^\s\]])|^\s*-\s*\{\s*[^\s}]")


def check_text(path, relpath):
    for i, line in enumerate(open(path, errors="replace"), 1):
        if "{{" in line or "}}" in line:
            continue
        body = line.split("#")[0]
        if FLOW_TEXT.search(body):
            report(relpath, i, line, "collection")


for path in sorted(ROOT.rglob("*.y*ml")):
    rel = path.relative_to(ROOT)
    relpath = str(rel)
    if not relpath.startswith(OURS):
        continue
    if "helm-toolkit" in relpath or "/charts/" in relpath:
        continue
    text = path.read_text(errors="replace")
    # Try the parser on everything. It is the only instrument that can tell structure from text,
    # so the text scan is a fallback for files it CANNOT read, never for files that merely look
    # templated.
    try:
        found = flow_collections(path)
        lines = text.splitlines()
        for line, _col, kind, empty in found:
            if empty:
                continue   # no block form exists; see the module docstring
            src = lines[line - 1] if line - 1 < len(lines) else ""
            report(relpath, line, src, kind)
        parsed += 1
        continue
    except yaml.YAMLError:
        pass
    check_text(path, relpath)
    scanned += 1

if parsed + scanned == 0:
    print(f"MEASUREMENT FAILED: no file under {OURS} was read, so this gate found no "
          "violations because it looked at nothing. Either the paths moved or the root is "
          f"wrong (root={ROOT}).")
    sys.exit(1)

if problems:
    print(f"FAIL: {len(problems)} problem(s) ({parsed} parsed, {scanned} scanned)")
    for p in problems:
        print("  " + p)
    print("\n  Block style for all of them. Empty `{}` and `[]` are allowed and need no change; "
          "everything under blueprints/<component>/chart is vendored upstream and is not "
          "checked.")
    sys.exit(1)

print(f"OK: no non-empty flow collections in {parsed + scanned} repo-authored YAML files "
      f"({parsed} read through the parser, {scanned} line-scanned because Go-templated); "
      "empty collections and vendored upstream charts are out of scope by design")
