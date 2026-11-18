#!/usr/bin/env python3
"""A RabbitMQ user rotation switches to the new generation only once its user is ready, and retires the old one only once
every reader has rolled onto the new one (#309 C2, Diego's (d): dual users per generation).

    hack/validate-messaging-rotation.py [repo-root]

The procedure (README "Rotating a RabbitMQ service user"): set rotate.oslo-messaging-<service> to a new token. The umbrella
derives each step from what exists (osh.messagingRotation), never from remembered state:
  minting   the new generation root (C1) and the umbrella's projection of it; once that Secret exists, labelled, the
            component renders the new User next to the active one, on the same vhost (so queues do not move); once that
            User is Ready, the umbrella switches the component: passwordFrom and username name the new generation;
  draining  the previous User stays until every reader has rolled (osh.generationRolled), then leaves the component's
            release, which deletes it, and the topology operator deletes its RabbitMQ user;
  steady.

Sources the predicate rests on (read, not assumed):
  - kubectl v1.36.4 staging/src/k8s.io/kubectl/pkg/polymorphichelpers/rollout_status.go: Deployment :75-87
    (observedGeneration, ProgressDeadlineExceeded, updatedReplicas == spec.replicas, status.replicas == updatedReplicas,
    availableReplicas == updatedReplicas); DaemonSet :107-112 (OnDelete refused; updatedNumberScheduled and numberAvailable
    == desiredNumberScheduled); StatefulSet :130-146 (OnDelete refused; readyReplicas == spec.replicas; a partition
    accepted once updatedReplicas >= replicas - partition; updateRevision == currentRevision). This predicate is stricter
    on one point: a partition counts as not rolled while any pod is below it, since those pods hold the old credential.
  - each workload's pod-template annotation krateo.io/messaging-generation (helm-toolkit custom_pod_annotations): its
    pods are the new generation's. (Upstream krateoplatformops/composition-dynamic-controller main writes no
    observedGeneration; the fork core-provider deploys does, see below.)
  - the composition-dynamic-controller core-provider 2.13.111 deploys (krateo-platformops/core-provider tag 2.13.111,
    commit 7cd0848): status.observedGeneration is written only in setStatus (internal/composition/support.go:377;
    unstructured-runtime v1.27.1 statusprojection.go:177, = metadata.generation as read), after a successful reconcile
    (a failed one returns first, composition.go Observe :700). That reconcile (plumbing v1.95.0 helm/v3/reconcile.go
    steps 5-8) runs one real helm upgrade whenever anything changed, which runs and waits for the hooks. So
    observedGeneration == generation means the switching release was applied, its hook Jobs ended; hooks are not in
    status.managed and need no clock. krateo.io/hooks-unverified (#563) is reported.
  - ESO v2.14.0: status.syncedResourceVersion is "<metadata.generation>-<HashMeta>" (GetResourceVersion), so a projection
    synced at its current spec carries its own generation as prefix.
  - chart-inspector internal/tracer/tracer.go RoundTrip records every named request of the dry-run render, lookups and
    404s included, and derives the release's RBAC from them: every read here is one named GET (osh.lookupNamed).
  - messaging-topology-operator v1.20.5: the User webhook refuses an import Secret without the label
    rabbitmq.com/topology-operator=true (internal/webhook/v1beta1/label_validation.go): the new User is only rendered
    once its projection exists, labelled.

  1. THE STEPS. osh.messagingRotation probed for heat with osh.lookupNamed, osh.lookupSecret, osh.lookupExternalSecret
     stood in, and a status.managed built from heat's own operator-mode render (its Deployments,
     StatefulSets, DaemonSets, CronJobs and non-hook Jobs):
       a. no token: nothing passed (the component's spec does not change);
       b. a token, its projection not written yet: minting, the base user only;
       c. the projection written and labelled: the new user joins, not switched (its User is not Ready);
       d. the new User Ready: switched in that render; both users; the new suffix and username;
       e. after the switch, workloads still at the old pod template: draining, each named;
       f. every workload rolled, the Composition's observedGeneration behind (its release, hooks included, not applied
          yet), or krateo.io/hooks-unverified set: draining, saying which;
       g. all of it done: steady, the new user only;
       h. the previous User being deleted (deletionTimestamp) or gone: steady;
       i. a second token while the first still drains: the new generation waits (never three users);
       j. half-finished rollouts, one per kind and failure (Deployment x5, StatefulSet x4, DaemonSet x3, CronJob x2,
          Job, bare Pod, an unreadable workload, a projection reading the old root, one not synced, one missing, an
          empty status.managed): each keeps the previous user and names the object.
     None of the cases supplies openstack-credential-generations: losing it loses nothing a rotation needs.
  2. THE UMBRELLA RENDER. With the lookups stood in at step c: the generation projection (name, username, root, label,
     OnChange) and the ConfigMap openstack-credential-rotation-<id> (phase, blockers). The spec-building span
     (compositions.yaml, `$ov :=` to TWO PHASES) with each case's $rot: the component's auth username and passwordFrom
     (store and key kept), operator.users and activeSuffix; nothing at all with no rotation. The drift latch holds the
     generation root's sha256 by Secret name, and reports an edit of it.
  3. THE CHARTS. Each of the 20 messaging charts rendered with the umbrella's operator spec before and after a switch
     (from the span in 2, one $rot per chart):
       a. one User and one Permission per generation passed, importing that generation's projection, on one vhost;
       b. every entrypoint wait on a topology resource is granted; a User or Permission wait names the chart's own
          service's ACTIVE generation, never another service's User (only its vhost and the administrator's permission);
       c. every Deployment, StatefulSet, DaemonSet and CronJob carries krateo.io/messaging-generation: <active suffix>;
          every workload mounting a projection of the root changes its pod template on the switch (rolls); no non-hook
          Job changes (a Job's template is immutable);
       d. the ExternalSecrets reading the root are exactly osh.messagingReaders' names, and no other chart reads it;
       e. a pod restarting after generation 0 is gone (steady: the new user only): its init waits name the active
          generation's User and Permission, and generation 0's User is not rendered;
       f. without a rotation: no messaging-generation annotation and the generation-0 names only.
  4. MUTANTS, each of which must fail the part it breaks: the waits ignoring the suffix (heat); the waits naming another
     service's User (ceilometer); the pod annotation removed; the partition rule dropped; the hook bound dropped;
     switching without the new User Ready.

Fails when helm or PyYAML is missing, and when a part checks nothing.
"""
import copy
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
BP = os.path.join(ROOT, "blueprints")
UMBRELLA = os.path.join(BP, "openstack", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
NS = "openstack"
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

import importlib.util as _u
_s = _u.spec_from_file_location("gate_values", os.path.join(ROOT, "hack", "gate_values.py"))
gate_values = _u.module_from_spec(_s)
_s.loader.exec_module(gate_values)

failures = []
counts = {}


def fail(m):
    failures.append(m)


STANDINS = {
    "osh.lookupNamed": '{{- index ((index . 0).Values.gateObj | default dict) (printf "%s/%s" (index . 2) (index . 3)) '
                       '| default dict | toJson -}}',
    "osh.lookupSecret": '{{- index ((index . 0).Values.gateObj | default dict) (printf "Secret/%s" (index . 1)) '
                        '| default dict | toJson -}}',
    "osh.lookupExternalSecret": '{{- index ((index . 0).Values.gateObj | default dict) (printf "ExternalSecret/%s" '
                                '(index . 1)) | default dict | toJson -}}',
    "osh.lookupConfigMap": '{{- index ((index . 0).Values.gateObj | default dict) (printf "ConfigMap/%s" (index . 1)) '
                           '| default dict | toJson -}}',
}


def stage_umbrella(tmp, name, mutate=None):
    w = os.path.join(tmp, name)
    shutil.copytree(UMBRELLA, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    os.makedirs(os.path.join(w, "charts"), exist_ok=True)
    shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    hp = os.path.join(w, "templates", "_helpers.tpl")
    h = open(hp).read()
    for name_, body in STANDINS.items():
        rx = re.compile(r'(\{\{- define "%s" -\}\}).*?(\{\{- end -\}\})' % re.escape(name_), re.S)
        if not rx.search(h):
            print("FAILED MEASUREMENT: _helpers.tpl defines no %s, so it cannot be stood in for" % name_)
            sys.exit(2)
        h = rx.sub(lambda m: m.group(1) + "\n" + body + "\n" + m.group(2), h, count=1)
    if mutate:
        h = mutate(h)
    open(hp, "w").write(h)
    return w


def stage_chart(tmp, chart, name=None, toolkit_mutate=None):
    w = os.path.join(tmp, name or chart)
    if os.path.isdir(w):
        return w
    shutil.copytree(os.path.join(BP, chart, "chart"), w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    os.makedirs(os.path.join(w, "charts"), exist_ok=True)
    tk = os.path.join(w, "charts", "helm-toolkit")
    shutil.copytree(TOOLKIT, tk)
    if toolkit_mutate:
        toolkit_mutate(tk)
    return w


def helm(chart_dir, release, vals, extra=(), umbrella=False):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(vals or {}, f)
    try:
        args = ["helm", "template", release, chart_dir, "-n", NS, "--kube-version", "1.36.0"]
        if not umbrella:
            args += list(gate_values.helm_args(chart_dir))
        return subprocess.run(args + ["-f", f.name] + list(extra), capture_output=True, text=True)
    finally:
        os.unlink(f.name)


BASE_VALS = {"profile": "full", "messaging": {"backend": "operator"},
             "componentValues": {"rabbitmq": {"pod": {"replicas": {"server": 3}}}}}


def merged(a, b):
    out = json.loads(json.dumps(a))
    for k, v in b.items():
        out[k] = merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


UV = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
CHARTS = sorted((UV.get("haTopology") or {}).get("messagingQuorum") or [])
if not CHARTS:
    print("FAILED MEASUREMENT: haTopology.messagingQuorum is empty")
    sys.exit(2)


def base_root(c):
    return next(i["secret"] for i in UV["credentials"]["items"] if i["id"] == "oslo-messaging-%s" % c)


def gen(c, tok):
    return "%s-g%s" % (base_root(c), hashlib.sha256(tok.encode()).hexdigest()[:8])


def sfx(c, root):
    return "" if root == base_root(c) else root[len(base_root(c)):]


REFRESH = "2033-05-18T03:33:20Z"


# ---------------------------------------------------------------------------------------------------------------------
# Spec-building span probe: the umbrella's operator spec per chart, with a $rot per chart (None: no rotation).
# ---------------------------------------------------------------------------------------------------------------------
def span_specs(tmp, rots, tag):
    comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
    start = comp.find("{{- $ov := ")
    end = comp.find("{{- /* TWO PHASES", start)
    if start < 0 or end < 0 or "$rot.pass" not in comp[start:end]:
        fail("2: compositions.yaml's spec-building span (`$ov :=` to TWO PHASES) does not hold the rotation span")
        return None
    w = stage_umbrella(tmp, "span-" + tag)
    open(os.path.join(w, "templates", "zz-gate-rot-span.yaml"), "w").write(
        '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
        '{{- range $c := .Values.components }}\n'
        '{{- $rot := index ($.Values.gateRot | default dict) $c.name | default dict }}\n'
        + comp[start:end] + '\n'
        '{{ printf "\\n# ROTSPEC %s %s %s\\n" $c.name ($ov | toJson) (include "osh.messagingReaders" (list $c)) }}\n'
        '{{- end }}\n')
    r = helm(w, "openstack-gate", merged(BASE_VALS, {"gateRot": rots}),
             ("--show-only", "templates/zz-gate-rot-span.yaml"), umbrella=True)
    if r.returncode != 0:
        fail("2: the spec-building span does not render (%s): %s" % (tag, r.stderr.strip()[:300]))
        return None
    out = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^# ROTSPEC (\S+) (\{.*\}) (\[.*\])$", line)
        if m:
            out[m.group(1)] = (json.loads(m.group(2)), json.loads(m.group(3)))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Part 1: the steps, probed.
# ---------------------------------------------------------------------------------------------------------------------
def heat_fixture(tmp, spec):
    """status.managed and rolled live objects from heat's own operator render."""
    w = stage_chart(tmp, "heat")
    r = helm(w, "heat", spec)
    if r.returncode != 0:
        fail("1: heat does not render with the operator spec: %s" % r.stderr.strip()[:300])
        return None, None
    managed, objs = [], {}
    plural = {"Deployment": "deployments", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets",
              "CronJob": "cronjobs", "Job": "jobs"}
    for d in yaml.safe_load_all(r.stdout):
        if not d or d.get("kind") not in plural:
            continue
        if d["kind"] == "Job" and (d["metadata"].get("annotations") or {}).get("helm.sh/hook"):
            continue   # hooks are not in the release manifest, so not in status.managed
        managed.append({"apiVersion": d["apiVersion"], "resource": plural[d["kind"]], "name": d["metadata"]["name"],
                        "namespace": NS})
        objs[(d["kind"], d["metadata"]["name"])] = d
    return managed, objs


def rolled_obj(d, suffix):
    o = copy.deepcopy(d)
    o["metadata"]["generation"] = 3
    k = o["kind"]
    tpl = o["spec"]["jobTemplate"]["spec"]["template"] if k == "CronJob" else o["spec"].get("template", {})
    if k != "Job":
        tpl.setdefault("metadata", {}).setdefault("annotations", {})["krateo.io/messaging-generation"] = suffix
        if not suffix:
            tpl["metadata"]["annotations"].pop("krateo.io/messaging-generation")
    rep = int(o["spec"].get("replicas", 1))
    if k == "Deployment":
        o["status"] = {"observedGeneration": 3, "replicas": rep, "updatedReplicas": rep, "availableReplicas": rep,
                       "readyReplicas": rep}
    elif k == "StatefulSet":
        o["status"] = {"observedGeneration": 3, "replicas": rep, "readyReplicas": rep, "updatedReplicas": rep,
                       "currentRevision": "r2", "updateRevision": "r2"}
    elif k == "DaemonSet":
        o["status"] = {"observedGeneration": 3, "desiredNumberScheduled": 3, "updatedNumberScheduled": 3,
                       "numberAvailable": 3}
    elif k == "CronJob":
        o["status"] = {}
    elif k == "Job":
        o["status"] = {"succeeded": 1}
    return o


def es_obj(name, root, gen_=4, synced=True, refresh=REFRESH):
    return {"metadata": {"name": name, "generation": gen_},
            "spec": {"data": [{"secretKey": "password", "remoteRef": {"key": root, "property": "password"}}]},
            "status": {"syncedResourceVersion": ("%d-abc" % gen_) if synced else "%d-abc" % (gen_ - 1),
                       "refreshTime": refresh,
                       "conditions": [{"type": "Ready", "status": "True"}]}}


def user_obj(ready=True, deleting=False):
    o = {"metadata": {"name": "u", "generation": 1}, "status": {"conditions": [
        {"type": "Ready", "status": "True" if ready else "False"}]}}
    if deleting:
        o["metadata"]["deletionTimestamp"] = "2033-05-18T03:00:00Z"
    return o


def labelled_secret():
    return {"metadata": {"labels": {"rabbitmq.com/topology-operator": "true"}}, "data": {"password": "eA=="}}


def live(c, root):
    return {"endpoints": {"oslo_messaging": {"auth": {c: {"passwordFrom": {"secret": root}}}}}}


def probe(w, cases):
    """cases: [(name, vals-overlay, live, managed)] -> {name: rot or error}"""
    open(os.path.join(w, "templates", "zz-gate-rot-probe.yaml"), "w").write(
        '{{- range $case := .Values.gateCases }}\n'
        '{{- range $c := $.Values.components }}{{- if eq $c.name "heat" }}\n'
        '{{- $v := merge (deepCopy $case.vals) (deepCopy $.Values) }}\n'
        '{{ printf "\\n# ROT %s %s\\n" $case.name (include "osh.messagingRotation" '
        '(list (dict "Values" $v "Release" $.Release) $c ($case.live | default dict) (dict "managed" ($case.managed | default list) '
        '"generation" ($case.vals.gateGen | default 5) "observedGeneration" ($case.vals.gateObs | default 5) '
        '"hooksUnverified" ($case.vals.gateHU | default false)))) }}\n'
        '{{- end }}{{- end }}\n{{- end }}\n')
    vals = merged(BASE_VALS, {"gateCases": [{"name": n, "vals": v, "live": lv, "managed": m} for n, v, lv, m in cases]})
    r = helm(w, "openstack-gate", vals, ("--show-only", "templates/zz-gate-rot-probe.yaml"), umbrella=True)
    out = {}
    if r.returncode != 0:
        return None, r.stderr.strip()[:400]
    for line in r.stdout.splitlines():
        m = re.match(r"^# ROT (\S+) (.*)$", line)
        if m:
            out[m.group(1)] = json.loads(m.group(2))
    return out, None


def part1(tmp, w=None, only=None):
    """Returns (cases_passed, {case: problems}) so a mutant can ask which cases broke."""
    c = "heat"
    b0, g1, g2 = base_root(c), gen(c, "t1"), gen(c, "t2")
    s1, s2 = sfx(c, g1), sfx(c, g2)
    proj = "heat-rabbitmq-topology-user"
    spec = (SPECS0 or {}).get(c, ({}, []))[0]
    managed, objs = FIXTURE
    if managed is None:
        return 0, {"fixture": ["no fixture"]}
    w = w or stage_umbrella(tmp, "probe")
    tok = lambda t: {"rotate": {"oslo-messaging-heat": t}}

    def world(active_sfx, **kw):
        """the cluster after a switch to active_sfx: every workload rolled, projections synced at REFRESH."""
        g = {}
        for (k, n), d in objs.items():
            g["%s/%s" % (k, n)] = rolled_obj(d, active_sfx)
        return g

    def es_world(root):
        return {"ExternalSecret/heat-rabbitmq-user": es_obj("heat-rabbitmq-user", root),
                "ExternalSecret/heat-transport": es_obj("heat-transport", root)}

    stamp = lambda prev: {"metadata": {"annotations": {"krateo.io/previous-root": prev}}}
    users = lambda *ss: [{"suffix": s, "secret": proj + s} for s in ss]
    after = dict(world(s1), **es_world(g1))
    after.update({"ExternalSecret/" + g1: stamp(b0), "User/heat-rabbitmq-user": user_obj(),
                  "User/heat-rabbitmq-user" + s1: user_obj(), "Secret/" + proj + s1: labelled_secret()})

    def mut(world_, key, f):
        g = copy.deepcopy(world_)
        f(g[key])
        return g

    def first(kind):
        for (k, n) in sorted(objs):
            if k == kind:
                return "%s/%s" % (k, n)
        return None

    dep, sts, ds, cj, job = (first(k) for k in ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job"))
    cases = [
        # name, overlay, live, managed, want
        ("a", {}, {}, [], {"pass": False, "phase": "steady"}),
        ("b", dict(tok("t1"), gateObj={}), {}, [],
         {"phase": "minting", "users": users(""), "roots": [g1], "activeSuffix": "", "switched": None,
          "blocker": "waiting for External Secrets"}),
        ("c", dict(tok("t1"), gateObj={"Secret/" + proj + s1: labelled_secret()}), {}, [],
         {"phase": "minting", "users": users("", s1), "activeSuffix": "", "switched": None,
          "blocker": "to be Ready"}),
        ("d", dict(tok("t1"), gateObj={"Secret/" + proj + s1: labelled_secret(),
                                       "User/heat-rabbitmq-user" + s1: user_obj()}), {}, [],
         {"phase": "draining", "users": users("", s1), "activeSuffix": s1, "activeRoot": g1, "switched": True,
          "username": "heat" + s1}),
        ("e", dict(tok("t1"), gateObj=dict(after, **world(""))), live(c, g1), managed,
         {"phase": "draining", "users": users("", s1), "activeSuffix": s1, "blocker": "its pod template is at generation"}),
        # the switching release not applied yet, hooks included (Composition observedGeneration behind)
        ("f", dict(tok("t1"), gateObj=after, gateObs=4), live(c, g1), managed,
         {"phase": "draining", "users": users("", s1), "blocker": "has not been applied yet, hooks included"}),
        ("f2", dict(tok("t1"), gateObj=after, gateHU=True), live(c, g1), managed,
         {"phase": "draining", "users": users("", s1), "blocker": "hooks-unverified"}),
        ("g", dict(tok("t1"), gateObj=after), live(c, g1), managed,
         {"phase": "steady", "users": users(s1), "activeSuffix": s1, "roots": [g1], "blockers": []}),
        ("h1", dict(tok("t1"), gateObj=dict(after, **{"User/heat-rabbitmq-user": user_obj(deleting=True)})),
         live(c, g1), managed, {"phase": "steady", "users": users(s1)}),
        ("h2", dict(tok("t1"), gateObj={k: v for k, v in after.items() if k != "User/heat-rabbitmq-user"}),
         live(c, g1), managed, {"phase": "steady", "users": users(s1)}),
        ("i1", dict(tok("t2"), gateObj=after, gateObs=4), live(c, g1), managed,
         {"phase": "minting", "users": users("", s1), "activeSuffix": s1, "roots": [g1, g2],
          "blocker": "waits until the previous rotation has drained"}),
        ("i2", dict(tok("t2"), gateObj=dict(after, **{"Secret/" + proj + s2: labelled_secret()})),
         live(c, g1), managed, {"phase": "minting", "users": users(s1, s2), "activeSuffix": s1}),
    ]
    # j. half-finished rollouts: each from the rolled world of g, broken one way.
    halves = []
    if dep:
        halves += [
            ("jd1", dep, lambda o: o["status"].update(updatedReplicas=0), "pods updated"),
            ("jd2", dep, lambda o: o["status"].update(replicas=o["status"]["replicas"] + 1), "old pods pending"),
            ("jd3", dep, lambda o: o["status"].update(availableReplicas=0), "updated pods available"),
            ("jd4", dep, lambda o: o["metadata"].update(generation=4), "not observed yet"),
            ("jd5", dep, lambda o: o["status"].update(conditions=[{"type": "Progressing", "status": "False",
                                                                   "reason": "ProgressDeadlineExceeded"}]),
             "progress deadline"),
        ]
    sts_key = sts or "StatefulSet/heat-gate-sts"
    ds_key = ds or "DaemonSet/heat-gate-ds"
    extra_managed = []
    extra_objs = {}
    if not sts:   # heat renders none by default: a synthetic one, so the StatefulSet branch is still exercised
        extra_managed.append({"apiVersion": "apps/v1", "resource": "statefulsets", "name": "heat-gate-sts"})
        extra_objs[sts_key] = rolled_obj({"apiVersion": "apps/v1", "kind": "StatefulSet",
                                          "metadata": {"name": "heat-gate-sts"},
                                          "spec": {"replicas": 3, "template": {"metadata": {}}}}, s1)
    if not ds:
        extra_managed.append({"apiVersion": "apps/v1", "resource": "daemonsets", "name": "heat-gate-ds"})
        extra_objs[ds_key] = rolled_obj({"apiVersion": "apps/v1", "kind": "DaemonSet",
                                         "metadata": {"name": "heat-gate-ds"},
                                         "spec": {"template": {"metadata": {}}}}, s1)
    cj_key = cj or "CronJob/heat-gate-cj"
    if not cj:
        extra_managed.append({"apiVersion": "batch/v1", "resource": "cronjobs", "name": "heat-gate-cj"})
        extra_objs[cj_key] = rolled_obj({"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "heat-gate-cj"},
                                         "spec": {"jobTemplate": {"spec": {"template": {"metadata": {}}}}}}, s1)
    job_key = job or "Job/heat-gate-job"
    if not job:
        extra_managed.append({"apiVersion": "batch/v1", "resource": "jobs", "name": "heat-gate-job"})
        extra_objs[job_key] = rolled_obj({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "heat-gate-job"},
                                          "spec": {"template": {"metadata": {}}}}, s1)
    extra_managed.append({"apiVersion": "v1", "resource": "pods", "name": "heat-gate-pod"})
    extra_objs["Pod/heat-gate-pod"] = {"metadata": {"name": "heat-gate-pod"}, "status": {"phase": "Succeeded"}}
    extra_managed.append({"apiVersion": "apps/v1", "resource": "deployments", "name": "heat-gate-gone-check"})
    extra_objs["Deployment/heat-gate-gone-check"] = rolled_obj(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "heat-gate-gone-check"},
         "spec": {"replicas": 1, "template": {"metadata": {}}}}, s1)
    full_managed = managed + extra_managed
    full_after = dict(after, **extra_objs)
    halves += [
        ("js1", sts_key, lambda o: o["spec"].update(updateStrategy={"type": "OnDelete"}), "updateStrategy OnDelete"),
        ("js2", sts_key, lambda o: (o["spec"].update(updateStrategy={"type": "RollingUpdate",
                                                                     "rollingUpdate": {"partition": 2}}),
                                    o["status"].update(updatedReplicas=1)), "partition 2"),
        ("js3", sts_key, lambda o: o["status"].update(updateRevision="r3"), "update revision"),
        ("js4", sts_key, lambda o: o["status"].update(readyReplicas=0), "pods ready"),
        ("jds1", ds_key, lambda o: o["spec"].update(updateStrategy={"type": "OnDelete"}), "updateStrategy OnDelete"),
        ("jds2", ds_key, lambda o: o["status"].update(updatedNumberScheduled=1), "pods updated"),
        ("jds3", ds_key, lambda o: o["status"].update(numberAvailable=1), "updated pods available"),
        ("jc1", cj_key, lambda o: o["status"].update(active=[{"name": "x"}]), "Job(s) running"),
        ("jc2", cj_key, lambda o: o["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["annotations"].update(
            {"krateo.io/messaging-generation": ""}), "its pod template is at generation"),
        ("jj", job_key, lambda o: o["status"].update(active=1), "running, and keeps"),
        ("jp", "Pod/heat-gate-pod", lambda o: o["status"].update(phase="Running"), "a bare Pod"),
        ("jes1", "ExternalSecret/heat-transport", lambda o: o["spec"]["data"][0]["remoteRef"].update(key=b0),
         "does not read"),
        ("jes2", "ExternalSecret/heat-rabbitmq-user", lambda o: o["status"].update(syncedResourceVersion="3-x"),
         "not synced at its generation"),
    ]
    for name, key, f, needle in halves:
        cases.append((name, dict(tok("t1"), gateObj=mut(full_after, key, f)), live(c, g1), full_managed,
                      {"phase": "draining", "users": users("", s1), "blocker": needle, "names": key.split("/", 1)[1]}))
    cases.append(("jgone", dict(tok("t1"), gateObj={k: v for k, v in full_after.items()
                                                    if k != "Deployment/heat-gate-gone-check"}),
                  live(c, g1), full_managed, {"phase": "draining", "blocker": "cannot be read", "names": "heat-gate-gone-check"}))
    cases.append(("jesgone", dict(tok("t1"), gateObj={k: v for k, v in full_after.items()
                                                      if k != "ExternalSecret/heat-transport"}),
                  live(c, g1), full_managed, {"phase": "draining", "blocker": "cannot be read", "names": "heat-transport"}))
    cases.append(("jempty", dict(tok("t1"), gateObj=full_after), live(c, g1), [],
                  {"phase": "draining", "blocker": "lists no status.managed"}))
    cases.append(("jall", dict(tok("t1"), gateObj=full_after), live(c, g1), full_managed,
                  {"phase": "steady", "users": users(s1), "blockers": []}))
    if only:
        cases = [x for x in cases if x[0] in only]
    got, err = probe(w, [(n, v, lv, m) for n, v, lv, m, _ in cases])
    broke = {}
    if got is None:
        return 0, {"render": [err]}
    n = 0
    for name, _, _, _, want in cases:
        r = got.get(name)
        bad = []
        if r is None:
            bad.append("no result")
        else:
            blk = " | ".join(r.get("blockers") or [])
            for k, v in want.items():
                if k == "blocker":
                    if v not in blk:
                        bad.append("blockers %r, want one saying %r" % (blk[:200], v))
                elif k == "names":
                    if v not in blk:
                        bad.append("blockers do not name %s" % v)
                elif r.get(k) != v:
                    bad.append("%s %r, want %r" % (k, r.get(k), v))
            if r.get("refuse"):
                bad.append("refused: %s" % r["refuse"])
        if bad:
            broke[name] = bad
        else:
            n += 1
    return n, broke


# ---------------------------------------------------------------------------------------------------------------------
# Part 2: the umbrella render and the span.
# ---------------------------------------------------------------------------------------------------------------------
def part2(tmp):
    n = 0
    c = "heat"
    b0, g1 = base_root(c), gen(c, "t1")
    s1 = sfx(c, g1)
    proj = "heat-rabbitmq-topology-user"
    w = stage_umbrella(tmp, "full")
    r = helm(w, "openstack-gate", merged(BASE_VALS, {"rotate": {"oslo-messaging-heat": "t1"},
                                                     "gateObj": {"Secret/" + proj + s1: labelled_secret()}}),
             umbrella=True)
    if r.returncode != 0:
        fail("2: the umbrella does not render at step c: %s" % r.stderr.strip()[:300])
        return 0
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == proj + s1]
    if len(es) != 1:
        fail("2: want one generation projection %s, got %d" % (proj + s1, len(es)))
    else:
        e = es[0]["spec"]
        probs = []
        if e.get("refreshPolicy") != "OnChange" or "refreshInterval" in e:
            probs.append("refresh %r/%r" % (e.get("refreshPolicy"), e.get("refreshInterval")))
        if e["target"]["template"]["data"].get("username") != "heat" + s1:
            probs.append("username %r" % e["target"]["template"]["data"].get("username"))
        if e["data"][0]["remoteRef"]["key"] != g1:
            probs.append("root %r" % e["data"][0]["remoteRef"]["key"])
        if (e["target"]["template"].get("metadata") or {}).get("labels", {}).get("rabbitmq.com/topology-operator") != "true":
            probs.append("not labelled for the topology operator")
        if probs:
            fail("2: the generation projection %s: %s" % (proj + s1, ", ".join(probs)))
        else:
            n += 1
    base = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == proj]
    if len(base) != 1 or base[0]["spec"]["data"][0]["remoteRef"]["key"] != b0:
        fail("2: the generation-0 projection %s must still render once, from %s" % (proj, b0))
    else:
        n += 1
    cm = [d for d in docs if d.get("kind") == "ConfigMap"
          and d["metadata"]["name"] == "openstack-credential-rotation-oslo-messaging-heat"]
    if len(cm) != 1 or cm[0]["data"].get("phase") != "minting" or "to be Ready" not in cm[0]["data"].get("blockers", ""):
        fail("2: want ConfigMap openstack-credential-rotation-oslo-messaging-heat, phase minting, naming the User it "
             "waits on; got %s" % [x.get("data") for x in cm])
    else:
        n += 1
    if any(d.get("kind") == "ConfigMap" and d["metadata"]["name"].startswith("openstack-credential-rotation-")
           and d["metadata"]["name"] != "openstack-credential-rotation-oslo-messaging-heat" for d in docs):
        fail("2: a rotation ConfigMap for a component with no token")
    # The drift latch covers generation roots: g1's data latched by Secret name, and an edit of it reported.
    data = {"password": "bmV3"}
    sha = hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()
    for label, baseline, want_drift in (("latched", None, False), ("edited", "0" * 64, True)):
        objs = {"Secret/" + proj + s1: labelled_secret(), "Secret/" + g1: {"data": data}}
        if baseline:
            objs["ConfigMap/openstack-credential-generations"] = {"data": {g1: baseline}}
        r = helm(w, "openstack-gate", merged(BASE_VALS, {"rotate": {"oslo-messaging-heat": "t1"}, "gateObj": objs}),
                 umbrella=True)
        if r.returncode != 0:
            fail("2: drift (%s): the umbrella does not render: %s" % (label, r.stderr.strip()[:200]))
            continue
        dd = [d for d in yaml.safe_load_all(r.stdout) if d and d.get("kind") == "ConfigMap"]
        state = next((d.get("data") or {} for d in dd if d["metadata"]["name"] == "openstack-credential-generations"), {})
        drift = [d for d in dd if d["metadata"]["name"] == "openstack-credential-drift-" + g1]
        if state.get(g1) != (baseline or sha):
            fail("2: drift (%s): the latch holds %r for %s, want %r" % (label, state.get(g1), g1, baseline or sha))
        elif bool(drift) != want_drift or (drift and drift[0]["data"].get("sha256") != sha):
            fail("2: drift (%s): want %s drift report for %s, got %s" % (
                label, "a" if want_drift else "no", g1, [x.get("data") for x in drift]))
        else:
            n += 1
    # The span, with and without a rotation.
    rot = {"pass": True, "username": "heat" + s1, "activeRoot": g1, "activeSuffix": s1,
           "users": [{"suffix": "", "secret": proj}, {"suffix": s1, "secret": proj + s1}]}
    sp = span_specs(tmp, {c: rot}, "heat")
    if sp and c in sp:
        om = (sp[c][0].get("endpoints") or {}).get("oslo_messaging") or {}
        a = (om.get("auth") or {}).get(c) or {}
        pf = a.get("passwordFrom") or {}
        probs = []
        if a.get("username") != "heat" + s1:
            probs.append("username %r" % a.get("username"))
        if pf.get("secret") != g1 or not pf.get("store") or not pf.get("key"):
            probs.append("passwordFrom %r" % pf)
        if (om.get("operator") or {}).get("users") != rot["users"] or (om.get("operator") or {}).get("activeSuffix") != s1:
            probs.append("operator %r" % om.get("operator"))
        if (om.get("operator") or {}).get("cluster") is None:
            probs.append("the operator cluster was lost in the merge")
        if probs:
            fail("2: the span with a rotation: %s" % ", ".join(probs))
        else:
            n += 1
        for other, (s, _) in sp.items():
            o = ((s.get("endpoints") or {}).get("oslo_messaging") or {}).get("operator") or {}
            if other != c and ("users" in o or "activeSuffix" in o):
                fail("2: %s has no rotation but its spec carries operator users/activeSuffix" % other)
    sp0 = SPECS0
    if sp0 and c in sp0:
        om = (sp0[c][0].get("endpoints") or {}).get("oslo_messaging") or {}
        if "users" in (om.get("operator") or {}) or "activeSuffix" in (om.get("operator") or {}) or \
                ((om.get("auth") or {}).get(c) or {}).get("passwordFrom", {}).get("secret") != b0:
            fail("2: without a rotation the span changes heat's spec: %r" % om)
        else:
            n += 1
    return n


