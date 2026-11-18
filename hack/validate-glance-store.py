#!/usr/bin/env python3
"""glance's image store: 10Gi for a new claim, an existing claim's size kept unless the user asks (issue #295).

    hack/validate-glance-store.py [repo-root]

On 2 GiB, one duplicate upload of manila's 952 MB service image filled glance-images (GKE 2026-11-17), glance-api went
unready on #163's free-space floor, and the upgrade stalled. From 0.3.71 a new claim requests volume.defaultSize (10Gi).
An existing claim keeps its size on upgrade (Diego, 2026-11-17): no upgrade patches a claim nobody asked to change, so a
StorageClass without allowVolumeExpansion (kind's local-path) cannot make one fail. Setting volume.size (empty by
default, so "set" is a fact rather than a guess from the value) resizes it, never below the live claim: a claim cannot
shrink.

The chart reads the live glance-images claim with one lookup, replaced here by stand-in claims, and what it renders is
checked, case by case:

  - no live claim                              -> 10Gi (volume.defaultSize)
  - no live claim, volume.size 5Gi             -> 5Gi
  - a live 2Gi claim, no volume.size           -> 2Gi: kept
  - a live 2Gi claim, volume.size 10Gi         -> 10Gi: expanded, as asked
  - a live 20Gi claim, volume.size 10Gi        -> 20Gi: never shrunk
  - a live 10240Mi claim, volume.size 10Gi     -> 10Gi (equal)
  - volume.size `lots`                         -> refused (by the schema, or the chart), naming it

And the store moving to Ceph (#308: storage rbd with the umbrella's krateo.ceph references), which stops glance
mounting the claim and leaves every image on it unreadable, reads the same one lookup:

  - a live claim, images not migrated          -> refused, naming storage.ceph.glanceImagesMigrated
  - a live claim, krateo.imagesMigratedFromPvc -> renders, no glance-images claim
  - no live claim                              -> renders, no glance-images claim

Fails when helm or PyYAML is missing, and when the chart no longer looks the claim up exactly once.
"""
import os
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
CHART = os.path.join(ROOT, "blueprints", "glance", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
LOOKUP = 'lookup "v1" "PersistentVolumeClaim" .Release.Namespace "glance-images"'
DEFAULT = "10Gi"
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
failures = []


def stage(tmp, name, live):
    w = os.path.join(tmp, name)
    shutil.copytree(CHART, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    os.makedirs(os.path.join(w, "charts"), exist_ok=True)
    shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    hits = []
    for dirpath, _, files in os.walk(os.path.join(w, "templates")):
        for f in files:
            p = os.path.join(dirpath, f)
            t = open(p).read()
            if LOOKUP in t:
                hits.append(p)
                obj = "(dict)" if live is None else (
                    '(dict "spec" (dict "resources" (dict "requests" (dict "storage" "%s"))))' % live)
                open(p, "w").write(t.replace(LOOKUP, obj))
    return w, hits


def render(w, values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    try:
        r = subprocess.run(["helm", "template", "glance-gate1234", w, "--namespace", "openstack", "--kube-version",
                            "1.36.0", "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r


def ceph_rbd(migrated=False):
    ref = lambda secret, key: {"store": "openstack-credentials", "secret": secret, "key": key}
    v = {"storage": "rbd", "krateo": {"ceph": {"keyringFrom": ref("openstack-credential-ceph-client-glance", "key"),
                                               "monHostFrom": ref("openstack-credential-ceph-mon-host", "mon_host")}}}
    if migrated:
        v["krateo"]["imagesMigratedFromPvc"] = True
    return v


def main():
    vol = (yaml.safe_load(open(os.path.join(CHART, "values.yaml"))) or {}).get("volume") or {}
    if vol.get("defaultSize") != DEFAULT or vol.get("size") not in ("", None):
        failures.append("glance's volume.defaultSize is %r and volume.size %r, want %r and empty: a new claim gets 10Gi "
                        "(on 2Gi one duplicate 952 MB manila image filled the store, #295), and volume.size is the "
                        "user's, so an upgrade tells a request from a default" % (vol.get("defaultSize"), vol.get("size"),
                                                                                   DEFAULT))
    cases = [("no live claim", None, {}, DEFAULT),
             ("no live claim, volume.size 5Gi", None, {"volume": {"size": "5Gi"}}, "5Gi"),
             ("a live 2Gi claim, no volume.size", "2Gi", {}, "2Gi"),
             ("a live 2Gi claim, volume.size 10Gi", "2Gi", {"volume": {"size": "10Gi"}}, "10Gi"),
             ("a live 20Gi claim, volume.size 10Gi", "20Gi", {"volume": {"size": "10Gi"}}, "20Gi"),
             ("a live 10240Mi claim, volume.size 10Gi", "10240Mi", {"volume": {"size": "10Gi"}}, "10Gi"),
             ("volume.size 'lots'", "2Gi", {"volume": {"size": "lots"}}, "refused"),
             ("ceph rbd, a live 2Gi claim, images not migrated", "2Gi", ceph_rbd(), "refused-migrate"),
             ("ceph rbd, a live 2Gi claim, images migrated", "2Gi", ceph_rbd(True), "no-claim"),
             ("ceph rbd, no live claim", None, ceph_rbd(), "no-claim")]
    checked = 0
    with tempfile.TemporaryDirectory() as tmp:
        for n, (label, live, values, want) in enumerate(cases):
            w, hits = stage(tmp, "g%d" % n, live)
            if len(hits) != 1 or open(hits[0]).read().count("(dict") == 0:
                failures.append("the glance chart looks up the live glance-images claim %d time(s) (%s), want once: "
                                "without it an upgrade cannot know the claim's size, and one that asks a claim to shrink "
                                "is refused" % (len(hits), LOOKUP))
                break
            r = render(w, values)
            if want == "refused-migrate":
                if r.returncode == 0 or "storage.ceph.glanceImagesMigrated" not in r.stderr:
                    failures.append("%s: renders (rc=%d) or does not name storage.ceph.glanceImagesMigrated; moving the "
                                    "store off a live claim would leave its images unreadable" % (label, r.returncode))
                checked += 1
                continue
            if want == "no-claim":
                if r.returncode != 0:
                    failures.append("%s: does not render: %s" % (label, r.stderr.strip()[-200:]))
                elif [d for d in yaml.safe_load_all(r.stdout) if d and d.get("kind") == "PersistentVolumeClaim"]:
                    failures.append("%s: renders a claim; the store is Ceph" % label)
                checked += 1
                continue
            if want == "refused":
                if r.returncode == 0 or "lots" not in r.stderr:
                    failures.append("%s: renders (rc=%d); a size it cannot compare must be refused, naming it"
                                    % (label, r.returncode))
                checked += 1
                continue
            if r.returncode != 0:
                failures.append("%s: does not render: %s" % (label, r.stderr.strip()[:200]))
                continue
            pvc = [d for d in yaml.safe_load_all(r.stdout) if d and d.get("kind") == "PersistentVolumeClaim"
                   and d["metadata"]["name"] == "glance-images"]
            got = pvc[0]["spec"]["resources"]["requests"]["storage"] if pvc else None
            if got != want:
                failures.append("%s: glance-images requests %s, want %s" % (label, got, want))
            checked += 1
    if checked == 0 and not failures:
        failures.append("no case was rendered; the check had no input")
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: glance-images: %d case(s): %s for a new claim, an existing claim kept unless volume.size asks, raised to "
          "it, never shrunk" % (checked, DEFAULT))


main()
