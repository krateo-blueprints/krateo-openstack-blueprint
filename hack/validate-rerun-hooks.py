#!/usr/bin/env python3
"""A component whose hooks never ran can be made to run them, by a value, and only that component moves (issue #297).

    hack/validate-rerun-hooks.py [repo-root]

core-provider (#563, fixed in 2.13.108) can leave a first install `deployed` with its post-install hooks never created;
the release then reads Ready=False/HooksMayNotHaveRun. A real helm upgrade runs the hooks again, and core-provider runs
one only on a chart-version change, a drift heal, or a change of the component's canonical values: the JSON of its
Composition spec. A label, an annotation, or a spec key the CRD does not declare (pruned) changes nothing. So the
umbrella value rerunHooks.<component> passes a token to that component alone, as its krateo.rerunHooksToken, which
every component schema declares and no template renders.

  1. SCHEMA. The umbrella's rerunHooks accepts exactly the component names, each a non-empty string: an unknown name, and
     an empty token, are refused by helm's schema validation; a known name with a token renders.
  2. ONLY THAT COMPONENT. The lines of compositions.yaml that build a component's spec are rendered for every component
     with and without rerunHooks.cyborg: cyborg's spec gains exactly krateo.rerunHooksToken, and every other component's
     spec is byte-identical.
  3. DECLARED EVERYWHERE. Every component chart's values.schema.json declares krateo.rerunHooksToken as a string, and its
     values.yaml defaults it to "", so the Composition CRD does not prune it. The token is rendered nowhere: cyborg's
     chart renders byte-identical with and without it.
  4. EVERY INSTALL HOOK IS AN UPGRADE HOOK. Every helm.sh/hook annotation in every chart's templates and values (and
     helm-toolkit's) that names post-install also names post-upgrade or pre-upgrade, so an upgrade re-runs what the
     install would have run. A templated hook value this check cannot read fails. Static: no chart is rendered.

Fails when helm or PyYAML is missing, and when any part finds nothing to check.
"""
import json
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
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
TOKEN = "2026-11-18T10:00:00Z"
PROBE_COMPONENT = "cyborg"
failures = []
counts = {}


def fail(m):
    failures.append(m)


