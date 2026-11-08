#!/usr/bin/env python3
"""Every Job must be a Helm hook, and the hooks must run in an order their dependencies allow.

    hack/validate-job-hooks.py [repo-root] [--base <git-ref>] [component ...]

A Job's pod template is immutable, and Krateo's composition-dynamic-controller patches a plain
Job in place. So any release that changes a Job's template -- an image bump is enough -- failed
that component's upgrade with `cannot patch ... spec.template: Invalid value`, while the
Composition still reported Synced=True (#179, core-provider#403). Measured on 0.3.40: powerdns's
api-key-init. Upgrades from 0.3.34 to 0.3.39 worked only because no other Job template changed.

A Helm hook is never patched: `before-hook-creation` deletes the old Job and the new one runs.
So every Job a chart renders must be one of:
  - post-install,post-upgrade, with before-hook-creation, a numeric weight, and never
    hook-succeeded (kubernetes-entrypoint finds a completed Job by name; a Job deleted on success
    would leave every pod waiting on it forever);
  - pre-delete, for teardown Jobs (db-drop, and the clean Jobs that delete images or secrets),
    which must not run on install or upgrade at all.

Helm runs post-* hooks one at a time in weight order and waits for each, so a hook that waits
(through kubernetes-entrypoint) on a Job that sorts after it would block until the hook timeout.
Each hook's effective dependencies are its DEPENDENCY_JOBS plus the Jobs that the pods behind
each same-chart DEPENDENCY_SERVICE wait on; every one must have a strictly lower weight, because
equal weights run in an order Helm does not promise.

Each chart is rendered twice: with its defaults, and with every manifests.job_* switch on, so a
Job that is off by default is still checked. Rendered under a release name unlike the chart's,
as Krateo names one. Manifests are parsed with duplicate keys refused, because an annotation
emitted twice is silently last-wins in a lenient parser.

With --base REF it also renders each chart at that git ref and lists every Job whose pod
template differs, which is exactly the set a plain Job would fail to upgrade; each must be a
hook in the new render.

It fails when it measured nothing: no charts, a chart that does not render, or no Jobs at all.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required")

RELEASE_SUFFIX = "gate1234"
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "Job")

# Jobs allowed to be something other than the two shapes above, as (component, job name): reason.
# Every entry is a reviewed decision; an entry added to make the gate pass recreates #179.
ALLOWED = {}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _no_duplicates(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep)


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def job_switches(chart: Path):
    values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
    manifests = values.get("manifests") or {}
    return sorted(k for k, v in manifests.items() if k.startswith("job_") and isinstance(v, bool))


def render(chart: Path, toolkit: Path, component: str, workdir: Path, extra):
    staged = workdir / component
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(chart, staged)
    cm = staged / "Chart.yaml"
    cm.write_text(cm.read_text().replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in cm.read_text() and toolkit.is_dir():
        (staged / "charts").mkdir(exist_ok=True)
        if not (staged / "charts" / "helm-toolkit").exists():
            shutil.copytree(toolkit, staged / "charts" / "helm-toolkit")
    r = subprocess.run(["helm", "template", f"{component}-{RELEASE_SUFFIX}", str(staged),
                        "--namespace", "openstack", *extra], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-1500:])
    return [d for d in yaml.load_all(r.stdout, Loader=UniqueKeyLoader) if isinstance(d, dict)]


def deps_of(pod):
    jobs, svcs = set(), set()
    for c in pod.get("initContainers") or []:
        for e in c.get("env") or []:
            v = e.get("value") or ""
            if e.get("name") == "DEPENDENCY_JOBS":
                jobs |= {x for x in v.split(",") if x}
            elif e.get("name") == "DEPENDENCY_SERVICE":
                svcs |= {x.split(":")[-1] for x in v.split(",") if x}
    return jobs, svcs


def classify(job):
    """Return ('upgrade', weight) / ('teardown', None) / ('plain'|'bad', reason)."""
    a = (job.get("metadata") or {}).get("annotations") or {}
    hook = {h.strip() for h in str(a.get("helm.sh/hook", "")).split(",") if h.strip()}
    policy = {p.strip() for p in str(a.get("helm.sh/hook-delete-policy", "")).split(",") if p.strip()}
    if not hook:
        return "plain", "not a Helm hook, so a changed pod template fails the upgrade"
    if hook == {"pre-delete"}:
        return "teardown", None
    if hook != {"post-install", "post-upgrade"}:
        return "bad", f"hook {sorted(hook)}: must be post-install,post-upgrade (or pre-delete for teardown)"
    if "before-hook-creation" not in policy:
        return "bad", "no before-hook-creation delete policy, so the next upgrade cannot re-create it"
    if "hook-succeeded" in policy:
        return "bad", "hook-succeeded deletes the completed Job that kubernetes-entrypoint waits on by name"
    try:
        return "upgrade", int(str(a.get("helm.sh/hook-weight", "")))
    except ValueError:
        return "bad", f"hook-weight {a.get('helm.sh/hook-weight')!r} is not an integer"


def check(component, docs):
    """Return (jobs measured, failures)."""
    services = {d["metadata"]["name"]: (d.get("spec") or {}).get("selector") or {}
                for d in docs if d.get("kind") == "Service"}
    workloads = []
    for d in docs:
        if d.get("kind") in WORKLOADS:
            t = (d.get("spec") or {}).get("template") or {}
            labels = (t.get("metadata") or {}).get("labels") or {}
            j, s = deps_of(t.get("spec") or {})
            workloads.append((d["kind"], d["metadata"]["name"], labels, j, s, d))
    jobs = {w[1]: w for w in workloads if w[0] == "Job"}
    failures, shape = [], {}
    for name, w in jobs.items():
        kind, info = classify(w[5])
        shape[name] = (kind, info)
        if kind in ("plain", "bad") and (component, name) not in ALLOWED:
            failures.append(f"Job {name}: {info}")

    def behind(svc, seen):
        sel = services.get(svc)
        if not sel:
            return set()
        out = set()
        for kind, name, labels, j, s, _ in workloads:
            if kind != "Job" and all(labels.get(k) == v for k, v in sel.items()):
                out |= {x for x in j if x in jobs}
                for s2 in s - seen:
                    out |= behind(s2, seen | {s2})
        return out

    for name, (kind, weight) in shape.items():
        if kind != "upgrade":
            continue
        _, _, _, j, s, _ = jobs[name]
        eff = {x for x in j if x in jobs}
        for svc in s:
            eff |= behind(svc, {svc})
        eff.discard(name)
        for dep in sorted(eff):
            dkind, dweight = shape[dep]
            if dkind == "teardown":
                failures.append(f"Job {name} waits on {dep}, which only runs at uninstall")
            elif dkind == "upgrade" and dweight >= weight:
                failures.append(f"Job {name} (weight {weight}) waits on {dep} (weight {dweight}); "
                                f"Helm runs hooks in weight order, so {dep} must sort strictly first")
    return len(jobs), failures


def templates(docs):
    out = {}
    for d in docs:
        if d.get("kind") == "Job":
            out[d["metadata"]["name"]] = yaml.safe_dump(
                (d.get("spec") or {}).get("template"), sort_keys=True)
    return out


def checkout(root: Path, ref: str, dest: Path):
    """Extract blueprints/ and charts/ at REF into dest, through git archive."""
    archive = dest / "base.tar"
    with open(archive, "wb") as fh:
        r = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", ref,
                            "blueprints", "charts"], stdout=fh, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"git archive {ref}: {r.stderr.decode().strip()}")
    with tarfile.open(archive) as tf:
        tf.extractall(dest, filter="data")
    archive.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("components", nargs="*")
    ap.add_argument("--base", help="git ref to compare Job pod templates against")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    charts = sorted(p.parent for p in (root / "blueprints").glob("*/chart/Chart.yaml"))
    charts = [c for c in charts if c.parent.name != "openstack"]  # the umbrella renders no Jobs
    if a.components:
        charts = [c for c in charts if c.parent.name in a.components]
    if not charts:
        print("FAIL: no component charts found -- nothing was measured")
        return 1

    failures, total_jobs, unrendered = [], 0, []
    for vp in root.glob("blueprints/*/chart/values.yaml"):
        values = yaml.safe_load(vp.read_text()) or {}
        if values.get("helm3_hook") is False:
            failures.append(f"{vp.parts[-3]}: helm3_hook: false renders plain Jobs, which is #179")

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        base_root = None
        if a.base:
            base_root = work / "base"
            base_root.mkdir()
            checkout(root, a.base, base_root)
        changed_total = 0
        for chart in charts:
            component = chart.parent.name
            toolkit = root / "charts" / "helm-toolkit"
            switches = job_switches(chart)
            on = [x for k in switches for x in ("--set", f"manifests.{k}=true")]
            measured = 0
            for label, extra in (("defaults", []), ("all jobs on", on)):
                if label == "all jobs on" and not switches:
                    continue
                try:
                    docs = render(chart, toolkit, component, work / "r", extra)
                except (RuntimeError, yaml.YAMLError) as e:
                    unrendered.append((component, label, str(e)))
                    continue
                n, f = check(component, docs)
                measured = max(measured, n)
                failures += [f"{component} ({label}): {x}" for x in f]
                if label == "defaults" and base_root is not None:
                    bchart = base_root / "blueprints" / component / "chart"
                    if bchart.is_dir():
                        try:
                            bdocs = render(bchart, base_root / "charts" / "helm-toolkit",
                                           component, work / "b", [])
                        except (RuntimeError, yaml.YAMLError) as e:
                            bdocs = None
                            print(f"NOTE {component}: base {a.base} does not render ({str(e)[:120]})")
                        if bdocs is not None:
                            new, old = templates(docs), templates(bdocs)
                            changed = sorted(n for n in new if n in old and new[n] != old[n])
                            changed_total += len(changed)
                            shapes = {d["metadata"]["name"]: classify(d)[0] for d in docs
                                      if d.get("kind") == "Job"}
                            for name in changed:
                                if shapes[name] not in ("upgrade", "teardown"):
                                    failures.append(f"{component}: Job {name} changes its pod "
                                                    f"template since {a.base} and is not a hook, "
                                                    "so this upgrade would fail")
                            if changed:
                                print(f"     {component}: {len(changed)} Job template(s) differ "
                                      f"from {a.base}: {', '.join(changed)}")
            total_jobs += measured
            mine = [x for x in failures if x.startswith(component + " ")]
            print(f"{'FAIL' if mine else 'OK  '} {component}: {measured} Jobs"
                  + (f", {len(mine)} problem(s)" if mine else ""))
        if base_root is not None:
            print(f"     {changed_total} Job template(s) differ from {a.base} in total")

    for component, label, err in unrendered:
        print(f"FAIL {component} ({label}): did not render, so nothing was measured: {err}")
    if total_jobs == 0:
        print("FAIL: no Jobs rendered in any chart -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures or unrendered:
        print(f"FAIL: {len(failures)} problem(s), {len(unrendered)} unrendered, across {len(charts)} charts")
        return 1
    print(f"OK: {len(charts)} charts, {total_jobs} Jobs, every one a hook in dependency order")
    return 0


if __name__ == "__main__":
    sys.exit(main())
