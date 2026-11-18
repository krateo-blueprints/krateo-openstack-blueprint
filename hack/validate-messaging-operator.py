#!/usr/bin/env python3
"""messaging.backend operator: a RabbitmqCluster, its pods in every client's host list, quorum queues for every client,
and chart mode unchanged (issue #309, phase H2b).

    hack/validate-messaging-operator.py [repo-root]

With messaging.backend operator the rabbitmq chart renders a RabbitmqCluster (krateo-blueprints/rabbitmq-operator-
blueprint) instead of its StatefulSet. The cluster-operator's RabbitMQ is 4.x (the chart pins rabbitmq:4.3.4), which
refuses the transient non-exclusive queues oslo.messaging declares unless they are quorum queues, and refuses policies
naming classic mirroring (ha-*). So operator mode is three changes that only work together: the broker, the clients'
host lists, and the clients' queue settings.

  1. THE LISTS ARE THE CHARTS'. haTopology.messagingQuorum is exactly the charts whose templates build a transport URL,
     and haTopology.messagingOperator.name is the rabbitmq chart's operator.name default.
  2. THE UMBRELLA SPECS. The lines of compositions.yaml that build a component's spec are rendered for every component:
       a. chart mode (messaging absent, or backend chart): every spec equals the spec built without this block;
       b. operator mode, with one and with three RabbitMQ replicas: rabbitmq gains operator {enabled, name}; every
          component in haTopology.messaging gains hosts.default <name>-nodes and statefulset {<name>-server, N}; every
          one in messagingByService gains the hosts; every one in messagingQuorum gains quorum_queues and
          conf.<component>.oslo_messaging_rabbit = messagingQuorumConf; nothing else moves;
       c. refused: rabbit_ha_queues true in a component's componentValues, and a statefulset override; the schema
          refuses an unknown backend.
  3. THE RABBITMQ CHART. With the operator spec it renders exactly one RabbitmqCluster (the name, a pinned image, the
     replicas, the ExternalSecret it names) and an ExternalSecret that reads the administrator's root, and none of the
     chart-mode objects (StatefulSet, Service, ConfigMaps, the cookie and admin Secrets). Against stand-in lookups on a
     live cluster: no rabbitmqclusters CRD is refused, naming the blueprint; the chart-mode StatefulSet present is
     refused as not a fresh install; the CRD present and no StatefulSet renders. At its defaults it renders no
     RabbitmqCluster.
  4. THE CLIENTS. Every chart in messagingQuorum is rendered with the operator spec:
       - every transport URL it renders lists exactly the cluster's pods (statefulset holders) or its headless Service;
       - every [oslo_messaging_rabbit] it renders has no rabbit_ha_queues true, and at least one has every
         messagingQuorumConf value;
       - a rabbit-init's RABBITMQ_AUXILIARY_CONFIGURATION, where one is still rendered, has no ha-* key and keeps
         message-ttl (in operator mode rabbit-init is replaced by topology resources, whose Policies
         hack/validate-messaging-topology.py checks); the chart-mode render still carries ha-mode, unchanged.

Fails when helm or PyYAML is missing, and when any part finds nothing to check.
"""
import base64
import configparser
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
SUFFIX = "%s.svc.cluster.local" % NS
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
failures = []
counts = {}
TRANSPORT_RX = re.compile(r"authenticated_transport_endpoint_uri_lookup|helm-toolkit\.utils\.transport_url|"
                          r"helm-toolkit\.utils\.transport_dropin")


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
        r = subprocess.run(["helm", "template", release, chart, "--namespace", NS, "--kube-version", "1.36.0",
                            "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def texts(d):
    if d.get("kind") == "Secret":
        for v in (d.get("data") or {}).values():
            try:
                yield base64.b64decode(v).decode("utf-8", "replace")
            except Exception:
                pass
        for v in (d.get("stringData") or {}).values():
            yield str(v)
        return

    def walk(n):
        if isinstance(n, dict):
            for v in n.values():
                yield from walk(v)
        elif isinstance(n, list):
            for v in n:
                yield from walk(v)
        elif isinstance(n, str):
            yield n
    yield from walk(d)


def merged(base, add):
    out = json.loads(json.dumps(base or {}))

    def rec(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                rec(a[k], v)
            else:
                a[k] = v
    rec(out, add)
    return out


# --- 1 ---
def part1(uv, tmp):
    topo = uv["haTopology"]
    builds = set()
    for c in sorted(os.listdir(BP)):
        tdir = os.path.join(BP, c, "chart", "templates")
        if not os.path.isdir(tdir):
            continue
        tpl = ""
        for dp, _, fs in os.walk(tdir):
            for f in fs:
                tpl += open(os.path.join(dp, f), errors="replace").read()
        if TRANSPORT_RX.search(tpl):
            builds.add(c)
    if not builds:
        fail("1: no chart builds a transport URL; the scan found nothing")
    have = set(topo.get("messagingQuorum") or [])
    if builds - have:
        fail("1: haTopology.messagingQuorum misses %s, whose chart builds a transport URL: with operator mode it would "
             "declare classic queues RabbitMQ 4.x refuses" % ", ".join(sorted(builds - have)))
    if have - builds:
        fail("1: haTopology.messagingQuorum names %s, whose chart builds no transport URL" % ", ".join(sorted(have - builds)))
    rv = yaml.safe_load(open(os.path.join(BP, "rabbitmq", "chart", "values.yaml")))
    if (rv.get("operator") or {}).get("name") != topo["messagingOperator"]["name"]:
        fail("1: the rabbitmq chart's operator.name is %r, the umbrella's haTopology.messagingOperator.name %r"
             % ((rv.get("operator") or {}).get("name"), topo["messagingOperator"]["name"]))
    counts["1"] = len(builds)
    return builds


# --- 2 ---
def spans():
    comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
    start = comp.find("{{- $ov := ")
    ha = comp.find('include "osh.haValues"', start)
    ha_end = comp.find("{{- end }}", ha) if ha >= 0 else -1
    op = comp.find('include "osh.messagingOperatorValues"', start)
    op_end = comp.find("{{- end }}", op) if op >= 0 else -1
    if min(start, ha, ha_end, op, op_end) < 0 or op < ha_end:
        return None, None
    return comp[start:ha_end + len("{{- end }}")], comp[start:op_end + len("{{- end }}")]


def probe(u, vals):
    rc, out, err = helm_template(u, "openstack-gate", vals)
    if rc != 0:
        return None, err
    s = {"OP": {}, "BASE": {}}
    for line in out.splitlines():
        m = re.match(r"^# (OP|BASE)SPEC (\S+) (.*)$", line)
        if m:
            s[m.group(1)][m.group(2)] = json.loads(m.group(3))
    return s, ""


def part2(uv, tmp):
    topo = uv["haTopology"]
    name = topo["messagingOperator"]["name"]
    names = [c["name"] for c in uv["components"]]
    base_span, op_span = spans()
    if base_span is None:
        fail("2: compositions.yaml builds no spec through osh.messagingOperatorValues after the ha merge")
        return None
    u = stage(UMBRELLA, tmp, "umbrella")
    open(os.path.join(u, "templates", "zz-gate-mq-probe.yaml"), "w").write(
        '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
        '{{- range $c := .Values.components }}\n' + base_span + '\n# BASESPEC {{ $c.name }} {{ $ov | toJson }}\n'
        '{{- end }}\n{{- range $c := .Values.components }}\n' + op_span +
        '\n# OPSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n')
    mq3 = {"rabbitmq": {"pod": {"replicas": {"server": 3}}}}
    cases = {
        "absent": {"profile": "full", "messaging": None},
        "chart": {"profile": "full", "messaging": {"backend": "chart"}},
        "op1": {"profile": "full", "messaging": {"backend": "operator"}},
        "op3": {"profile": "full", "messaging": {"backend": "operator"}, "componentValues": mq3},
    }
    specs = {}
    for k, v in cases.items():
        s, err = probe(u, v)
        if s is None:
            fail("2: the umbrella probe does not render with %s: %s" % (k, err.strip()[:300]))
            return None
        specs[k] = s
    for n in names:
        for k in ("absent", "chart"):
            if specs[k]["OP"].get(n) != specs[k]["BASE"].get(n):
                fail("2a: in chart mode (%s) %s's spec changes: %s" % (k, n, json.dumps(specs[k]["OP"].get(n))[:200]))
        for k, reps in (("op1", 1), ("op3", 3)):
            want = specs[k]["BASE"].get(n) or {}
            add = {}
            om = {}
            if n == "rabbitmq":
                add["operator"] = {"enabled": True, "name": name}
            if n in topo["messaging"]:
                om["hosts"] = {"default": "%s-nodes" % name}
                om["statefulset"] = {"name": "%s-server" % name, "replicas": reps}
            if n in (topo.get("messagingByService") or []):
                om["hosts"] = {"default": "%s-nodes" % name}
            if n in topo["messagingQuorum"]:
                om["quorum_queues"] = True
                om["operator"] = {"cluster": name}
                add["conf"] = {n: {"oslo_messaging_rabbit": topo["messagingQuorumConf"]}}
            if om:
                add["endpoints"] = {"oslo_messaging": om}
            want = merged(want, add)
            if (specs[k]["OP"].get(n) or {}) != want:
                fail("2b: with operator mode (%s) %s's spec is %s, want %s" % (
                    k, n, json.dumps(specs[k]["OP"].get(n))[:300], json.dumps(want)[:300]))
    counts["2"] = len(names)
    for label, vals, needle in (
            ("rabbit_ha_queues true in nova's componentValues",
             {"messaging": {"backend": "operator"},
              "componentValues": {"nova": {"conf": {"nova": {"oslo_messaging_rabbit": {"rabbit_ha_queues": True}}}}}},
             "rabbit_ha_queues is true"),
            ("a statefulset override in keystone's componentValues",
             {"messaging": {"backend": "operator"},
              "componentValues": {"keystone": {"endpoints": {"oslo_messaging": {"statefulset": {
                  "name": "%s-server" % name, "replicas": 1}}}}}},
             "the RabbitMQ host list is the umbrella's")):
        rc, _, err = helm_template(u, "openstack-gate", vals)
        if rc == 0:
            fail("2c: %s renders; it must be refused" % label)
        elif needle not in err:
            fail("2c: %s is refused, but not by this check: %s" % (label, err.strip()[:200]))
    rc, _, _ = helm_template(u, "openstack-gate", {"messaging": {"backend": "kafka"}})
    if rc == 0:
        fail("2c: messaging.backend kafka renders; the schema must allow only chart and operator")
    return specs["op3"]["OP"]


# --- 3 ---
def part3(uv, spec, tmp):
    name = uv["haTopology"]["messagingOperator"]["name"]
    r = stage(os.path.join(BP, "rabbitmq", "chart"), tmp, "rabbitmq")
    rc, out, err = helm_template(r, "rabbitmq", spec)
    if rc != 0:
        fail("3: rabbitmq does not render with the operator spec: %s" % err.strip()[:300])
        return
    docs = [d for d in yaml.safe_load_all(out) if d]
    clusters = [d for d in docs if d.get("kind") == "RabbitmqCluster"]
    if len(clusters) != 1 or clusters[0]["metadata"]["name"] != name:
        fail("3: want exactly one RabbitmqCluster %s, got %s" % (name, [d["metadata"]["name"] for d in clusters]))
    else:
        s = clusters[0]["spec"]
        img = str(s.get("image") or "")
        if not re.match(r"^[^:]+:\d+\.\d+\.\d+", img):
            fail("3: the RabbitmqCluster's image %r is not pinned to a version: the clients' queue settings follow it" % img)
        if s.get("replicas") != 3:
            fail("3: the RabbitmqCluster runs %s replica(s), want the one source's 3" % s.get("replicas"))
        es_name = ((s.get("secretBackend") or {}).get("externalSecret") or {}).get("name")
        es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == es_name]
        root = (((spec.get("endpoints") or {}).get("oslo_messaging") or {}).get("auth") or {}).get("user", {}).get(
            "passwordFrom", {})
        if not es:
            fail("3: the RabbitmqCluster names externalSecret %r, which the chart does not render" % es_name)
        else:
            refs = [(x["remoteRef"]["key"], x["remoteRef"]["property"]) for x in es[0]["spec"].get("data") or []]
            if refs != [(root.get("secret"), root.get("key"))]:
                fail("3: the default-user ExternalSecret reads %s, not the administrator's root %s/%s"
                     % (refs, root.get("secret"), root.get("key")))
            if "default_user.conf" not in es[0]["spec"]["target"]["template"]["data"]:
                fail("3: the default-user Secret has no default_user.conf, which the cluster-operator reads")
        if "requiredDuringSchedulingIgnoredDuringExecution" not in ((s.get("affinity") or {}).get("podAntiAffinity") or {}):
            fail("3: three quorum members without required hostname anti-affinity: one node's loss can take a majority")
    leftovers = sorted("%s/%s" % (d["kind"], d["metadata"]["name"]) for d in docs
                       if d.get("kind") in ("StatefulSet", "Service", "ConfigMap")
                       or (d.get("kind") in ("Secret", "ExternalSecret") and ("cookie" in d["metadata"]["name"]
                                                                              or "admin-user" in d["metadata"]["name"])))
    if leftovers:
        fail("3: in operator mode the chart still renders chart-mode objects: %s" % leftovers)
    rc, out, _ = helm_template(r, "rabbitmq", {})
    if rc != 0 or any(d and d.get("kind") == "RabbitmqCluster" for d in yaml.safe_load_all(out)):
        fail("3: at its defaults the rabbitmq chart renders a RabbitmqCluster, or does not render")
    # The live-cluster refusals, with the lookups replaced by stand-ins.
    tpl = os.path.join(r, "templates", "_operator.tpl")
    orig = open(tpl).read()
    sts = json.dumps({"metadata": {"name": "rabbitmq-rabbitmq"}})
    crd = json.dumps({"metadata": {"name": "rabbitmqclusters.rabbitmq.com"}})

    def with_lookups(crd_json, sts_json):
        """_operator.tpl with each lookup replaced by a raw-string constant: the live-cluster witness true, and the
        CRD and the StatefulSet as the given JSON ({} for absent)."""
        s = orig
        pats = (
            (r'lookup "apiextensions\.k8s\.io/v1" "CustomResourceDefinition" "" "compositiondefinitions\.core\.krateo\.io"',
             '`true`'),
            (r'lookup "apiextensions\.k8s\.io/v1" "CustomResourceDefinition" "" "rabbitmqclusters\.rabbitmq\.com" \| toJson',
             '`%s`' % crd_json),
            (r'lookup "apps/v1" "StatefulSet" \.Release\.Namespace \(printf "%s-rabbitmq" \.deployment_name\) \| toJson',
             '`%s`' % sts_json),
        )
        for pat, rep in pats:
            s, n = re.subn(pat, lambda _m: rep, s)
            if n != 1:
                return False
        if re.search(r"\blookup \"", s):
            return False
        open(tpl, "w").write(s)
        return True

    for label, crd_json, sts_json, needle in (
            ("no rabbitmqclusters CRD", "{}", "{}", "rabbitmq-operator-blueprint"),
            ("the chart-mode StatefulSet present", crd, sts, "fresh installs"),
            ("the CRD present and no StatefulSet", crd, "{}", None)):
        if not with_lookups(crd_json, sts_json):
            fail("3: the stand-in lookups could not replace every lookup in _operator.tpl; the refusals were not checked")
            break
        rc, _, err = helm_template(r, "rabbitmq", spec)
        if needle is None and rc != 0:
            fail("3: %s is refused: %s" % (label, err.strip()[:200]))
        elif needle is not None and (rc == 0 or needle not in err):
            fail("3: %s must be refused naming %r; rc %d: %s" % (label, needle, rc, err.strip()[:200]))
    open(tpl, "w").write(orig)
    counts["3"] = True


# --- 4 ---
def part4(uv, specs_op, builds, tmp):
    topo = uv["haTopology"]
    name = topo["messagingOperator"]["name"]
    want_pods = sorted("%s-server-%d.%s-nodes.%s:5672" % (name, i, name, SUFFIX) for i in range(3))
    want_svc = ["%s-nodes.%s:5672" % (name, SUFFIX)]
    qconf = {k: str(v).lower() for k, v in topo["messagingQuorumConf"].items()}
    seen = {}
    for chart in sorted(topo["messagingQuorum"]):
        w = stage(os.path.join(BP, chart, "chart"), tmp, "c-" + chart)
        rc, out, err = helm_template(w, chart + "-gate1234", specs_op.get(chart) or {})
        if rc != 0:
            fail("4: %s does not render with the operator spec: %s" % (chart, err.strip()[:200]))
            continue
        docs = [d for d in yaml.safe_load_all(out) if d]
        want = want_svc if chart in (topo.get("messagingByService") or []) else want_pods
        nurl = nsec = 0
        complete = False
        for d in docs:
            for text in texts(d):
                for line in text.splitlines():
                    if "rabbit://" in line and re.search(r"@[A-Za-z0-9.-]+:5672\b", line):
                        nurl += 1
                        hosts = sorted(re.findall(r"@([A-Za-z0-9.-]+:\d+)", line))
                        if hosts != want:
                            fail("4: %s renders a transport URL listing %s, want %s" % (chart, hosts, want))
                if "[oslo_messaging_rabbit]" in text:
                    cp = configparser.RawConfigParser(strict=False, interpolation=None)
                    try:
                        cp.read_string(text)
                    except configparser.Error:
                        continue
                    if not cp.has_section("oslo_messaging_rabbit"):
                        continue
                    nsec += 1
                    sec = {k: v.strip().lower() for k, v in cp.items("oslo_messaging_rabbit")}
                    if sec.get("rabbit_ha_queues") == "true":
                        fail("4: %s renders [oslo_messaging_rabbit] rabbit_ha_queues = true: oslo.messaging raises "
                             "RuntimeError with quorum queues" % chart)
                    if all(sec.get(k) == v for k, v in qconf.items()):
                        complete = True
            for c in (d.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []) \
                    if d.get("kind") == "Job" else []:
                for e in c.get("env") or []:
                    if e.get("name") == "RABBITMQ_AUXILIARY_CONFIGURATION":
                        aux = json.loads(e["value"])
                        for p in aux.get("policies") or []:
                            bad = [k for k in (p.get("definition") or {}) if k.startswith("ha-")]
                            if bad:
                                fail("4: %s's rabbit-init imports policy %s with %s, which RabbitMQ 4.x refuses"
                                     % (chart, p.get("name"), bad))
                            elif "message-ttl" not in (p.get("definition") or {}):
                                fail("4: %s's rabbit-init policy %s lost message-ttl" % (chart, p.get("name")))
        if chart in builds and not nurl:
            fail("4: %s builds a transport URL but renders none with the operator spec" % chart)
        if not complete:
            fail("4: %s renders no [oslo_messaging_rabbit] with every quorum setting (%d section(s) seen)" % (chart, nsec))
        seen[chart] = (nurl, nsec)
    # Chart mode keeps the policies as they were.
    w = os.path.join(tmp, "c-nova")
    rc, out, _ = helm_template(w, "nova-gate1234", {})
    if rc == 0 and '\\"ha-mode\\"' not in out and '"ha-mode"' not in out:
        fail("4: nova at its defaults no longer imports its ha-mode policy: chart mode must stay unchanged")
    counts["4"] = seen


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    if not uv.get("components") or not (uv.get("haTopology") or {}).get("messagingQuorum"):
        print("FAILED MEASUREMENT: the umbrella lists no component or no haTopology.messagingQuorum")
        sys.exit(2)
    with tempfile.TemporaryDirectory() as tmp:
        builds = part1(uv, tmp)
        specs = part2(uv, tmp)
        if specs is not None:
            part3(uv, specs.get("rabbitmq") or {}, tmp)
            part4(uv, specs, builds, tmp)
    for k in ("1", "2", "3", "4"):
        if not counts.get(k):
            fail("part %s checked nothing" % k)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: messaging.backend operator: %d transport-URL charts listed; %d component specs (chart mode passes nothing, "
          "operator mode moves only the broker and its clients); the RabbitmqCluster and its refusals; %d client chart(s) "
          "list the cluster's pods and run quorum queues, their policies without ha-*" % (counts["1"], counts["2"],
                                                                                         len(counts["4"])))


main()
