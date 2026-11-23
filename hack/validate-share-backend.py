#!/usr/bin/env python3
"""manila renders only with a share backend chosen, and never with the generic driver (#426).

    hack/validate-share-backend.py [repo-root]

Upstream's default share backend, generic (GenericShareDriver, driver_handles_share_servers = true), cannot start on
these images: its service-instance helper runs `ip` in init_host, and no airshipit manila image ships iproute2 (measured
on the images 0.3.23, 0.3.73 and 0.3.74 ran, #426). manila-share stayed Running with its service thread dead, and before
0.3.74 nothing probed it. So, as cinder does for its volume backend (#196):

  1. THE PROFILE LEAVES IT OUT. Every umbrella component whose chart has templates/krateo-share-backend.yaml requires
     shared-filesystem, and nothing the umbrella provides is that capability (osh.providedCapabilities, rendered), so
     profile full-strict cannot select manila. The component set must be non-empty, or nothing was measured.
  2. THE CHART REFUSES. Rendered with no values, manila fails, naming #426 and the escape (krateo.shareBackend:
     unmanaged); its values.yaml default is `none`. Unmanaged with upstream's own backends (generic) is refused, naming
     the generic driver. Unmanaged with an empty enabled_share_backends, a named backend with no section, or an unknown
     shareBackend value is refused.
  3. THE OPERATOR PATH RENDERS. hack/share-backend-operator-example.yaml, the values every other gate renders manila
     with (hack/gate_values.py), renders, with a manila-share workload. The same file with its driver swapped for the
     generic one is refused, so the example is not passing by luck.
  4. MUTANTS. The `none` refusal removed must let the default render (caught by 2); the generic-driver refusal removed
     must let unmanaged+generic render (caught by 2). Each must be caught, or this gate measures nothing of the refusal.

Fails when helm or PyYAML is missing and when any render it needs does not happen.
"""
import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
BP = os.path.join(ROOT, "blueprints")
UMBRELLA = os.path.join(BP, "openstack", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
EXAMPLE = os.path.join(ROOT, "hack", "share-backend-operator-example.yaml")
TEMPLATE = os.path.join("templates", "krateo-share-backend.yaml")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

REFUSE_NONE = "manila needs a share backend, and none is configured (#426)"
REFUSE_GENERIC = "is the generic driver"
failures = []
counts = {}


def fail(m):
    failures.append(m)


def count(k):
    counts[k] = counts.get(k, 0) + 1


def stage(chart_dir, tmp, name, mutate=None):
    w = os.path.join(tmp, name)
    shutil.copytree(chart_dir, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in txt:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    if mutate:
        p = os.path.join(w, TEMPLATE)
        t = open(p).read()
        t2 = mutate(t)
        if t2 == t:
            print("FAILED MEASUREMENT: a mutant changed nothing in %s, so it would measure nothing" % TEMPLATE)
            sys.exit(2)
        open(p, "w").write(t2)
    return w


def helm_template(chart, release, values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    try:
        r = subprocess.run(["helm", "template", release, chart, "--namespace", "openstack", "--kube-version", "1.36.0",
                            "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def share_workload(out):
    return [d for d in yaml.safe_load_all(out) if isinstance(d, dict)
            and d.get("kind") in ("Deployment", "StatefulSet") and d["metadata"]["name"].endswith("-share")]


def upstream_unmanaged(cvals):
    """krateo.shareBackend unmanaged with the chart's own (upstream's) backends: generic."""
    return {"krateo": {"shareBackend": "unmanaged"}}


def refusals(chart, label):
    """Part 2 against one staged chart: returns the list of failures (empty when every refusal holds)."""
    out = []
    rc, _, err = helm_template(chart, "manila-gate1234", {})
    if rc == 0:
        out.append("%s: manila renders at its defaults; with no shareBackend it must refuse (#426)" % label)
    elif REFUSE_NONE not in err or "krateo.shareBackend: unmanaged" not in err:
        out.append("%s: manila's refusal at its defaults does not name %r and the escape: %s"
                   % (label, REFUSE_NONE, err.strip()[-300:]))
    rc, _, err = helm_template(chart, "manila-gate1234", {"krateo": {"shareBackend": "unmanaged"}})
    if rc == 0:
        out.append("%s: manila renders with krateo.shareBackend unmanaged and upstream's generic backend; the generic "
                   "driver must be refused (#426)" % label)
    elif REFUSE_GENERIC not in err:
        out.append("%s: unmanaged + generic is refused without naming the generic driver: %s" % (label, err.strip()[-300:]))
    return out


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml"))) or {}
    charts = [c["name"] for c in uv.get("components") or []
              if os.path.isfile(os.path.join(BP, c["name"], "chart", TEMPLATE))]
    if not charts:
        print("FAILED MEASUREMENT: no chart has %s, so nothing was checked" % TEMPLATE)
        sys.exit(2)
    # 1. the profile leaves it out
    for c in uv["components"]:
        if c["name"] in charts:
            if "shared-filesystem" not in (c.get("requires") or []):
                fail("1: %s refuses without a share backend but its umbrella entry does not require shared-filesystem, "
                     "so profile full-strict would select it and render its refusal" % c["name"])
            count("1")
    helpers = open(os.path.join(UMBRELLA, "templates", "_helpers.tpl")).read()
    m = re.search(r'define "osh.providedCapabilities".*?\{\{- end -\}\}\s*$', helpers, re.S | re.M)
    body = m.group(0) if m else ""
    if not body:
        fail("1: osh.providedCapabilities not found in the umbrella's _helpers.tpl")
    elif "shared-filesystem" in body:
        fail("1: osh.providedCapabilities names shared-filesystem: update this gate with what provides it (#308) and "
             "render the selection with and without it")
    count("1")
    with tempfile.TemporaryDirectory() as tmp:
        for n in charts:
            cdir = os.path.join(BP, n, "chart")
            cvals = yaml.safe_load(open(os.path.join(cdir, "values.yaml"))) or {}
            if ((cvals.get("krateo") or {}).get("shareBackend")) != "none":
                fail("2: %s's values.yaml krateo.shareBackend is %r, want none" % (n, (cvals.get("krateo") or {}).get("shareBackend")))
            c = stage(cdir, tmp, n)
            # 2. the chart refuses
            for f in refusals(c, "2"):
                fail(f)
            count("2")
            for vals, want, what in (
                    ({"krateo": {"shareBackend": "unmanaged"}, "conf": {"manila": {"DEFAULT": {"enabled_share_backends": ""}}}},
                     "enabled_share_backends is empty", "an empty enabled_share_backends"),
                    ({"krateo": {"shareBackend": "unmanaged"}, "conf": {"manila": {"DEFAULT": {"enabled_share_backends": "nosuch"}}}},
                     "has no share_driver", "a backend with no section"),
                    # helm's schema check (values.schema.json's enum) refuses an unknown value before the template's own
                    # last branch runs; either refusal names the allowed values.
                    ({"krateo": {"shareBackend": "lvm"}}, ("is not a value this chart knows",
                                                           "value must be one of 'none', 'unmanaged'"),
                     "an unknown shareBackend")):
                rc, _, err = helm_template(c, "manila-gate1234", vals)
                wants = want if isinstance(want, tuple) else (want,)
                if rc == 0:
                    fail("2: %s renders with %s; it must refuse" % (n, what))
                elif not any(w in err for w in wants):
                    fail("2: %s refuses %s without %r: %s" % (n, what, want, err.strip()[-300:]))
                count("2")
            # 3. the operator path renders
            ex = yaml.safe_load(open(EXAMPLE)) or {}
            rc, out, err = helm_template(c, "manila-gate1234", ex)
            if rc != 0:
                fail("3: %s does not render with %s: %s" % (n, os.path.relpath(EXAMPLE, ROOT), err.strip()[-300:]))
            elif not share_workload(out):
                fail("3: %s with the operator example renders no manila-share workload" % n)
            count("3")
            gen = copy.deepcopy(ex)
            for name, sec in ((gen.get("conf") or {}).get("manila") or {}).items():
                if isinstance(sec, dict) and "share_driver" in sec:
                    sec["share_driver"] = "manila.share.drivers.generic.GenericShareDriver"
            rc, _, err = helm_template(c, "manila-gate1234", gen)
            if rc == 0 or REFUSE_GENERIC not in err:
                fail("3: the operator example with its driver swapped for the generic one is not refused (rc %d): %s"
                     % (rc, err.strip()[-200:]))
            count("3")
            # 4. mutants
            mutants = (
                ("the none refusal removed", lambda t: t.replace('{{- if eq $backend "none" -}}', '{{- if eq $backend "never" -}}')),
                ("the generic-driver refusal removed", lambda t: t.replace('contains "GenericShareDriver"', 'contains "NoSuchDriver"')),
            )
            for i, (what, f) in enumerate(mutants):
                wm = stage(cdir, tmp, "%s-mut%d" % (n, i), mutate=f)
                if not refusals(wm, "mutant"):
                    fail("4: mutant '%s' passes part 2: the refusal is not what part 2 measures" % what)
                count("4")
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: %d chart(s) (%s) refuse without a share backend and refuse the generic driver; they require "
          "shared-filesystem, which nothing provides, so full-strict leaves them out; the operator example renders and "
          "its generic twin is refused; %d mutant(s) killed (checks: %s)"
          % (len(charts), ", ".join(charts), counts.get("4", 0), ", ".join("%s=%d" % kv for kv in sorted(counts.items()))))


main()
