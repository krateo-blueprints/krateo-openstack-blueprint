#!/usr/bin/env python3
"""Every Secret, ConfigMap, claim and ServiceAccount a pod names must be one its chart renders.

    hack/validate-pod-references.py [repo-root] [component ...]

A pod spec that names an object nobody creates is valid YAML, renders without complaint and is
accepted by the API server. The pod then waits forever: `FailedMount ... configmap "x" not
found`, or a container that never starts because an env secret is missing. Nothing fails, so
nothing reports it.

The powerdns chart shipped exactly that (#179). Its own templates named the bin and etc
ConfigMaps after the Helm release (`<release>-powerdns-bin`), while the db-init job, rendered by
helm-toolkit, mounted the fixed names `powerdns-bin` and `powerdns-etc`. The two agree only when
the release is called `powerdns`, which is what the smoke test used. Krateo names a release after
the Composition plus a suffix (`powerdns-wvxf9xwb`), so on a real install the jobs waited on
FailedMount for as long as anyone watched.

So each chart is rendered under a release name unlike its chart name, the way Krateo names one,
and every reference a pod template makes is checked against the objects the same render
produced. A reference marked `optional: true` is skipped, because Kubernetes starts the pod
without it. Anything legitimately created outside the chart must be listed in ALLOWED below,
with the reason, so it is a reviewed decision and not a silent exception.

It fails when it measured nothing: a chart that does not render, or a run that found no pod
templates at all, is a failed measurement, not a pass.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required")

# The release name each chart is rendered under. Unlike the chart name, as Krateo's is: the
# Composition's name plus a generated suffix.
RELEASE_SUFFIX = "gate1234"

# Objects a pod may name although its own chart does not render them, as
# (component, kind, name): reason. Every entry is a decision someone reviewed. Adding one to make
# the gate pass without knowing who creates the object recreates the defect this gate exists for.
ALLOWED = {
    # Created at runtime by keystone's fernet-setup and credential-setup Jobs
    # (bin/_fernet-manage.py.tpl writes them through the API), then rotated by its CronJobs.
    ("keystone", "Secret", "keystone-fernet-keys"):
        "minted at runtime by keystone's fernet-setup Job",
    ("keystone", "Secret", "keystone-credential-keys"):
        "minted at runtime by keystone's credential-setup Job",
    # Minted once per install by powerdns's api-key-init Job (bin/_api-key-init.py.tpl), so the
    # key is in no rendered manifest. designate-worker reads the same Secret by name.
    ("powerdns", "Secret", "powerdns-api-key"):
        "minted at runtime by powerdns's api-key-init Job",
    ("designate", "Secret", "powerdns-api-key"):
        "minted at runtime by the powerdns component's api-key-init Job",
}

# Real defects, each with an open issue. Listed on every run as KNOWN, so they stay visible, and
# not failed on, so the gate can guard every other chart meanwhile. An entry is removed by fixing
# the chart; until then the issue says why the object is still dangling.
KNOWN_DEFECTS = {
    ("cinder", "ConfigMap", "ceph-etc"): "#196",
    ("cinder", "Secret", "cinder-volume-rbd-keyring"): "#196",
    ("cinder", "Secret", "pvc-ceph-client-key"): "#196",
}

KINDS = ("Secret", "ConfigMap", "PersistentVolumeClaim", "ServiceAccount")


def stage_and_render(chart: Path, component: str, workdir: Path) -> str:
    staged = workdir / component
    shutil.copytree(chart, staged)
    cm = staged / "Chart.yaml"
    cm.write_text(cm.read_text().replace("CHART_VERSION", "0.0.0-gate"))
    toolkit = chart.parent.parent.parent / "charts" / "helm-toolkit"
    if (staged / "Chart.yaml").read_text().find("helm-toolkit") != -1 and toolkit.is_dir():
        (staged / "charts").mkdir(exist_ok=True)
        if not (staged / "charts" / "helm-toolkit").exists():
            shutil.copytree(toolkit, staged / "charts" / "helm-toolkit")
    release = f"{component}-{RELEASE_SUFFIX}"
    r = subprocess.run(
        ["helm", "template", release, str(staged), "--namespace", "openstack"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:1500])
    return r.stdout


def pod_templates(doc):
    """Yield (owner, podSpec) for every pod template a rendered object carries."""
    kind = doc.get("kind")
    name = (doc.get("metadata") or {}).get("name", "?")
    spec = doc.get("spec") or {}
    owner = f"{kind}/{name}"
    if kind == "Pod":
        yield owner, spec
    elif kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
        yield owner, ((spec.get("template") or {}).get("spec") or {})
    elif kind == "CronJob":
        job = ((spec.get("jobTemplate") or {}).get("spec") or {})
        yield owner, ((job.get("template") or {}).get("spec") or {})


def references(pod):
    """Yield (kind, name, where) for every non-optional object a pod spec names."""
    for v in pod.get("volumes") or []:
        where = f"volume {v.get('name')}"
        s = v.get("secret")
        if s and not s.get("optional"):
            yield "Secret", s.get("secretName"), where
        c = v.get("configMap")
        if c and not c.get("optional"):
            yield "ConfigMap", c.get("name"), where
        p = v.get("persistentVolumeClaim")
        if p:
            yield "PersistentVolumeClaim", p.get("claimName"), where
        for src in (v.get("projected") or {}).get("sources") or []:
            ps, pc = src.get("secret"), src.get("configMap")
            if ps and not ps.get("optional"):
                yield "Secret", ps.get("name"), where + " (projected)"
            if pc and not pc.get("optional"):
                yield "ConfigMap", pc.get("name"), where + " (projected)"
    for c in (pod.get("containers") or []) + (pod.get("initContainers") or []):
        cname = c.get("name")
        for ef in c.get("envFrom") or []:
            sr, cr = ef.get("secretRef"), ef.get("configMapRef")
            if sr and not sr.get("optional"):
                yield "Secret", sr.get("name"), f"container {cname} envFrom"
            if cr and not cr.get("optional"):
                yield "ConfigMap", cr.get("name"), f"container {cname} envFrom"
        for e in c.get("env") or []:
            vf = e.get("valueFrom") or {}
            sk, ck = vf.get("secretKeyRef"), vf.get("configMapKeyRef")
            if sk and not sk.get("optional"):
                yield "Secret", sk.get("name"), f"container {cname} env {e.get('name')}"
            if ck and not ck.get("optional"):
                yield "ConfigMap", ck.get("name"), f"container {cname} env {e.get('name')}"
    sa = pod.get("serviceAccountName")
    if sa and sa != "default":
        yield "ServiceAccount", sa, "serviceAccountName"
    for ips in pod.get("imagePullSecrets") or []:
        yield "Secret", ips.get("name"), "imagePullSecrets"


def self_inconsistent(pod, claim_templates=()):
    """Yield (what, where) for faults inside one pod spec that the API server refuses.

    Both shipped (#179): aodh's alarms-cleaner CronJob mounted `aodh-etc-snippets` while its
    volume was spelled `aodh-et-etc-snippets`, so the CronJob was refused and the release
    failed; ceilometer's db-sync Job had no image, because values carried no
    images.tags.ceilometer_db_sync, so the Job was never created and every ceilometer pod waited
    on it. A render passes both, and the server only refuses them at apply time.
    """
    # A StatefulSet's volumeClaimTemplates are volumes too, though the pod spec does not list them.
    volumes = {v.get("name") for v in pod.get("volumes") or []} | set(claim_templates)
    for c in (pod.get("containers") or []) + (pod.get("initContainers") or []):
        cname = c.get("name")
        if not c.get("image"):
            yield "(no image)", f"container {cname} has no image"
        for m in c.get("volumeMounts") or []:
            if m.get("name") not in volumes:
                yield m.get("name"), f"container {cname} mounts volume {m.get('name')!r}, which the pod does not declare"


def check(component, rendered):
    docs = [d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]
    have = {(d.get("kind"), (d.get("metadata") or {}).get("name")) for d in docs}
    pods, dangling, known = 0, [], []
    for d in docs:
        claims = [(t.get("metadata") or {}).get("name")
                  for t in (d.get("spec") or {}).get("volumeClaimTemplates") or []]
        for owner, pod in pod_templates(d):
            pods += 1
            for what, where in self_inconsistent(pod, claims):
                dangling.append((owner, "pod spec", what, where))
            for kind, name, where in references(pod):
                if not name:
                    dangling.append((owner, kind, "(empty name)", where))
                    continue
                if (kind, name) in have or (component, kind, name) in ALLOWED:
                    continue
                if (component, kind, name) in KNOWN_DEFECTS:
                    known.append((owner, kind, name, where, KNOWN_DEFECTS[(component, kind, name)]))
                    continue
                dangling.append((owner, kind, name, where))
    return pods, dangling, known


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    only = set(sys.argv[2:])
    charts = sorted(p.parent for p in (root / "blueprints").glob("*/chart/Chart.yaml"))
    charts = [c for c in charts if c.parent.name != "openstack"]  # the umbrella renders no pods
    if only:
        charts = [c for c in charts if c.parent.name in only]
    if not charts:
        sys.exit("FAIL: no component charts found -- nothing was measured")

    total_pods, failures, unrendered = 0, [], []
    with tempfile.TemporaryDirectory() as td:
        for chart in charts:
            component = chart.parent.name
            try:
                rendered = stage_and_render(chart, component, Path(td))
            except RuntimeError as e:
                unrendered.append((component, str(e)))
                continue
            pods, dangling, known = check(component, rendered)
            total_pods += pods
            status = "OK  " if not dangling else "FAIL"
            print(f"{status} {component}: {pods} pod templates, {len(dangling)} dangling")
            for owner, kind, name, where, issue in known:
                print(f"KNOWN {component}: {owner} {where} names {kind} {name!r} ({issue})")
            for owner, kind, name, where in dangling:
                failures.append((component, owner, kind, name, where))

    for component, err in unrendered:
        print(f"FAIL {component}: did not render, so nothing was measured: {err}")
    if total_pods == 0:
        print("FAIL: no pod templates rendered in any chart -- nothing was measured")
        return 1
    if failures:
        print(f"\nDANGLING REFERENCES: {len(failures)} (release named <component>-{RELEASE_SUFFIX})")
        for component, owner, kind, name, where in failures:
            print(f"  {component}: {owner} {where} names {kind} {name!r}, which the chart does not render")
        print("\nA pod naming an object nobody creates waits forever (FailedMount, or a container")
        print("that never starts) and nothing reports it. Render it from the chart, or, if another")
        print("component creates it, add it to ALLOWED in this script with the reason.")
    if failures or unrendered:
        return 1
    print(f"\nOK: {len(charts)} charts, {total_pods} pod templates, every reference rendered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
