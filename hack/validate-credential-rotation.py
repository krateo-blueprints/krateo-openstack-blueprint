#!/usr/bin/env python3
"""A RabbitMQ user's rotation is derived from what exists, mints an immutable root per generation, and refuses a token it
cannot honour (#309 C, Diego's (d)).

    hack/validate-credential-rotation.py [repo-root]

Why immutable roots: switching the existing roots from CreatedOnce to OnChange is itself a spec change, so ESO
(v2.14.0 shouldRefresh: OnChange refreshes when syncedResourceVersion != generation+HashMeta) would regenerate every
messaging root at the upgrade, chart-mode installs included. So a rotation token names a NEW root,
<base>-g<first 8 hex of sha256(token)>, minted once; the base root (generation 0) is never touched.

Why derived: the active root is what the LIVE component Composition references now, and the previous one is stamped on
the target root's ExternalSecret (krateo.io/previous-root) when first rendered. No ConfigMap is consulted, so losing
openstack-credential-generations loses nothing a rotation needs (case 7: none of these cases supplies it).

  1. osh.messagingRotation, probed with synthetic live specs and a stand-in for osh.lookupExternalSecret (helm template's
     lookup returns nothing):
       a. no token, nothing live: steady on the base root;
       b. a new token: minting the generation root, previous = the active base;
       c. the same token mid-rotation (its root exists, stamped with the active root): still minting, no refusal;
       d. a reused token (its root exists, stamped with another root than the active one): refused, naming why;
       e. the token removed after a rotation (active is a generation root): refused, naming why;
       f. switched (active == target): steady, previous read from the stamp.
  2. The umbrella rendered offline in operator mode with rotate.oslo-messaging-heat set: it emits the generation root,
     refreshPolicy CreatedOnce, from the Password generator, stamped with previous-root and the token; with the reused-
     token stand-in, the render refuses with that message on the status.

Fails when helm or PyYAML is missing, and when a part checks nothing.
"""
import hashlib
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
UMBRELLA = os.path.join(ROOT, "blueprints", "openstack", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
failures = []
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)


def fail(m):
    failures.append(m)


