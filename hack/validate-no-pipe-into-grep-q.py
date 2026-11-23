#!/usr/bin/env python3
"""No shell line pipes a producer into `grep -q`.

    hack/validate-no-pipe-into-grep-q.py [repo-root]
    hack/validate-no-pipe-into-grep-q.py --self-test

Under `set -o pipefail` (every script and workflow step here runs under it), `producer | grep -q pattern` fails
when grep exits on its first match while the producer still has output to write: the producer takes SIGPIPE
(rc 141) and pipefail makes that the pipeline's status, so a line that WAS found reads as missing. It is a race
on how much the producer writes after the match, so it passes for months and then fails a release: nova-kog's
0.1.9 release refused a chart whose values.schema.json was the third of 24 archive entries (nova-kog #183,
2026-11-18). Forced: `seq 1 3000000 | grep -q '^1$'` under pipefail exits 141; `seq 1 100` exits 0.

The same holds for any reader that exits early: `| head` (2026-11-18, the GHCR push script's
`tar -tzf | head -n1` died 20 of 20 times on a 20,001-entry archive) and `| grep -m N`. All three are refused.

The clear form captures, then matches: `out=$(producer) || fail ...; grep -q pattern <<<"$out"`.

A producer whose output fits in one pipe write (`printf "$v" | grep -q x`) cannot lose the race, but this gate
refuses it too, deliberately: whether a producer's output stays that small is not something a static scan can
know (an `echo "$x"` or `cat file` grows with its input), and the `<<<` form is just as short.
Measured (nova-kog, 2026-11-18): `printf '%s' "$var" | grep -q` with a first-byte match exits 0 at 1, 32, 64 and
128 KB and 141 at 1 MB, so "safe by size" is a threshold, not a property. Two of that repo's credential-leak
checks piped `docker logs` captures this way.

Scans .github/workflows/*.y[a]ml and every *.sh under hack/ and .github/, skipping comment lines. Fails when it
scanned nothing (a gate with no input must fail), and when its own self-test does not catch the planted forms.
"""
import glob
import os
import re
import sys

PIPE_GREP_Q = re.compile(r"\|\s*(?:grep\b(?:\s+-[A-Za-z]+)*?\s+-[A-Za-z]*[qm][A-Za-z0-9]*\b|head\b)")


def offending(text):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        code = re.split(r"\s#\s", line, maxsplit=1)[0]
        if PIPE_GREP_Q.search(code):
            hits.append((n, s))
    return hits


def self_test():
    caught = [
        'tar -tzf x.tgz | grep -qx "a/values.schema.json"',
        'migrate prepare --apply | grep -q "nothing to migrate" || fail x',
        "helm template . | grep -Eq '^kind: Job'",
        "kubectl get pods | grep -i -q running",
        'name="$(tar -tzf "$tgz" | head -n1 | cut -d/ -f1)"',
        "git log | grep -m1 Merge",
        # refused deliberately: safe only while the output fits one pipe write, which a scan cannot know
        'printf "%s" "$version" | grep -q "^v"',
    ]
    passed = [
        'grep -q "nothing to migrate" <<<"$out"',
        '# producer | grep -q x  (a comment)',
        'out=$(producer) || fail; grep -qxF "$want" <<< "$members"',
        "producer | grep -c pattern",
        "head -n1 file.txt",
        "grep -m1 Merge log.txt",
    ]
    bad = [c for c in caught if not offending(c)] + [p for p in passed if offending(p)]
    if bad:
        for b in bad:
            print("FAIL: self-test misjudged: %s" % b)
        return 1
    print("OK: self-test, %d planted pipes into grep -q caught, %d safe forms passed" % (len(caught), len(passed)))
    return 0


def main(root):
    files = sorted(set(glob.glob(os.path.join(root, ".github", "workflows", "*.y*ml"))
                       + glob.glob(os.path.join(root, "hack", "**", "*.sh"), recursive=True)
                       + glob.glob(os.path.join(root, ".github", "**", "*.sh"), recursive=True)))
    if not files:
        print("FAILED MEASUREMENT: no workflow or shell file found under %s, so nothing was checked" % root)
        return 2
    failures = []
    for f in files:
        for n, s in offending(open(f, encoding="utf-8", errors="replace").read()):
            failures.append("%s:%d: %s" % (os.path.relpath(f, root), n, s))
    if failures:
        for x in failures:
            print("FAIL: pipes a producer into an early-exiting reader (grep -q, grep -m, head; under pipefail a found line can read as missing; capture, "
                  "then match with <<<): " + x)
        return 1
    print("OK: %d workflow and shell file(s) scanned; none pipes a producer into grep -q, grep -m or head" % len(files))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    sys.exit(self_test() or main(os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")))