# ---------------------------------------------------------------------------------------------------------------------
# Part 3: the charts.
# ---------------------------------------------------------------------------------------------------------------------
def pods(docs):
    for d in docs:
        sp = d.get("spec") or {}
        if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
            yield d, sp.get("template") or {}
        elif d.get("kind") == "CronJob":
            yield d, ((sp.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}


def secret_refs(pod):
    names = set()
    for v in pod.get("volumes") or []:
        if "secret" in v:
            names.add(v["secret"].get("secretName"))
        for s in (v.get("projected") or {}).get("sources") or []:
            if "secret" in s:
                names.add(s["secret"].get("name"))
    for ct in (pod.get("containers") or []) + (pod.get("initContainers") or []):
        for e in ct.get("env") or []:
            r = ((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
            if r:
                names.add(r)
        for ef in ct.get("envFrom") or []:
            r = (ef.get("secretRef") or {}).get("name")
            if r:
                names.add(r)
    return names


def waits_of(pod):
    for ct in pod.get("initContainers") or []:
        for e in ct.get("env") or []:
            if e.get("name") == "DEPENDENCY_CUSTOM_RESOURCE" and e.get("value"):
                yield from json.loads(e["value"])


def grants(docs):
    roles = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Role"}
    out = {}
    for b in (d for d in docs if d.get("kind") == "RoleBinding"):
        role = roles.get(b["roleRef"]["name"])
        for s in (b.get("subjects") or []) if role else []:
            for r in role.get("rules") or []:
                if "get" in (r.get("verbs") or []):
                    for g in r.get("apiGroups") or []:
                        for res in r.get("resources") or []:
                            for nm in r.get("resourceNames") or ["*"]:
                                out.setdefault(s["name"], set()).add((g, res, nm))
    return out


def chart_rot(c):
    g1 = gen(c, "t1")
    s1 = sfx(c, g1)
    proj = "%s-rabbitmq-topology-user" % c.replace("_", "-")
    user = c
    return g1, s1, {"pass": True, "username": user + s1, "activeRoot": g1, "activeSuffix": s1,
                    "users": [{"suffix": "", "secret": proj}, {"suffix": s1, "secret": proj + s1}]}


def for_chart(w, spec):
    """The umbrella's spec as a chart render takes it: where hack/gate_values.py supplies what the umbrella delivers for
    storage (#308: cinder refuses its default), its storage backend comes from there, not from this offline span."""
    spec = copy.deepcopy(spec)
    if list(gate_values.helm_args(w)):
        (spec.get("krateo") or {}).pop("storageBackend", None)
    return spec


def check_chart(tmp, c, spec_a, spec_b, readers, tag="", toolkit_mutate=None):
    """-> list of (check, problem)"""
    probs = []
    w = stage_chart(tmp, c, name=c + tag, toolkit_mutate=toolkit_mutate)
    spec_a, spec_b = for_chart(w, spec_a), for_chart(w, spec_b)
    ra = helm(w, c, spec_a)
    rb = helm(w, c, spec_b)
    if ra.returncode or rb.returncode:
        return [("render", (ra.stderr or rb.stderr).strip()[:300])]
    da = [d for d in yaml.safe_load_all(ra.stdout) if d]
    db = [d for d in yaml.safe_load_all(rb.stdout) if d]
    b0 = base_root(c)
    g1, s1, rot = chart_rot(c)
    su = c.replace("_", "-")
    # a. users and permissions per generation
    want_users = {("%s-rabbitmq-user%s" % (su, u["suffix"])): u["secret"] for u in rot["users"]}
    got_users = {d["metadata"]["name"]: d["spec"]["importCredentialsSecret"]["name"] for d in db
                 if d.get("kind") == "User" and d.get("apiVersion", "").startswith("rabbitmq.com/")}
    if got_users != want_users:
        probs.append(("3a", "Users %r, want %r" % (got_users, want_users)))
    perms = [d for d in db if d.get("kind") == "Permission" and d.get("apiVersion", "").startswith("rabbitmq.com/")
             and d["spec"].get("userReference")]
    pu = {d["spec"]["userReference"]["name"] for d in perms}
    if pu != set(want_users):
        probs.append(("3a", "Permissions for %r, want %r" % (sorted(pu), sorted(want_users))))
    if len({d["spec"]["vhost"] for d in perms}) != 1:
        probs.append(("3a", "Permissions on more than one vhost: %r" % sorted({d["spec"]["vhost"] for d in perms})))
    # b. waits name the active generation, and are granted
    g = grants(db)
    for d, t in pods(db):
        sa = (t.get("spec") or {}).get("serviceAccountName")
        for wt in waits_of(t.get("spec") or {}):
            if not str(wt.get("apiVersion", "")).startswith("rabbitmq.com/"):
                continue
            nm = wt["name"]
            if ("rabbitmq.com", wt.get("resource"), nm) not in g.get(sa, set()):
                probs.append(("3b", "%s/%s waits on %s without a grant" % (d["kind"], d["metadata"]["name"], nm)))
            if wt.get("kind") not in ("User", "Permission") or nm.endswith("-rabbitmq-admin-permission"):
                continue
            # its own service's User and Permission: the ACTIVE generation; another service's: never (its generation
            # is not this chart's to know; it waits on that vhost and the administrator's permission instead)
            if nm not in ("%s-rabbitmq-user%s" % (su, s1), "%s-rabbitmq-permission%s" % (su, s1)):
                probs.append(("3b", "%s/%s waits on %s: only its own service's active generation (%s) may be waited on"
                              % (d["kind"], d["metadata"]["name"], nm, s1)))
    # c. annotation, rolls, immutable Jobs
    root_es_b = {d["metadata"]["name"] for d in db if d.get("kind") == "ExternalSecret" and any(
        ((x.get("remoteRef") or {}).get("key") == g1) for x in (d["spec"].get("data") or []))}
    root_es_a = {d["metadata"]["name"] for d in da if d.get("kind") == "ExternalSecret" and any(
        ((x.get("remoteRef") or {}).get("key") == b0) for x in (d["spec"].get("data") or []))}
    ta = {(d["kind"], d["metadata"]["name"]): (d, t) for d, t in pods(da)}
    for d, t in pods(db):
        k = (d["kind"], d["metadata"]["name"])
        ann = ((t.get("metadata") or {}).get("annotations") or {}).get("krateo.io/messaging-generation")
        hook = (d["metadata"].get("annotations") or {}).get("helm.sh/hook")
        if d["kind"] != "Job" and ann != s1:
            probs.append(("3c", "%s/%s carries krateo.io/messaging-generation %r, want %r" % (k + (ann, s1))))
        before = ta.get(k)
        changed = before is None or json.dumps(before[1], sort_keys=True) != json.dumps(t, sort_keys=True)
        if d["kind"] == "Job" and not hook and changed:
            probs.append(("3c", "non-hook Job/%s changes its (immutable) pod template on the switch" % k[1]))
        if d["kind"] != "Job" and (secret_refs(t.get("spec") or {}) & root_es_b) and not changed:
            probs.append(("3c", "%s/%s mounts a projection of the root but does not roll on the switch" % k))
    # d. readers
    if root_es_a != set(readers) or root_es_b != set(readers):
        probs.append(("3d", "the ExternalSecrets reading the root are %r (before) / %r (after), osh.messagingReaders "
                            "says %r" % (sorted(root_es_a), sorted(root_es_b), sorted(readers))))
    # f. nothing without a rotation
    for d, t in pods(da):
        if "krateo.io/messaging-generation" in ((t.get("metadata") or {}).get("annotations") or {}):
            probs.append(("3f", "without a rotation %s/%s carries krateo.io/messaging-generation" % (
                d["kind"], d["metadata"]["name"])))
    ua = {d["metadata"]["name"] for d in da if d.get("kind") == "User" and d.get("apiVersion", "").startswith("rabbitmq.com/")}
    if ua != {"%s-rabbitmq-user" % su}:
        probs.append(("3f", "without a rotation the Users are %r" % sorted(ua)))
    others = {base_root(x) for x in ROTATING if x != c}
    other = {(d["metadata"]["name"], x["remoteRef"]["key"]) for d in da if d.get("kind") == "ExternalSecret"
             for x in (d["spec"].get("data") or []) if (x.get("remoteRef") or {}).get("key") in others}
    if other:
        probs.append(("3d", "reads another component's messaging root: %r" % sorted(other)))
    return probs


def part3(tmp):
    rots = {c: chart_rot(c)[2] for c in ROTATING}
    sb = span_specs(tmp, rots, "all")
    if not sb or not SPECS0:
        return 0
    n = 0
    for c in ROTATING:
        if c not in sb or c not in SPECS0:
            fail("3: no umbrella spec for %s" % c)
            continue
        probs = check_chart(tmp, c, SPECS0[c][0], sb[c][0], sb[c][1])
        for k, p in probs:
            fail("%s: %s: %s" % (k, c, p))
        if not probs:
            n += 1
    counts["specB"] = sb
    # e. generation 0 gone (steady after a rotation: the active user only): a restarting pod waits on the active
    #    generation's User and Permission, and nothing renders or waits on generation 0's User.
    steady = {}
    for c in ROTATING:
        g1, s1, rot = chart_rot(c)
        rot = dict(rot, users=[u for u in rot["users"] if u["suffix"]])
        steady[c] = rot
    ss = span_specs(tmp, steady, "steady")
    n_e = 0
    for c in ROTATING if ss else []:
        su = c.replace("_", "-")
        s1 = chart_rot(c)[1]
        w = stage_chart(tmp, c)
        r = helm(w, c, for_chart(w, ss[c][0]))
        if r.returncode:
            fail("3e: %s does not render after generation 0 is gone: %s" % (c, r.stderr.strip()[:200]))
            continue
        docs = [d for d in yaml.safe_load_all(r.stdout) if d]
        users = {d["metadata"]["name"] for d in docs if d.get("kind") == "User"
                 and d.get("apiVersion", "").startswith("rabbitmq.com/")}
        waits = [(d["metadata"]["name"], wt["name"]) for d, t in pods(docs) for wt in waits_of(t.get("spec") or {})
                 if wt.get("kind") in ("User", "Permission") and not wt["name"].endswith("-admin-permission")
                 and wt["name"].startswith(su + "-rabbitmq-")]
        bad = [x for x in waits if x[1] not in ("%s-rabbitmq-user%s" % (su, s1), "%s-rabbitmq-permission%s" % (su, s1))]
        if users != {"%s-rabbitmq-user%s" % (su, s1)}:
            fail("3e: %s after generation 0 is gone renders Users %r" % (c, sorted(users)))
        elif bad:
            fail("3e: %s after generation 0 is gone: a restarting pod would wait on %r (deleted)" % (c, bad[:3]))
        elif not waits and c == "heat":
            fail("3e: heat has no wait on its own User at all, so this check measured nothing")
        else:
            n_e += 1
    counts["3e"] = n_e
    return n


# ---------------------------------------------------------------------------------------------------------------------
# Part 4: mutants.
# ---------------------------------------------------------------------------------------------------------------------
def part4(tmp):
    n = 0
    sb = counts.get("specB") or {}
    if any(c not in sb or c not in (SPECS0 or {}) for c in ("heat", "ceilometer")):
        fail("4: no heat or ceilometer specs to mutate against")
        return 0

    def tk_sub(rel, old, new):
        def f(tk):
            p = os.path.join(tk, rel)
            s = open(p).read()
            if s.count(old) != 1:
                raise SystemExit("FAILED MEASUREMENT: mutant anchor not found in %s: %r" % (rel, old[:80]))
            open(p, "w").write(s.replace(old, new))
        return f

    toolkit_mutants = [
        ("waits ignore the suffix", "3b", "heat", tk_sub("templates/utils/_dependency_jobs.tpl",
                                                 '{{- $s := ternary $sfx "" (eq $su $own) -}}',
                                                 '{{- $s := "" -}}')),
        ("waits on another service's User", "3b", "ceilometer", tk_sub("templates/utils/_dependency_jobs.tpl",
                                                         '{{- if or (not $own) (eq $su $own) -}}',
                                                         '{{- if true -}}')),
        ("pod annotation removed", "3c", "heat", tk_sub("templates/snippets/_custom_pod_annotations.tpl",
                                                '{{- if $sfx -}}\n{{- $annotations = mergeOverwrite',
                                                '{{- if false -}}\n{{- $annotations = mergeOverwrite')),
    ]
    for i, (what, part, chart, f) in enumerate(toolkit_mutants):
        probs = check_chart(tmp, chart, SPECS0[chart][0], sb[chart][0], sb[chart][1], tag="-m%d" % i,
                            toolkit_mutate=f)
        hit = [p for k, p in probs if k == part]
        # the annotation-removed control must name that chart's workloads, not just fail somewhere
        if hit and (what != "pod annotation removed" or any(("/%s-" % chart) in p for p in hit)):
            n += 1
        else:
            fail("4: mutant '%s' passes part %s (or does not name %s's workloads): %s" % (what, part, chart, probs[:2]))
    helper_mutants = [
        ("partition rule dropped", "js2", lambda h: h.replace(
            '{{- else if and (gt $part 0) (lt (int64 ($st.updatedReplicas | default 0)) $want) -}}',
            '{{- else if false -}}')),
        ("observedGeneration ignored", "f", lambda h: h.replace(
            '{{- if lt (int64 ($ls.observedGeneration | default 0)) (int64 ($ls.generation | default 0)) -}}',
            '{{- if false -}}')),
        ("hooks-unverified ignored", "f2", lambda h: h.replace('{{- if $ls.hooksUnverified -}}', '{{- if false -}}')),
        ("switch without the User Ready", "c", lambda h: h.replace(
            '{{- if and (eq (toString $cond.type) "Ready") (eq (toString $cond.status) "True") -}}{{- $ready = true -}}{{- end -}}\n{{- end -}}',
            '{{- end -}}{{- $ready = true -}}')),
    ]
    for i, (what, case, f) in enumerate(helper_mutants):
        def mutate(h, f=f, what=what):
            h2 = f(h)
            if h2 == h:
                raise SystemExit("FAILED MEASUREMENT: mutant '%s' changed nothing" % what)
            return h2
        w = stage_umbrella(tmp, "mut%d" % i, mutate=mutate)
        _, broke = part1(tmp, w=w, only={case})
        if case in broke:
            n += 1
        else:
            fail("4: mutant '%s' passes case 1%s" % (what, case))
    return n


SPECS0 = None
FIXTURE = (None, None)
ROTATING = []   # the messaging charts the umbrella writes a user projection for: the ones rotate.<id> rotates


def main():
    global SPECS0, FIXTURE, ROTATING
    with tempfile.TemporaryDirectory() as tmp:
        SPECS0 = span_specs(tmp, {}, "none")
        ROTATING = sorted(c for c in CHARTS if (((SPECS0 or {}).get(c, ({}, []))[0].get("endpoints") or {}).get(
            "oslo_messaging") or {}).get("operator", {}).get("userSecret"))
        schema = json.load(open(os.path.join(UMBRELLA, "values.schema.json")))
        ids = sorted(k[len("oslo-messaging-"):] for k in ((schema.get("properties") or {}).get("rotate") or {}).get(
            "properties", {}))
        if ROTATING != ids:
            fail("3: the charts with an umbrella projection %r are not the ids values.schema.json lets rotate %r"
                 % (ROTATING, ids))
        if SPECS0 and "heat" in SPECS0:
            FIXTURE = heat_fixture(tmp, SPECS0["heat"][0])
        n1, broke = part1(tmp)
        for case, probs in sorted(broke.items()):
            fail("1%s: %s" % (case, "; ".join(probs)))
        n2 = part2(tmp)
        n3 = part3(tmp)
        n4 = part4(tmp) if not failures else 0
    for k, v in (("part 1", n1), ("part 2", n2), ("part 3", n3)):
        if not v:
            fail("%s checked nothing" % k)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: a RabbitMQ user rotation switches only once the new user is Ready and retires the old one only once every "
          "reader has rolled: %d step case(s) with lookups stood in (the state from what exists, no ConfigMap); %d umbrella render "
          "check(s); %d of %d rotating charts render one user per generation, wait on the active one, roll every "
          "reader and change no non-hook Job; %d of them restart onto the active user with generation 0 gone; "
          "%d mutant(s) killed" % (n1, n2, n3, len(ROTATING), counts.get("3e", 0), n4))


main()