def stage(chart_dir, tmp, name):
    w = os.path.join(tmp, name)
    shutil.copytree(chart_dir, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in txt:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
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


def deep_merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            deep_merge(a[k], v)
        else:
            a[k] = v
    return a


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    names = [c["name"] for c in uv.get("components") or []]
    if not names:
        print("FAILED MEASUREMENT: the umbrella lists no component")
        sys.exit(2)
    with tempfile.TemporaryDirectory() as tmp:
        # 1. schema
        u = stage(UMBRELLA, tmp, "umbrella")
        for label, vals, ok in (("rerunHooks.%s set" % PROBE_COMPONENT, {"rerunHooks": {PROBE_COMPONENT: TOKEN}}, True),
                                ("rerunHooks naming an unknown component", {"rerunHooks": {"no-such-component": TOKEN}},
                                 False),
                                ("an empty rerunHooks.%s" % PROBE_COMPONENT, {"rerunHooks": {PROBE_COMPONENT: ""}}, False)):
            rc, out, err = helm_template(u, "openstack-gate", vals)
            if ok and rc != 0:
                fail("1: %s: the umbrella does not render: %s" % (label, err.strip()[:200]))
            elif not ok and rc == 0:
                fail("1: %s: the umbrella renders; its schema must refuse it" % label)
            counts["1"] = counts.get("1", 0) + 1
        props = ((json.load(open(os.path.join(UMBRELLA, "values.schema.json")))["properties"].get("rerunHooks") or {})
                 .get("properties") or {})
        if sorted(props) != sorted(names):
            fail("1: the umbrella schema's rerunHooks names %s, the umbrella's components are %s" % (
                sorted(set(props) ^ set(names)) or sorted(props), "the same set" if False else "different"))
        # 2. only that component's spec moves
        comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
        start = comp.find("{{- $ov := ")
        mark = comp.find("$.Values.rerunHooks", start)
        end = comp.find("{{- end }}", mark) if mark >= 0 else -1
        if start < 0 or end < 0:
            fail("2: compositions.yaml builds no spec from $.Values.rerunHooks (the span from `{{- $ov := ` through the "
                 "rerunHooks merge), so no component receives a token")
        else:
            span = comp[start:end + len("{{- end }}")]
            open(os.path.join(u, "templates", "zz-gate-rerun-probe.yaml"), "w").write(
                '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
                '{{- $dr := include "osh.deregistration" (list $ (splitList " " (include "osh.selected" (list $))) (list)) | fromYaml }}\n{{- range $c := .Values.components }}\n' + span + '\n'
                '# RRSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n')
            specs = {}
            for key, vals in (("without", {}), ("with", {"rerunHooks": {PROBE_COMPONENT: TOKEN}})):
                rc, out, err = helm_template(u, "openstack-gate", vals)
                if rc != 0:
                    fail("2: the umbrella probe does not render %s a token: %s" % (key, err.strip()[:200]))
                    continue
                specs[key] = {l.split(" ", 3)[2]: l.split(" ", 3)[3] for l in out.splitlines() if l.startswith("# RRSPEC ")}
            if len(specs) == 2:
                if sorted(specs["with"]) != sorted(names):
                    fail("2: the probe saw %d component spec(s), the umbrella lists %d" % (len(specs["with"]), len(names)))
                for n in names:
                    a, b = specs["without"].get(n), specs["with"].get(n)
                    if n == PROBE_COMPONENT:
                        want = deep_merge(json.loads(a or "{}"), {"krateo": {"rerunHooksToken": TOKEN}})
                        if json.loads(b or "{}") != want:
                            fail("2: with rerunHooks.%s the %s spec is not its spec plus krateo.rerunHooksToken"
                                 % (PROBE_COMPONENT, n))
                    elif a != b:
                        fail("2: rerunHooks.%s changes %s's spec too: a token must move its component alone"
                             % (PROBE_COMPONENT, n))
                    counts["2"] = counts.get("2", 0) + 1
        # 3. declared everywhere, rendered nowhere
        for n in names:
            sf = os.path.join(BP, n, "chart", "values.schema.json")
            vf = os.path.join(BP, n, "chart", "values.yaml")
            try:
                k = ((json.load(open(sf))["properties"].get("krateo") or {}).get("properties") or {}).get("rerunHooksToken")
            except (OSError, ValueError):
                k = None
            if not k or k.get("type") != "string":
                fail("3: %s's values.schema.json declares no string krateo.rerunHooksToken: the Composition CRD prunes "
                     "rerunHooks.%s, and the hooks never re-run" % (n, n))
            default = ((yaml.safe_load(open(vf)) or {}).get("krateo") or {}).get("rerunHooksToken")
            if default != "":
                fail("3: %s's values.yaml defaults krateo.rerunHooksToken to %r, want \"\"" % (n, default))
            counts["3"] = counts.get("3", 0) + 1
        w = stage(os.path.join(BP, PROBE_COMPONENT, "chart"), tmp, PROBE_COMPONENT)
        r0 = helm_template(w, PROBE_COMPONENT + "-gate1234", {})
        r1 = helm_template(w, PROBE_COMPONENT + "-gate1234", {"krateo": {"rerunHooksToken": TOKEN}})
        if r0[0] != 0 or r1[0] != 0:
            fail("3: %s does not render with and without a token: %s" % (PROBE_COMPONENT, (r0[2] or r1[2]).strip()[:200]))
        elif r0[1] != r1[1] or TOKEN in r1[1]:
            fail("3: %s renders the token, or renders differently with it: it must change the values and nothing else"
                 % PROBE_COMPONENT)
    # 4. every install hook is an upgrade hook (static)
    hook = re.compile(r"""["']?helm\.sh/hook["']?\s*:\s*(.+)$""")
    files = []
    for base in [os.path.join(BP, d, "chart") for d in sorted(os.listdir(BP))] + [TOOLKIT]:
        for dirpath, _, fs in os.walk(base):
            if os.sep + "charts" + os.sep in dirpath + os.sep and base != TOOLKIT:
                continue
            files += [os.path.join(dirpath, f) for f in fs if f.endswith((".yaml", ".tpl", ".yml"))]
    seen = 0
    for f in files:
        for i, line in enumerate(open(f, errors="replace"), 1):
            m = hook.search(line)
            if not m:
                continue
            value = m.group(1).strip().strip("\"'")
            where = "%s:%d" % (os.path.relpath(f, ROOT), i)
            if "{{" in value:
                fail("4: %s sets helm.sh/hook from a template (%s): this check cannot read which hooks it names"
                     % (where, value[:80]))
                continue
            seen += 1
            phases = {p.strip() for p in value.split(",")}
            if "post-install" in phases and not phases & {"post-upgrade", "pre-upgrade"}:
                fail("4: %s is a post-install hook that no upgrade runs (%s): a hook a first install missed could then "
                     "only be re-run by deleting the component (#297)" % (where, value))
    counts["4"] = seen
    if not seen:
        fail("4: no helm.sh/hook annotation was found in any chart; the check had no input")
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: rerunHooks: %d schema case(s); %d component spec(s) compared, only %s's moves; %d component schema(s) "
          "declare krateo.rerunHooksToken; %d hook annotation(s), every install hook also an upgrade hook"
          % (counts.get("1", 0), counts.get("2", 0), PROBE_COMPONENT, counts.get("3", 0), counts.get("4", 0)))


main()
