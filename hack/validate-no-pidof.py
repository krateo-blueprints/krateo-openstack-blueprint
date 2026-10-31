#!/usr/bin/env python3
"""Refuse `pidof` in anything that runs inside a container.

Why this gate exists: /usr/bin/pidof in the OpenStack-Helm images is sysvinit's killall5,
which identifies a process by its EXECUTABLE. Under an emulating container runtime -- Rosetta
or qemu, i.e. every amd64 image on an arm64 host -- /proc/<pid>/exe resolves to the emulator,
so `pidof <program>` matches nothing while the program is running normally, and `pidof
<emulator>` matches everything. A probe built on it fails forever, the kubelet kills a healthy
process, and the pod never becomes Ready. Measured, not theorised:

    readlink /proc/176/exe  ->  /run/rosetta/rosetta
    pidof mariadbd          ->  (empty)      # mariadbd WAS running, PID 176
    pidof rosetta           ->  632 626 176 34
    pgrep -x mariadbd       ->  176

pgrep matches /proc/<pid>/comm, which the kernel sets correctly under emulation and natively,
so it is strictly more reliable. Use `pgrep -x <name>` for a program and `pgrep -f <path>` for
a script. See issue #96.

This gate exists because the fix is one word and the symptom is a pod that hangs at 0/1 for
ten minutes and then restarts cleanly -- which reads as a slow or loaded environment, not as a
broken probe. It cost most of a day to attribute correctly, twice to the wrong cause.

Comments are ignored: explaining why pidof is wrong must stay allowed.
"""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
# Vendored upstream helm-toolkit is excluded: we do not edit it, and a finding there is an
# upstream report, not a local fix. Anything we DO ship is in scope.
SKIP = ("/helm-toolkit/", "/charts/helm-toolkit/")
# A shell comment (#) or a Go-template comment line. Not a YAML-value '#', which cannot occur
# before a command in the positions we scan.
COMMENT = re.compile(r"^\s*(#|\{\{-?\s*/\*)")
PIDOF = re.compile(r"(?<![\w-])pidof(?![\w-])")

findings = []
for path in sorted(ROOT.glob("blueprints/**/*")):
    if not path.is_file() or any(s in str(path) for s in SKIP):
        continue
    if path.suffix not in (".yaml", ".yml", ".tpl", ".sh", ".py"):
        continue
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        continue
    for n, line in enumerate(lines, 1):
        if COMMENT.match(line):
            continue
        if PIDOF.search(line):
            findings.append((path.relative_to(ROOT), n, line.strip()[:96]))

if findings:
    print("FAIL: pidof cannot identify a process under an emulating runtime (issue #96).")
    print("      Use `pgrep -x <program>`, or `pgrep -f <path>` for a script.\n")
    for rel, n, text in findings:
        print(f"  {rel}:{n}\n      {text}")
    sys.exit(1)

scanned = sum(
    1
    for p in ROOT.glob("blueprints/**/*")
    if p.is_file()
    and p.suffix in (".yaml", ".yml", ".tpl", ".sh", ".py")
    and not any(s in str(p) for s in SKIP)
)
if scanned == 0:
    # A gate that scans nothing passes everything. Fail loudly rather than report success.
    print(f"FAILED MEASUREMENT: scanned 0 files under {ROOT}/blueprints -- wrong path?")
    sys.exit(2)
print(f"OK: no pidof in {scanned} shipped files (pgrep is used instead)")
