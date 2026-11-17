#!/usr/bin/env python3
"""glance's image store is large enough by default, grows on upgrade, and never asks a claim to shrink (issue #295).

    hack/validate-glance-store.py [repo-root]

On 2 GiB, one duplicate upload of manila's 952 MB service image filled glance-images (GKE 2026-11-17), glance-api went
unready on #163's free-space floor, and the upgrade stalled. The default is 10Gi from 0.3.71.

The chart reads the live glance-images claim (its own, in its namespace) and renders volume.size unless the live claim
already requests more: a PersistentVolumeClaim cannot shrink, the API server refuses that, and the release would be left
failed. Offline there is no live claim, so the one lookup is replaced by stand-in claims and what the chart renders is
checked, case by case:

  - no live claim                          -> volume.size (10Gi at defaults)
  - a live 2Gi claim (installs before 0.3.71) -> 10Gi: the claim is expanded
  - a live claim equal in another unit (10240Mi) -> volume.size
  - a live 20Gi claim                      -> 20Gi: never shrunk
  - volume.size 5Gi and a live 2Gi claim   -> 5Gi: the value stays overridable
  - a live claim whose size is not a quantity -> refused, naming it

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


def main():
    default = ((yaml.safe_load(open(os.path.join(CHART, "values.yaml"))) or {}).get("volume") or {}).get("size")
    if default != DEFAULT:
        failures.append("glance's volume.size is %s, want %s: on 2Gi one duplicate 952 MB manila image filled the store "
                        "(#295)" % (default, DEFAULT))
    cases = [("no live claim", None, {}, DEFAULT),
             ("a live 2Gi claim", "2Gi", {}, DEFAULT),
             ("a live 10240Mi claim", "10240Mi", {}, DEFAULT),
             ("a live 20Gi claim", "20Gi", {}, "20Gi"),
             ("volume.size 5Gi and a live 2Gi claim", "2Gi", {"volume": {"size": "5Gi"}}, "5Gi"),
             ("a live claim of size 'lots'", "lots", {}, "refused")]
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
    print("OK: glance-images: %d case(s): %s by default, expanded from a smaller live claim, never shrunk, overridable"
          % (checked, DEFAULT))


main()
