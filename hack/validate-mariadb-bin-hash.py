#!/usr/bin/env python3
"""A mariadb workload rolls when, and only when, a mariadb-bin script it mounts changes (#445).

Upstream hashes the whole rendered configmap-bin.yaml into every workload that mounts mariadb-bin. That ConfigMap also
holds scripts the server never runs (the controller's mariadb_controller.py, the test pod's test.sh, the cluster-wait
Job's script, the backup scripts), so a change to any of them rolled the one-replica server StatefulSet: a database
outage. Measured on GKE (r074, 0.3.71 -> 0.3.74-main.gc16973f): #335's controller heartbeat restarted MariaDB and
Keystone's token API failed for about 2 minutes. templates/_bin_hash.tpl now hashes, per workload, only the keys it
mounts.

This gate renders the mariadb chart (default gate values, and again with conf.backup.enabled so the backup keys exist)
and, for every key of the rendered mariadb-bin whose source is a templates/bin/ file, appends one comment line to that
source and re-renders. For every long-running workload (Deployment, StatefulSet, DaemonSet) that mounts the mariadb-bin
volume, its pod template must change exactly when the mutated key is one it mounts (its subPath mounts, or every key
when it mounts the whole volume). It also checks that each mutation reached the rendered ConfigMap (else it measured
nothing).

Planted control, run every time: the same check on a copy whose server StatefulSet hashes the whole configmap-bin.yaml
again (upstream's line) must FAIL, naming the controller's key. If it passes, this gate cannot say no and fails.

Fails when the chart does not render, when no workload mounts mariadb-bin, or when no key of either kind (mounted by
the server / not mounted by it) was mutated.

Usage: hack/validate-mariadb-bin-hash.py [repo-root]
"""
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("job_hooks", HERE / "validate-job-hooks.py")
job_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job_hooks)

LONG_RUNNING = ("Deployment", "StatefulSet", "DaemonSet")
VOLUME_CM = "mariadb-bin"
KEY_SOURCE = re.compile(r'^  ([A-Za-z0-9_.-]+): \|\n\{\{ tuple "(bin/[^"]+)"', re.M)
SHARED_HASH = '{{ tuple "configmap-bin.yaml" . | include "helm-toolkit.utils.hash" }}'
SERVER_HASH = re.compile(r'(\n        configmap-bin-hash: )\{\{ tuple \(include "mariadb.server_bin_keys"[^\n]*\}\}')
VARIANTS = (("defaults", []), ("backup on", ["--set", "conf.backup.enabled=true"]))


def bin_data(docs):
    for d in docs:
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == VOLUME_CM:
            return d.get("data") or {}
    return None


def workloads(docs):
    """{kind/name: (pod template JSON, set of mounted mariadb-bin keys or None for the whole volume)}"""
    out = {}
    for d in docs:
        if d.get("kind") not in LONG_RUNNING:
            continue
        pod = d["spec"]["template"]
        vols = {v["name"] for v in pod["spec"].get("volumes") or [] if (v.get("configMap") or {}).get("name") == VOLUME_CM}
        if not vols:
            continue
        keys, whole = set(), False
        for c in (pod["spec"].get("containers") or []) + (pod["spec"].get("initContainers") or []):
            for m in c.get("volumeMounts") or []:
                if m["name"] in vols:
                    if m.get("subPath"):
                        keys.add(m["subPath"])
                    else:
                        whole = True
        out["%s/%s" % (d["kind"], d["metadata"]["name"])] = (json.dumps(pod, sort_keys=True), None if whole else keys)
    return out


def run(root: Path, chart_src: Path, label: str):
    """(failures, server-mounted keys mutated, other keys mutated, workloads seen)"""
    toolkit = root / "charts" / "helm-toolkit"
    fails, mounted_n, other_n, seen = [], 0, 0, set()
    sources = dict(KEY_SOURCE.findall((chart_src / "templates" / "configmap-bin.yaml").read_text()))
    if not sources:
        return ["%s: no key of configmap-bin.yaml has a templates/bin/ source: nothing to mutate" % label], 0, 0, seen
    for vlabel, extra in VARIANTS:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            try:
                base = job_hooks.render(chart_src, toolkit, "mariadb", td / "base", extra)
            except RuntimeError as e:
                return ["%s (%s): the chart does not render: %s" % (label, vlabel, e)], 0, 0, seen
            data, wl = bin_data(base), workloads(base)
            if data is None or not wl:
                return ["%s (%s): no mariadb-bin ConfigMap or no long-running workload mounting it" % (label, vlabel)], 0, 0, seen
            seen |= set(wl)
            for key, src in sorted(sources.items()):
                if key not in data:
                    continue
                mut = td / ("mut-" + key)
                shutil.copytree(chart_src, mut)
                f = mut / "templates" / src
                f.write_text(f.read_text().rstrip("\n") + "\n# validate-mariadb-bin-hash mutation of %s\n" % key)
                docs = job_hooks.render(mut, toolkit, "mariadb", td / ("r-" + key), extra)
                if (bin_data(docs) or {}).get(key) == data[key]:
                    fails.append("%s (%s): mutating %s did not change mariadb-bin's %s: the gate measured nothing" % (label, vlabel, src, key))
                    continue
                after = workloads(docs)
                for w, (tmpl, keys) in sorted(wl.items()):
                    mounts = keys is None or key in keys
                    changed = after.get(w, ("",))[0] != tmpl
                    if w.startswith("StatefulSet/"):
                        mounted_n += mounts
                        other_n += not mounts
                    if mounts and not changed:
                        fails.append("%s (%s): %s mounts %s, and its pod template did not change when %s changed: it would keep the old script" % (label, vlabel, w, key, src))
                    if changed and not mounts:
                        fails.append("%s (%s): %s does not mount %s, and its pod template changed when %s changed: it would restart for a script it never runs" % (label, vlabel, w, key, src))
    return fails, mounted_n, other_n, seen


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    chart = root / "blueprints" / "mariadb" / "chart"
    fails, mounted_n, other_n, seen = run(root, chart, "mariadb")
    print("workloads mounting mariadb-bin: %s" % (" ".join(sorted(seen)) or "none"))
    print("server-mounted key mutations: %d; mutations of keys the server does not mount: %d" % (mounted_n, other_n))
    if not mounted_n or not other_n:
        fails.append("no mutation of a key the server %s: nothing measured" % ("mounts" if not mounted_n else "does not mount"))
    # planted control: upstream's shared hash restored on the server must be caught
    with tempfile.TemporaryDirectory() as td:
        planted = Path(td) / "chart"
        shutil.copytree(chart, planted)
        sts = planted / "templates" / "statefulset.yaml"
        text = sts.read_text()
        text2, n = SERVER_HASH.subn(lambda m: m.group(1) + SHARED_HASH, text)
        if n != 1:
            fails.append("control: the server pod template's scoped configmap-bin-hash line was not found (found %d), so the planted case cannot be built" % n)
        else:
            sts.write_text(text2)
            pf, _, _, _ = run(root, planted, "planted")
            caught = [f for f in pf if "StatefulSet/" in f and "mariadb_controller.py" in f and "does not mount" in f]
            if caught:
                print("control: the shared hash restored on the server is caught (%d finding(s), e.g. %s)" % (len(pf), caught[0][:160]))
            else:
                fails.append("control: with upstream's shared hash restored on the server, the gate found no controller-script restart: it cannot say no (%s)" % (pf[:2] or "no findings"))
    for f in fails:
        print("FAIL " + f)
    print("OK: every mariadb workload rolls exactly when a script it mounts changes" if not fails else "%d failure(s)" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
