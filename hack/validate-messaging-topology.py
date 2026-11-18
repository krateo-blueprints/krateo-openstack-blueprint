#!/usr/bin/env python3
"""With the RabbitMQ cluster-operator, each component's rabbit-init becomes topology-operator resources, from the same
root, and every pod that waited on the Job waits on them (issue #309, phase H2b).

    hack/validate-messaging-topology.py [repo-root]

With messaging.backend operator the umbrella passes endpoints.oslo_messaging.operator.cluster to every component that
builds a transport URL. helm-toolkit.manifests.rabbitmq_topology then renders, in place of the rabbit-init Job, a User
(importing <su>-rabbitmq-topology-user, an ExternalSecret projecting the user's generated root), a Vhost (quorum by
default, retained on deletion), the user's Permission, the administrator's Permission (ceilometer reads other vhosts as the
administrator, #281) and a Policy per conf.rabbitmq.policies entry without ha-* keys. helm-toolkit.utils.
dependency_jobs_filter turns every wait on <su>-rabbit-init into waits on those resources being Ready, which the pod's
ServiceAccount may read (#370).

  1. THE UMBRELLA SPEC. The component specs are rendered from compositions.yaml in operator mode (profile full, three
     RabbitMQ replicas), as hack/validate-messaging-operator.py does.
  2. EACH COMPONENT. Every chart in haTopology.messagingQuorum is rendered with its spec. Where it runs rabbit-init:
       - no rabbit-init Job, and exactly the five resources by name, each referencing the cluster;
       - the User imports the ExternalSecret <su>-rabbitmq-topology-user, which reads exactly the root the spec passes
         at endpoints.oslo_messaging.auth.<component>.passwordFrom, with the chart's username; it is the one
         ExternalSecret of that name and the only writer of its Secret, which it labels rabbitmq.com/topology-operator=true
         (the operator reads no other Secret, and its User webhook refuses one without it);
       - the Vhost is the chart's path, quorum by default, deletionPolicy retain; both Permissions name it, one with the
         User, one with the administrator's username;
       - one Policy per chart policy, none with an ha-* key, message-ttl kept, names DNS-1123.
     Where it does not (keystone and glance ship rabbit-init off): no topology resource, and no pod waits on one.
     In every chart: no two rendered resources share an identity (group, kind, namespace, name). helm install refuses
     such a release and helm template does not, so before H2 run 2 the topology projection and the chart's own
     connection Secret, both ExternalSecret <svc>-rabbitmq-user, passed here and failed on GKE.
     2b: rabbit-init switched off renders and waits on nothing. 2c: a chart whose secrets.oslo_messaging.<svc> is named
     like the projection must refuse to render.
  3. THE WAITS. In every rendered pod: no Job wait named *-rabbit-init; every custom-resource wait is on a Ready
     condition and granted to the pod's ServiceAccount; every rabbit-init the chart-mode render waited on is replaced
     by its four waits; and every resource any pod waits on is rendered by some component.
  4. CHART MODE. nova at its defaults still renders its rabbit-init Job and no rabbitmq.com resource.

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
NS = "openstack"
DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
KINDS = ("User", "Vhost", "Permission", "Policy")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
failures = []


# OPERATOR_OBJECTS_DIR: hack/validate-operator-admission.py sets it to collect every rabbitmq.com object this gate
# renders from the umbrella's operator spec (never from a mutation), for a real API server to admit. Unset, nothing is
# written and the gate is unchanged.
EMIT = os.environ.get("OPERATOR_OBJECTS_DIR")


def _load(name):
    import importlib.util as _u
    _s = _u.spec_from_file_location(name, os.path.join(ROOT, "hack", name + ".py"))
    _m = _u.module_from_spec(_s)
    _s.loader.exec_module(_m)
    return _m


# render_identity (H2 run 2): every render this gate makes from the umbrella's operator spec must hold no two resources
# of one identity, which helm template accepts and helm install refuses.
LEDGER = _load("render_identity").Ledger(os.path.basename(__file__))


def emit(tag, docs):
    if not EMIT:
        return
    # rabbitmq.com objects, and the ExternalSecrets whose Secrets the operators' webhooks read (eso_model, #405).
    objs = [d for d in docs if d and (str(d.get("apiVersion", "")).startswith("rabbitmq.com/")
                                      or d.get("kind") == "ExternalSecret")]
    if objs:
        with open(os.path.join(EMIT, "%s.yaml" % tag), "w") as f:
            yaml.safe_dump_all(objs, f, default_flow_style=False, sort_keys=False)


counts = {}
UMBRELLA_PROJ = {}   # name -> the umbrella-rendered projection ExternalSecret (op_specs)

# What the umbrella delivers for a chart's storage (#308): cinder's default refuses, so a chart render carries
# hack/gate_values.py's arguments (nothing for every other chart).
import importlib.util as _gv_util
import os as _gv_os
_gv_spec = _gv_util.spec_from_file_location(
    "gate_values", _gv_os.path.join(_gv_os.path.dirname(_gv_os.path.abspath(__file__)), "gate_values.py"))
gate_values = _gv_util.module_from_spec(_gv_spec)
_gv_spec.loader.exec_module(gate_values)


def fail(m):
    failures.append(m)


def stage(chart_dir, tmp, name):
    w = os.path.join(tmp, name)
    if os.path.isdir(w):
        return w
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
        r = subprocess.run(["helm", "template", release, chart, "--namespace", NS, "--kube-version", "1.36.0",
                            *gate_values.helm_args(chart), "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def op_specs(tmp):
    comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
    start = comp.find("{{- $ov := ")
    op = comp.find('include "osh.messagingOperatorValues"', start)
    end = comp.find("{{- end }}", op) if op >= 0 else -1
    if start < 0 or end < 0:
        fail("1: compositions.yaml builds no spec through osh.messagingOperatorValues")
        return None
    u = stage(UMBRELLA, tmp, "umbrella")
    open(os.path.join(u, "templates", "zz-gate-topology-probe.yaml"), "w").write(
        '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n{{- range $c := .Values.components }}\n'
        + comp[start:end + len("{{- end }}")] + '\n# TOPOSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n')
    rc, out, err = helm_template(u, "openstack-gate", {"profile": "full", "messaging": {"backend": "operator"},
                                                       "componentValues": {"rabbitmq": {"pod": {"replicas": {
                                                           "server": 3}}}}})
    if rc != 0:
        fail("1: the umbrella probe does not render in operator mode: %s" % err.strip()[:300])
        return None
    specs = {}
    for line in out.splitlines():
        m = re.match(r"^# TOPOSPEC (\S+) (.*)$", line)
        if m:
            specs[m.group(1)] = json.loads(m.group(2))
    counts["1"] = len(specs)
    # The user projections the UMBRELLA writes (templates/messaging-projections.yaml, #309 H2 run 4 option 4). The same
    # offline render carries them: they are not gated on a lookup (the component Compositions are, and an offline
    # render emits none of those; see part 5 of validate-messaging-operator.py for that gate, with a stand-in).
    for d in yaml.safe_load_all(out):
        if d and d.get("kind") == "ExternalSecret" and ((d.get("metadata") or {}).get("labels") or {}).get(
                "krateo.io/messaging-projection"):
            name = d["metadata"]["name"]
            if name in UMBRELLA_PROJ:
                fail("1: the umbrella renders the projection %s twice" % name)
            UMBRELLA_PROJ[name] = d
    emit("umbrella-projections", list(UMBRELLA_PROJ.values()))
    if not UMBRELLA_PROJ:
        fail("1: the umbrella renders no user projection in operator mode (templates/messaging-projections.yaml)")
    return specs


def pods(docs):
    for d in docs:
        spec = d.get("spec") or {}
        if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
            yield d, spec.get("template", {}).get("spec", {})
        elif d.get("kind") == "CronJob":
            yield d, spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})


def env_of(pod, name):
    for c in pod.get("initContainers") or []:
        for e in c.get("env") or []:
            if e.get("name") == name:
                return e.get("value") or ""
    return None


def grants(docs):
    roles = {(d["metadata"].get("namespace"), d["metadata"]["name"]): d for d in docs if d.get("kind") == "Role"}
    out = {}
    for b in (d for d in docs if d.get("kind") == "RoleBinding"):
        role = roles.get((b["metadata"].get("namespace"), b["roleRef"]["name"]))
        for s in (b.get("subjects") or []) if role else []:
            for r in role.get("rules") or []:
                if "get" in (r.get("verbs") or []):
                    for g in r.get("apiGroups") or []:
                        for res in r.get("resources") or []:
                            for n in r.get("resourceNames") or ["*"]:
                                out.setdefault(s["name"], set()).add((g, res, n))
    return out


def waits(docs):
    """[(workload, sa, jobs[], custom_resources[])] for every pod with a kubernetes-entrypoint init container."""
    out = []
    for d, pod in pods(docs):
        jobs = env_of(pod, "DEPENDENCY_JOBS")
        if jobs is None:
            continue
        crs = env_of(pod, "DEPENDENCY_CUSTOM_RESOURCE") or ""
        out.append(("%s/%s" % (d["kind"], d["metadata"]["name"]), pod.get("serviceAccountName"),
                    [j for j in jobs.split(",") if j], json.loads(crs) if crs else []))
    return out


def chart_values(chart):
    return yaml.safe_load(open(os.path.join(BP, chart, "chart", "values.yaml")))


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    topo = uv.get("haTopology") or {}
    charts = topo.get("messagingQuorum") or []
    if not charts:
        print("FAILED MEASUREMENT: haTopology.messagingQuorum is empty")
        sys.exit(2)
    cluster = topo["messagingOperator"]["name"]
    rendered_crs = set()
    waited = []
    with tempfile.TemporaryDirectory() as tmp:
        specs = op_specs(tmp)
        if specs is None:
            report()
        n_with = 0
        for chart in sorted(charts):
            spec = specs.get(chart) or {}
            cv = chart_values(chart)
            w = stage(os.path.join(BP, chart, "chart"), tmp, chart)
            rc, out, err = helm_template(w, chart, spec)
            if rc != 0:
                fail("2: %s does not render with the operator spec: %s" % (chart, err.strip()[:300]))
                continue
            docs = [d for d in yaml.safe_load_all(out) if d]
            rc, out0, _ = helm_template(w, chart, {})
            docs0 = [d for d in yaml.safe_load_all(out0) if d] if rc == 0 else []
            manifests = dict(cv.get("manifests") or {})
            manifests.update((spec.get("manifests") or {}))
            runs = manifests.get("job_rabbit_init", True) and manifests.get("secret_rabbitmq", True)
            topo_docs = [d for d in docs if d.get("apiVersion", "").startswith("rabbitmq.com/") and d.get("kind") in KINDS]
            for d in topo_docs:
                rendered_crs.add((d["kind"], d["metadata"]["name"]))
            emit(chart, docs)   # emit() keeps the rabbitmq.com objects and the ExternalSecrets (the projections)
            # helm install refuses a release holding two resources of one identity ("rendered manifest contains
            # duplicate resource"); helm template does not, so this gate must (H2 run 2: the topology projection and
            # the chart's own connection Secret were both ExternalSecret <svc>-rabbitmq-user).
            LEDGER.control(docs)
            LEDGER.check("%s (operator mode)" % chart, docs)
            om = cv["endpoints"]["oslo_messaging"]
            su = chart.replace("_", "-")
            if not runs:
                if topo_docs:
                    fail("2: %s ships rabbit-init off but renders topology resources %s"
                         % (chart, [d["metadata"]["name"] for d in topo_docs]))
            else:
                n_with += 1
                if any(d.get("kind") == "Job" and d["metadata"]["name"].endswith("-rabbit-init") for d in docs):
                    fail("2: %s still renders its rabbit-init Job in operator mode" % chart)
                by = {(d["kind"], d["metadata"]["name"]): d for d in topo_docs}
                want = [("User", su + "-rabbitmq-user"), ("Vhost", su + "-rabbitmq-vhost"),
                        ("Permission", su + "-rabbitmq-permission"), ("Permission", su + "-rabbitmq-admin-permission")]
                for k in want:
                    if k not in by:
                        fail("2: %s renders no %s %s" % (chart, k[0], k[1]))
                for d in topo_docs:
                    if (d["spec"].get("rabbitmqClusterReference") or {}).get("name") != cluster:
                        fail("2: %s's %s %s references cluster %r, not %r" % (
                            chart, d["kind"], d["metadata"]["name"], d["spec"].get("rabbitmqClusterReference"), cluster))
                    if not DNS1123.match(d["metadata"]["name"]):
                        fail("2: %s's %s name %r is not DNS-1123" % (chart, d["kind"], d["metadata"]["name"]))
                vhost = (om.get("path") or "").lstrip("/")
                u = by.get(want[0])
                root = ((((spec.get("endpoints") or {}).get("oslo_messaging") or {}).get("auth") or {}).get(chart) or {}
                        ).get("passwordFrom") or {}
                if u:
                    es_name = (u["spec"].get("importCredentialsSecret") or {}).get("name")
                    passed = ((((spec.get("endpoints") or {}).get("oslo_messaging") or {}).get("operator") or {})
                              .get("userSecret"))
                    in_chart = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == es_name]
                    owners = [d["metadata"]["name"] for d in docs if d.get("kind") == "ExternalSecret"
                              and ((d.get("spec") or {}).get("target") or {}).get("name") == es_name] + \
                             [d["metadata"]["name"] for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"] == es_name]
                    # One writer: the umbrella when it passes the name (option 4), else the chart (standalone).
                    if passed:
                        es = [UMBRELLA_PROJ[es_name]] if es_name in UMBRELLA_PROJ else []
                        if es_name != passed:
                            fail("2: %s's User imports %r, not the name the umbrella passes (%r)" % (chart, es_name, passed))
                        if in_chart or owners:
                            fail("2: %s renders %s itself although the umbrella passes and writes it: two writers"
                                 % (chart, in_chart and es_name or owners))
                        counts["umbrella-owned"] = counts.get("umbrella-owned", 0) + 1
                    else:
                        es = in_chart
                    if not es:
                        fail("2: %s's User imports %r, which %s does not render" % (chart, es_name,
                                                                                  "the umbrella" if passed else "the chart"))
                    elif len(es) != 1 or (not passed and owners != [es_name]):
                        fail("2: %s's User imports %r, which %d ExternalSecret(s) render and %s write: one projection, "
                             "one owner of its Secret" % (chart, es_name, len(es), owners))
                    else:
                        refs = [(x["remoteRef"]["key"], x["remoteRef"]["property"]) for x in es[0]["spec"]["data"]]
                        data = es[0]["spec"]["target"]["template"]["data"]
                        if refs != [(root.get("secret"), root.get("key"))] or not root:
                            fail("2: %s's user projection reads %s, not the root the umbrella passes (%s/%s)"
                                 % (chart, refs, root.get("secret"), root.get("key")))
                        lbl = ((es[0]["spec"]["target"]["template"].get("metadata") or {}).get("labels") or {})
                        if lbl.get("rabbitmq.com/topology-operator") != "true":
                            fail("2: %s's user projection does not label its Secret rabbitmq.com/topology-operator=true "
                                 "(target.template.metadata.labels %s): the topology operator caches only labelled "
                                 "Secrets and its User webhook refuses the import otherwise (v1.20.5 cmd/main.go:281, "
                                 "internal/webhook/v1beta1/label_validation.go; H2 run 3)" % (chart, lbl))
                        if data.get("username") != om["auth"][chart]["username"] or "password" not in data:
                            fail("2: %s's user projection holds username %r and keys %s; want username %r and a "
                                 "password" % (chart, data.get("username"), sorted(data), om["auth"][chart]["username"]))
                v = by.get(want[1])
                if v and (v["spec"].get("name") != vhost or v["spec"].get("defaultQueueType") != "quorum"
                          or v["spec"].get("deletionPolicy") != "retain"):
                    fail("2: %s's Vhost is %s; want name %r, defaultQueueType quorum, deletionPolicy retain"
                         % (chart, json.dumps(v["spec"])[:200], vhost))
                p = by.get(want[2])
                if p and (p["spec"].get("vhost") != vhost or (p["spec"].get("userReference") or {}).get("name") != want[0][1]):
                    fail("2: %s's Permission does not grant the User %s on vhost %s" % (chart, want[0][1], vhost))
                a = by.get(want[3])
                if a and (a["spec"].get("vhost") != vhost or a["spec"].get("user") != om["auth"]["admin"]["username"]):
                    fail("2: %s's administrator Permission does not name %r on vhost %s"
                         % (chart, om["auth"]["admin"]["username"], vhost))
                pols = [d for d in topo_docs if d["kind"] == "Policy"]
                want_pols = ((cv.get("conf") or {}).get("rabbitmq") or {}).get("policies") or []
                if len(pols) != len(want_pols):
                    fail("2: %s renders %d Policy resource(s) for %d chart policies" % (chart, len(pols), len(want_pols)))
                for d in pols:
                    defn = d["spec"].get("definition") or {}
                    if any(k.startswith("ha-") for k in defn):
                        fail("2: %s's Policy %s keeps %s, which RabbitMQ 4.x refuses" % (
                            chart, d["metadata"]["name"], [k for k in defn if k.startswith("ha-")]))
                    if "message-ttl" not in defn:
                        fail("2: %s's Policy %s lost message-ttl" % (chart, d["metadata"]["name"]))
            # 3. the waits
            g = grants(docs)
            chart_mode_rabbit = set()
            for _, _, jobs, _ in waits(docs0):
                chart_mode_rabbit |= {j for j in jobs if j.endswith("-rabbit-init")}
            for wl, sa, jobs, crs in waits(docs):
                for j in jobs:
                    if j.endswith("-rabbit-init"):
                        fail("3: %s %s waits on Job %s, which operator mode does not render" % (chart, wl, j))
                names = {(c.get("kind"), c.get("name")) for c in crs}
                for c in crs:
                    if not c.get("conditions"):
                        fail("3: %s %s waits on %s %s without a condition" % (chart, wl, c.get("kind"), c.get("name")))
                    grp = (c.get("apiVersion") or "").split("/")[0]
                    have = g.get(sa, set())
                    if (grp, c.get("resource"), c.get("name")) not in have:
                        fail("3: %s %s waits on %s %s, but ServiceAccount %s may not get it" % (
                            chart, wl, c.get("kind"), c.get("name"), sa))
                    if grp == "rabbitmq.com":
                        waited.append((chart, wl, c.get("kind"), c.get("name")))
            for wl0, _, jobs0, _ in waits(docs0):
                for j in jobs0:
                    if not j.endswith("-rabbit-init") or not runs:
                        continue
                    jsu = j[:-len("-rabbit-init")]
                    match = [x for x in waits(docs) if x[0] == wl0]
                    if not match:
                        continue
                    have = {(c.get("kind"), c.get("name")) for c in match[0][3]}
                    for k in (("User", jsu + "-rabbitmq-user"), ("Vhost", jsu + "-rabbitmq-vhost"),
                              ("Permission", jsu + "-rabbitmq-permission"),
                              ("Permission", jsu + "-rabbitmq-admin-permission")):
                        if k not in have:
                            fail("3: %s %s waited on %s in chart mode but does not wait on %s %s in operator mode"
                                 % (chart, wl0, j, k[0], k[1]))
        counts["2"] = n_with
        # 2b. rabbit-init switched off in a chart that has it: nothing rendered, nothing waited on. Without the guard
        # the waits would point at resources no template renders, and the pods would stay in Init forever.
        nova_spec = json.loads(json.dumps(specs.get("nova") or {}))
        nova_spec.setdefault("manifests", {})["job_rabbit_init"] = False
        rc, out, err = helm_template(stage(os.path.join(BP, "nova", "chart"), tmp, "nova"), "nova", nova_spec)
        if rc != 0:
            fail("2b: nova with job_rabbit_init off does not render in operator mode: %s" % err.strip()[:200])
        else:
            ndocs = [d for d in yaml.safe_load_all(out) if d]
            stray = [d["metadata"]["name"] for d in ndocs if d.get("apiVersion", "").startswith("rabbitmq.com/")]
            bad = [(wl, c.get("name")) for wl, _, _, crs in waits(ndocs) for c in crs
                   if (c.get("apiVersion") or "").startswith("rabbitmq.com/")]
            if stray or bad:
                fail("2b: nova with job_rabbit_init off renders %s and waits on %s" % (stray, bad[:3]))
            if not waits(ndocs):
                fail("2b: nova with job_rabbit_init off renders no pod with dependencies; nothing was checked")
        # 2c. A chart whose own connection Secret is named like the topology projection must refuse to render: one
        # release cannot hold both, and helm would refuse it only at install, past every gate.
        heat_spec = json.loads(json.dumps(specs.get("heat") or {}))
        heat_spec.setdefault("secrets", {}).setdefault("oslo_messaging", {})["heat"] = "heat-rabbitmq-topology-user"
        rc, out, err = helm_template(stage(os.path.join(BP, "heat", "chart"), tmp, "heat-2c"), "heat", heat_spec)
        if rc == 0:
            fail("2c: heat renders with secrets.oslo_messaging.heat named like its topology projection; it must refuse")
        elif "the name of the user projection" not in err:
            fail("2c: heat with a colliding secrets.oslo_messaging.heat fails for another reason: %s" % err.strip()[:200])
        else:
            counts["2c"] = True
        for chart, wl, kind, name in waited:
            if (kind, name) not in rendered_crs:
                fail("3: %s %s waits on %s %s, which no component renders" % (chart, wl, kind, name))
        counts["3"] = len(waited)
        # 4. chart mode
        w = stage(os.path.join(BP, "nova", "chart"), tmp, "nova")
        rc, out, err = helm_template(w, "nova", {})
        docs = [d for d in yaml.safe_load_all(out) if d] if rc == 0 else []
        if not any(d.get("kind") == "Job" and d["metadata"]["name"] == "nova-rabbit-init" for d in docs) or \
                any(d.get("apiVersion", "").startswith("rabbitmq.com/") for d in docs):
            fail("4: nova at its defaults must render its rabbit-init Job and no rabbitmq.com resource")
        counts["4"] = True
    report()


def report():
    for k in ("1", "2", "2c", "3", "4", "umbrella-owned"):
        if not counts.get(k):
            fail("part %s checked nothing" % k)
    for f in LEDGER.report():
        fail("identity: " + f)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: operator mode: %d component(s) render their user, vhost, permissions and policies as topology resources "
          "from their roots; %d custom-resource wait(s) replace rabbit-init, each Ready-conditioned, granted and "
          "rendered; chart mode unchanged; identity: %s" % (counts["2"], counts["3"], LEDGER.summary()))
    sys.exit(0)


main()