def stage(tmp, name):
    w = os.path.join(tmp, name)
    shutil.copytree(UMBRELLA, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    if os.path.isdir(TOOLKIT):
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    hp = os.path.join(w, "templates", "_helpers.tpl")
    h = open(hp).read()
    rx = re.compile(r'(\{\{- define "osh\.lookupExternalSecret" -\}\}).*?(\{\{- end -\}\})', re.S)
    if not rx.search(h):
        print("FAILED MEASUREMENT: _helpers.tpl defines no osh.lookupExternalSecret, so it cannot be stood in for")
        sys.exit(2)
    open(hp, "w").write(rx.sub(lambda m: m.group(1) + "\n{{- index ((index . 0).Values.gateES | default dict) (index . 1) "
                               "| default dict | toJson -}}\n" + m.group(2), h, count=1))
    return w


def helm(w, vals, extra=()):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(vals, f)
    try:
        return subprocess.run(["helm", "template", "openstack-gate", w, "-n", "openstack", "--kube-version", "1.36.0",
                               "-f", f.name] + list(extra), capture_output=True, text=True)
    finally:
        os.unlink(f.name)


BASE_VALS = {"profile": "full", "messaging": {"backend": "operator"},
             "componentValues": {"rabbitmq": {"pod": {"replicas": {"server": 3}}}}}


def merged(a, b):
    out = json.loads(json.dumps(a))
    for k, v in b.items():
        out[k] = merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def part1(tmp):
    w = stage(tmp, "probe")
    open(os.path.join(w, "templates", "zz-gate-rotation-probe.yaml"), "w").write(
        '{{- range $case := .Values.gateCases }}\n'
        '{{- range $c := $.Values.components }}{{- if eq $c.name "heat" }}\n'
        '{{ printf "\\n# ROTATION %s %s\\n" $case.name (include "osh.messagingRotation" (list $ $c ($case.live | default dict))) }}\n'
        '{{- end }}{{- end }}\n{{- end }}\n')
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    base = next(i["secret"] for i in uv["credentials"]["items"] if i["id"] == "oslo-messaging-heat")
    gen = lambda tok: "%s-g%s" % (base, hashlib.sha256(tok.encode()).hexdigest()[:8])
    live = lambda root: {"endpoints": {"oslo_messaging": {"auth": {"heat": {"passwordFrom": {"secret": root}}}}}}
    stamp = lambda prev: {"metadata": {"annotations": {"krateo.io/previous-root": prev}}}
    cases = [
        ("a", {}, {}, None, {"phase": "steady", "target": base, "active": base}),
        ("b", {"t1"}, {}, None, {"phase": "minting", "target": gen("t1"), "active": base, "previous": base}),
        ("c", {"t1"}, {}, {gen("t1"): stamp(base)}, {"phase": "minting", "previous": base}),
        ("d", {"t1"}, live(gen("t0")), {gen("t1"): stamp(base)}, {"refuse": "names an old generation"}),
        ("e", set(), live(gen("t1")), {}, {"refuse": "was removed"}),
        ("f", {"t1"}, live(gen("t1")), {gen("t1"): stamp(base)}, {"phase": "steady", "previous": base}),
    ]
    n = 0
    for name, toks, lv, es, want in cases:
        vals = merged(BASE_VALS, {"gateCases": [{"name": name, "live": lv}], "gateES": es or {}})
        if toks:
            vals["rotate"] = {"oslo-messaging-heat": sorted(toks)[0]}
        r = helm(w, vals, ("--show-only", "templates/zz-gate-rotation-probe.yaml"))
        m = re.search(r"^# ROTATION %s (.*)$" % name, r.stdout, re.M)
        if r.returncode != 0 or not m:
            fail("1%s: the probe did not render: %s" % (name, (r.stderr or r.stdout).strip()[:300]))
            continue
        got = json.loads(m.group(1))
        bad = []
        for k, v in want.items():
            if k == "refuse":
                if v not in str(got.get("refuse", "")):
                    bad.append("refuse %r, want it to say %r" % (got.get("refuse"), v))
            elif got.get(k) != v:
                bad.append("%s %r, want %r" % (k, got.get(k), v))
        if "refuse" not in want and got.get("refuse"):
            bad.append("refused (%s), want no refusal" % got["refuse"])
        if bad:
            fail("1%s: %s" % (name, "; ".join(bad)))
        else:
            n += 1
    return n, base, gen


def part2(tmp, base, gen):
    w = stage(tmp, "full")
    r = helm(w, merged(BASE_VALS, {"rotate": {"oslo-messaging-heat": "t1"}}))
    if r.returncode != 0:
        fail("2: the umbrella does not render with rotate.oslo-messaging-heat: %s" % r.stderr.strip()[:300])
        return 0
    es = [d for d in yaml.safe_load_all(r.stdout) if d and d.get("kind") == "ExternalSecret"
          and d["metadata"]["name"] == gen("t1")]
    n = 0
    if len(es) != 1:
        fail("2: want exactly one generation root %s, got %d" % (gen("t1"), len(es)))
    else:
        e = es[0]
        ann = e["metadata"].get("annotations") or {}
        spec = e["spec"]
        problems = []
        if spec.get("refreshPolicy") != "CreatedOnce":
            problems.append("refreshPolicy %r" % spec.get("refreshPolicy"))
        if not any(((x.get("sourceRef") or {}).get("generatorRef") or {}).get("kind") == "Password"
                   for x in spec.get("dataFrom") or []):
            problems.append("no Password generator")
        if ann.get("krateo.io/previous-root") != base or ann.get("krateo.io/rotation-token") != "t1":
            problems.append("stamps %s" % {k: ann.get(k) for k in ("krateo.io/previous-root", "krateo.io/rotation-token")})
        if ann.get("helm.sh/resource-policy") != "keep":
            problems.append("not resource-policy keep")
        if problems:
            fail("2: the generation root %s: %s" % (gen("t1"), ", ".join(problems)))
        else:
            n += 1
    r = helm(w, merged(BASE_VALS, {"rotate": {"oslo-messaging-heat": "t1"},
                                   "gateES": {gen("t1"): {"metadata": {"annotations": {
                                       "krateo.io/previous-root": gen("t0")}}}}}))
    if r.returncode == 0 or "names an old generation" not in r.stderr:
        fail("2: with a reused token the render must refuse naming why; rc %d: %s" % (r.returncode, r.stderr.strip()[:200]))
    else:
        n += 1
    return n


def main():
    with tempfile.TemporaryDirectory() as tmp:
        n1, base, gen = part1(tmp)
        n2 = part2(tmp, base, gen)
    if not n1:
        fail("part 1 checked nothing")
    if not n2:
        fail("part 2 checked nothing")
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: rotation derived from what exists (no ConfigMap consulted): %d probe case(s), lookups stood in; the "
          "umbrella mints an immutable, stamped generation root and refuses a reused token (%d render check(s))"
          % (n1, n2))


main()
