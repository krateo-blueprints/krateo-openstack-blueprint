#!/usr/bin/env python3
"""The umbrella's component DAG must be closed, acyclic, and backed by real charts.

Why this is worth a gate even though the DAG is correct today. The orchestrator emits a
component's Composition only once every name in its `deps` reports Ready. A dep naming a
component that does not exist is never Ready, so the dependent is never emitted -- and nothing
reports an error. The install simply stops partway with fewer components than the profile lists,
which is **also exactly what a healthy install looks like for its first minutes**. The gate
emitting in levels is normal; the gate waiting forever on a typo is not; and from the outside
they are the same picture.

A cycle has the same shape: two components each waiting for the other, forever, silently.

Also checks `kind`, because the gate matches a component's CRD by Kind. core-provider derives
the served kind from the chart name, so `kind` must be the capitalised name -- `MariaDB` instead
of `Mariadb` means the gate looks for a CRD nobody serves and waits forever, again with no error.

And renders every component chart. 29 of the 34 are compute-tier and have never been installed
anywhere: libvirt needs /dev/kvm and openvswitch needs kernel modules, so kind cannot run them.
Rendering is not installing, but it does catch the class that broke 0.3.10 -- a chart that does
not template -- without needing hardware.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
UMBRELLA = ROOT / "blueprints/openstack/chart/values.yaml"
TOOLKIT = ROOT / "charts/helm-toolkit"
SKIP_RENDER = {"openstack"}  # the umbrella itself needs lookups; covered by its own gates

try:
    import yaml
except ImportError:
    print("SKIP: PyYAML unavailable")
    sys.exit(0)

if not UMBRELLA.is_file():
    print(f"FAILED MEASUREMENT: no umbrella values at {UMBRELLA}")
    sys.exit(2)

comps = {c["name"]: c for c in (yaml.safe_load(UMBRELLA.read_text()).get("components") or [])}
if not comps:
    print("FAILED MEASUREMENT: the umbrella declares no components")
    sys.exit(2)

names = set(comps)
problems = []

# 1. closure
for n, c in sorted(comps.items()):
    for d in c.get("deps") or []:
        if d not in names:
            problems.append(f"{n}: dep {d!r} is not a declared component -- it will never be "
                            f"Ready, so {n} is never emitted and nothing reports it")

# 2. acyclicity
WHITE, GREY, BLACK = 0, 1, 2
state = {n: WHITE for n in names}
def visit(n, stack):
    if state[n] == GREY:
        problems.append("cycle: " + " -> ".join(stack + [n]) +
                        " -- each waits for the next, forever, with no error")
        return
    if state[n] == BLACK:
        return
    state[n] = GREY
    for d in comps[n].get("deps") or []:
        if d in comps:
            visit(d, stack + [n])
    state[n] = BLACK
for n in sorted(names):
    visit(n, [])

# 3. kind matches the name core-provider will serve
for n, c in sorted(comps.items()):
    want = n.capitalize()
    got = c.get("kind")
    if got != want:
        problems.append(f"{n}: kind is {got!r}, but core-provider serves {want!r} -- the gate "
                        f"would match a CRD nobody serves and wait forever")

# 4. a chart exists, and renders
rendered = 0
for n in sorted(names):
    chart = ROOT / "blueprints" / n / "chart"
    if not (chart / "Chart.yaml").is_file():
        problems.append(f"{n}: no chart at blueprints/{n}/chart")
        continue
    if n in SKIP_RENDER:
        continue
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "chart"
        shutil.copytree(chart, work)
        cy = work / "Chart.yaml"
        cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
        if TOOLKIT.is_dir():
            (work / "charts").mkdir(exist_ok=True)
            shutil.copytree(TOOLKIT, work / "charts/helm-toolkit", dirs_exist_ok=True)
        out = subprocess.run(
            # Without --kube-version a kubeVersion-constrained chart renders nothing, and a
            # gate that accepts an empty render accepts anything.
            ["helm", "template", "gate", str(work), "--kube-version", "1.36.0"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            problems.append(f"{n}: helm template failed -- {out.stderr.strip().splitlines()[-1][:160] if out.stderr.strip() else 'no stderr'}")
        elif not out.stdout.strip():
            problems.append(f"{n}: rendered 0 objects")
        else:
            rendered += 1

if problems:
    print(f"FAIL: {len(problems)} problem(s) in the component DAG")
    for p in problems:
        print(f"  {p}")
    sys.exit(1)

tiers = {}
for c in comps.values():
    tiers[c.get("tier", "?")] = tiers.get(c.get("tier", "?"), 0) + 1
print(f"OK: {len(names)} components, DAG closed and acyclic, {rendered} charts render "
      f"({', '.join(f'{k}={v}' for k, v in sorted(tiers.items()))})")
