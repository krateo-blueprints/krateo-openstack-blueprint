#!/usr/bin/env python3
"""One source for each stateful host list, reaching every reader; ha off changes nothing (issue #309).

    hack/validate-ha-propagation.py [repo-root]

The umbrella's ha switch and haTopology (blueprints/openstack/chart/values.yaml, osh.haValues) carry two host lists:

  - RabbitMQ: every component that builds an oslo.messaging transport URL lists one host per server replica
    (helm-toolkit authenticated_transport_endpoint_uri_lookup over endpoints.oslo_messaging.statefulset). 22 charts held
    `replicas: 1` each, and nothing propagated the server's count, so a 3-node RabbitMQ would still be reached through
    pod 0 alone. The count's one source is componentValues.rabbitmq.pod.replicas.server.
  - memcached: with ha.enabled, 3 replicas behind a headless Service, and every component that builds a memcache server
    list names each replica, so every client hashes a key to the same one. Before, one ClusterIP with ClientIP affinity
    pinned each client to one replica, so the replicas would have held disagreeing caches.

Parts:
  1. THE LISTS ARE THE CHARTS'. haTopology.messaging is exactly the charts whose templates build a transport URL, and
     haTopology.cache exactly the charts (memcached aside) whose templates or values build an oslo_cache server list.
     Every messaging chart's own default lists the one host it would list at replicas 1 (so passing nothing at 1 is
     right), and haTopology.messagingStatefulset is the StatefulSet name the rabbitmq chart renders.
  2. THE UMBRELLA SPECS. The lines of compositions.yaml that build a component's spec are rendered for every component:
       a. ha off, one RabbitMQ replica: every spec equals the spec built without the ha block (nothing is passed);
       b. RabbitMQ at 3: each messaging component gains exactly endpoints.oslo_messaging.statefulset {name, replicas 3};
          every other component is unchanged;
       c. ha.enabled: memcached gains pod.replicas.server 3 and manifests.service_discovery; each cache component gains
          exactly endpoints.oslo_cache {hosts.default: discovery, statefulset: {name, replicas 3}}; nothing else moves;
       d. refused: memcached at 3 without ha; a component overriding the RabbitMQ count; a component setting an
          oslo_cache statefulset without ha. The schema refuses a non-boolean ha.enabled and an unknown ha key.
  3. THE CHARTS, WITH THOSE SPECS. memcached renders its StatefulSet as haTopology.cacheStatefulset with 3 replicas,
     governed by the headless Service haTopology.cacheDiscovery, which selects the StatefulSet's pods; the ClusterIP
     Service has no ClientIP affinity; a PodDisruptionBudget allows one disruption. At its defaults memcached keeps
     ClientIP, renders no discovery Service and no PDB. Then every messaging and cache chart is rendered with the spec
     the umbrella builds (ha on, RabbitMQ at 3), and every RabbitMQ URL it renders lists exactly the 3 server pods, and
     every memcache server list exactly the 3 memcached pods, each chart rendering at least one.
  4. THE NODE REFUSAL. osh.requireHaNodes, called with synthetic node lists: ha on with 2 labelled nodes is refused,
     naming the label; with 3 it renders; ha off renders; an empty list (not measured) renders. compositions.yaml calls
     it with the node list it looks up.

Fails when helm or PyYAML is missing, and when any part finds nothing to check.
"""
import base64
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


# What the umbrella delivers for a chart's storage (#308): cinder's default refuses, so a chart render carries
# hack/gate_values.py's arguments (nothing for every other chart).
import importlib.util as _gv_util
import os as _gv_os
_gv_spec = _gv_util.spec_from_file_location(
    "gate_values", _gv_os.path.join(_gv_os.path.dirname(_gv_os.path.abspath(__file__)), "gate_values.py"))
gate_values = _gv_util.module_from_spec(_gv_spec)
_gv_spec.loader.exec_module(gate_values)


# tooz 8.1.0 (openstack/requirements stable/2026.1 upper-constraints), pyproject.toml [project.entry-points."tooz.backends"]:
# the schemes a tooz coordination URL may name. H2 run 4: gnocchi rendered memcached-discovery://h0,h1,h2, which this gate
# counted as a valid memcache list (it compared only the hosts), and tooz refused it ("No 'tooz.backends' driver found").
TOOZ_DRIVERS = {"etcd", "etcd3+http", "etcd3+https", "kazoo", "memcached", "ipc", "redis", "postgresql", "mysql", "file",
                "zookeeper", "consul", "kubernetes"}
TOOZ_RX = re.compile(r"^\s*(coordination_url|backend_url)\s*=\s*(\S+)\s*$")
REDIS_URL_RX = re.compile(r"^\s*redis_url\s*=\s*(\S+)\s*$")


def tooz_problem(url):
    """None when url is a tooz URL a lock can live on: a registered driver and, unless it is a path, one host."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return "has no scheme"
    if scheme not in TOOZ_DRIVERS:
        return "names driver %r, which tooz 8.1.0 does not register (%s)" % (scheme, ", ".join(sorted(TOOZ_DRIVERS)))
    if scheme not in ("file", "ipc", "kubernetes"):
        netloc = rest.split("/", 1)[0].split("?", 1)[0].rsplit("@", 1)[-1]
        if "," in netloc or not netloc:
            return "names %s host(s) %r: a lock must live on exactly one backend" % ("no" if not netloc else "several", netloc)
    return None


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
                            *gate_values.helm_args(chart), "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def strings(node):
    """Every string in a rendered document, base64 Secret data decoded."""
    if isinstance(node, dict):
        for v in node.values():
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)
    elif isinstance(node, str):
        yield node


def doc_texts(d):
    if d.get("kind") == "Secret":
        for v in (d.get("data") or {}).values():
            try:
                yield base64.b64decode(v).decode("utf-8", "replace")
            except Exception:
                pass
        yield from strings(d.get("stringData") or {})
    else:
        yield from strings(d)


# --- 1. the lists are the charts' ---------------------------------------------------------------------------------
# A transport URL: built directly, or through the #228 transport drop-in (helm-toolkit transport_dropin).
TRANSPORT_RX = re.compile(r"authenticated_transport_endpoint_uri_lookup|helm-toolkit\.utils\.transport_url|"
                          r"helm-toolkit\.utils\.transport_dropin")
# A memcache server list. Between tokens: whitespace, and the backslashes of a YAML double-quoted scalar that carries
# the template inside values.yaml (horizon's local_settings), line continuations included.
_G = r'[\s\\]*'
CACHE_RX = re.compile(r'"oslo_cache\\?"' + _G + r'\\?"internal\\?"' + _G + r'\\?"memcache\\?"' + _G + r'\.' + _G +
                      r'\|' + _G + r'include' + _G + r'\\?"helm-toolkit\.endpoints\.host_and_port_endpoint_uri_lookup')


def chart_reads(chart):
    tdir = os.path.join(BP, chart, "chart", "templates")
    texts = []
    for dp, _, fs in os.walk(tdir):
        for f in fs:
            texts.append(open(os.path.join(dp, f), errors="replace").read())
    values = open(os.path.join(BP, chart, "chart", "values.yaml"), errors="replace").read()
    tpl = "\n".join(texts)
    return bool(TRANSPORT_RX.search(tpl)), bool(CACHE_RX.search(tpl) or CACHE_RX.search(values))


def part1(uv, tmp):
    topo = uv.get("haTopology") or {}
    names = {c["name"] for c in uv.get("components") or []}
    charts = sorted(d for d in os.listdir(BP) if os.path.isfile(os.path.join(BP, d, "chart", "values.yaml")))
    mq, by_service, cache, builds = set(), set(), set(), set()
    for c in charts:
        t, k = chart_reads(c)
        v = yaml.safe_load(open(os.path.join(BP, c, "chart", "values.yaml")))
        holds = "statefulset" in (((v.get("endpoints") or {}).get("oslo_messaging")) or {})
        if holds:
            mq.add(c)
        if t:
            builds.add(c)
            if not holds:
                by_service.add(c)
        if k and c != "memcached":
            cache.add(c)
    if not mq or not cache:
        fail("1: no chart builds a transport URL (%d) or a memcache list (%d): the scan found nothing to compare"
             % (len(mq), len(cache)))
    builds_by_list = {c for c in builds if c in mq}
    counts["builds"] = builds_by_list
    for label, want, have in (("messaging", mq, set(topo.get("messaging") or [])),
                              ("messagingByService", by_service, set(topo.get("messagingByService") or [])),
                              ("cache", cache, set(topo.get("cache") or []))):
        if want - have:
            fail("1: haTopology.%s misses %s, measured from its chart (messaging: holds endpoints.oslo_messaging."
                 "statefulset; messagingByService: builds a transport URL without one; cache: builds a memcache list)"
                 % (label, ", ".join(sorted(want - have))))
        if have - want:
            fail("1: haTopology.%s names %s, whose chart does not match it (see the line above for what each list means)"
                 % (label, ", ".join(sorted(have - want))))
        if have - names:
            fail("1: haTopology.%s names %s, which is not a component" % (label, ", ".join(sorted(have - names))))
    counts["1"] = (len(mq), len(cache))
    sts = topo.get("messagingStatefulset")
    for c in sorted(mq):
        v = yaml.safe_load(open(os.path.join(BP, c, "chart", "values.yaml")))
        s = ((v.get("endpoints") or {}).get("oslo_messaging") or {}).get("statefulset")
        if s is not None and (s.get("name") != sts or int(s.get("replicas") or 0) != 1):
            fail("1: %s's own endpoints.oslo_messaging.statefulset is %s, not {name: %s, replicas: 1}: at one RabbitMQ "
                 "replica the umbrella passes nothing, so the chart default must list the one host" % (c, s, sts))
    w = stage(os.path.join(BP, "rabbitmq", "chart"), tmp, "rabbitmq")
    rc, out, err = helm_template(w, "rabbitmq-gate1234", {})
    if rc != 0:
        fail("1: the rabbitmq chart does not render: %s" % err.strip()[:200])
        return
    rendered = [d["metadata"]["name"] for d in yaml.safe_load_all(out) if d and d.get("kind") == "StatefulSet"]
    if rendered != [sts]:
        fail("1: the rabbitmq chart renders StatefulSet(s) %s; haTopology.messagingStatefulset is %s" % (rendered, sts))


# --- 2. the umbrella specs ----------------------------------------------------------------------------------------
def spans():
    comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
    start = comp.find("{{- $ov := ")
    kog = comp.find("$.Values.kogCredentials", start)
    base_end = comp.find("{{- end }}", kog) if kog >= 0 else -1
    ha = comp.find('include "osh.haValues"', start)
    ha_end = comp.find("{{- end }}", ha) if ha >= 0 else -1
    if start < 0 or base_end < 0 or ha < 0 or ha_end < 0 or ha < base_end:
        return None, None
    return comp[start:base_end + len("{{- end }}")], comp[start:ha_end + len("{{- end }}")]


def probe_specs(u, vals):
    rc, out, err = helm_template(u, "openstack-gate", vals)
    if rc != 0:
        return None, err
    specs = {"HA": {}, "BASE": {}}
    for line in out.splitlines():
        m = re.match(r"^# (HA|BASE)SPEC (\S+) (.*)$", line)
        if m:
            specs[m.group(1)][m.group(2)] = json.loads(m.group(3))
    return specs, ""


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


def part2(uv, tmp):
    topo = uv["haTopology"]
    names = [c["name"] for c in uv["components"]]
    base_span, ha_span = spans()
    if base_span is None:
        fail("2: compositions.yaml builds no spec through osh.haValues after the kogCredentials merge, so nothing "
             "carries the host lists")
        return None, None
    u = stage(UMBRELLA, tmp, "umbrella")
    open(os.path.join(u, "templates", "zz-gate-ha-probe.yaml"), "w").write(
        '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
        '{{- range $c := .Values.components }}\n' + base_span + '\n# BASESPEC {{ $c.name }} {{ $ov | toJson }}\n'
        '{{- end }}\n'
        '{{- range $c := .Values.components }}\n' + ha_span + '\n# HASPEC {{ $c.name }} {{ $ov | toJson }}\n'
        '{{- end }}\n')
    mq3 = {"componentValues": {"rabbitmq": {"pod": {"replicas": {"server": 3}}}}}
    # profile full, so that every component's spec is complete (ceilometer's notification vhosts follow the selection).
    cases = {
        "off": {"profile": "full", "ha": {"enabled": False}},
        "absent": {"profile": "full", "ha": None},
        "mq3": dict(mq3, profile="full"),
        "ha": {"profile": "full", "ha": {"enabled": True}},
        "ha+mq3": dict(mq3, profile="full", ha={"enabled": True}),
    }
    specs = {}
    for k, vals in cases.items():
        s, err = probe_specs(u, vals)
        if s is None:
            fail("2: the umbrella probe does not render with %s: %s" % (k, err.strip()[:300]))
        else:
            specs[k] = s
    if len(specs) != len(cases):
        return None, None
    mqset, cacheset = set(topo["messaging"]), set(topo["cache"])
    mq_add = {"endpoints": {"oslo_messaging": {"statefulset": {"name": topo["messagingStatefulset"], "replicas": 3}}}}
    cache_add = {"endpoints": {"oslo_cache": {"hosts": {"default": topo["cacheDiscovery"]},
                                              "statefulset": {"name": topo["cacheStatefulset"], "replicas": 3}}}}
    mc_add = {"pod": {"replicas": {"server": 3}}, "manifests": {"service_discovery": True}}
    for n in names:
        for k in ("off", "absent"):
            if specs[k]["HA"].get(n) != specs[k]["BASE"].get(n):
                fail("2a: with ha %s and one RabbitMQ replica, %s's spec changes: %s, want %s" % (
                    k, n, json.dumps(specs[k]["HA"].get(n))[:200], json.dumps(specs[k]["BASE"].get(n))[:200]))
        want = specs["mq3"]["BASE"].get(n) or {}
        if n in mqset:
            want = merged(want, mq_add)
        if (specs["mq3"]["HA"].get(n) or {}) != want:
            fail("2b: with RabbitMQ at 3, %s's spec is %s, want %s" % (n, json.dumps(specs["mq3"]["HA"].get(n))[:200],
                                                                       json.dumps(want)[:200]))
        want = specs["ha"]["BASE"].get(n) or {}
        if n in cacheset:
            want = merged(want, cache_add)
        if n == "memcached":
            want = merged(want, mc_add)
        if (specs["ha"]["HA"].get(n) or {}) != want:
            fail("2c: with ha.enabled, %s's spec is %s, want %s" % (n, json.dumps(specs["ha"]["HA"].get(n))[:300],
                                                                    json.dumps(want)[:300]))
    counts["2"] = len(names)
    refusals = (
        ("memcached at 3 without ha", {"componentValues": {"memcached": {"pod": {"replicas": {"server": 3}}}}},
         "ha.enabled is false"),
        ("a component overriding the RabbitMQ count",
         dict(mq3, componentValues={"rabbitmq": {"pod": {"replicas": {"server": 3}}},
                                    "nova": {"endpoints": {"oslo_messaging": {"statefulset": {
                                        "name": topo["messagingStatefulset"], "replicas": 1}}}}}),
         "the one source"),
        ("an oslo_cache statefulset without ha",
         {"componentValues": {"keystone": {"endpoints": {"oslo_cache": {"statefulset": {
             "name": topo["cacheStatefulset"], "replicas": 3}}}}}},
         "ha.enabled is false"),
    )
    for label, vals, needle in refusals:
        rc, _, err = helm_template(u, "openstack-gate", vals)
        if rc == 0:
            fail("2d: %s renders; it must be refused" % label)
        elif needle not in err:
            fail("2d: %s is refused, but not by the ha check (no %r in): %s" % (label, needle, err.strip()[:300]))
    for label, vals in (("ha.enabled \"yes\"", {"ha": {"enabled": "yes"}}), ("an unknown ha key", {"ha": {"replicas": 3}})):
        rc, _, _ = helm_template(u, "openstack-gate", vals)
        if rc == 0:
            fail("2d: %s renders; the schema must refuse it" % label)
    return specs["ha+mq3"]["HA"], u


# --- 3. the charts with those specs ----------------------------------------------------------------------------------
def labels_match(selector, labels):
    return selector and all((labels or {}).get(k) == v for k, v in selector.items())


def part3(uv, ha_specs, tmp):
    topo = uv["haTopology"]
    w = stage(os.path.join(BP, "memcached", "chart"), tmp, "memcached")
    rc, out, err = helm_template(w, "memcached", ha_specs.get("memcached") or {})
    if rc != 0:
        fail("3: memcached does not render with the ha spec: %s" % err.strip()[:200])
    else:
        docs = [d for d in yaml.safe_load_all(out) if d]
        sts = [d for d in docs if d.get("kind") == "StatefulSet"]
        svcs = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Service"}
        pdbs = [d for d in docs if d.get("kind") == "PodDisruptionBudget"]
        if [d["metadata"]["name"] for d in sts] != [topo["cacheStatefulset"]]:
            fail("3: with ha, memcached renders StatefulSet(s) %s, want [%s]" % ([d["metadata"]["name"] for d in sts],
                                                                               topo["cacheStatefulset"]))
        else:
            s = sts[0]["spec"]
            podlabels = s["template"]["metadata"]["labels"]
            if s.get("replicas") != 3 or s.get("serviceName") != topo["cacheDiscovery"]:
                fail("3: memcached's StatefulSet has replicas %s and serviceName %s, want 3 and %s"
                     % (s.get("replicas"), s.get("serviceName"), topo["cacheDiscovery"]))
            disc = svcs.get(topo["cacheDiscovery"])
            if not disc or disc["spec"].get("clusterIP") != "None" or not labels_match(disc["spec"].get("selector"), podlabels):
                fail("3: no headless Service %s selecting memcached's pods: the pods would have no DNS names to list"
                     % topo["cacheDiscovery"])
            for name, svc in svcs.items():
                if svc["spec"].get("sessionAffinity") == "ClientIP":
                    fail("3: with ha, memcached's Service %s keeps ClientIP affinity" % name)
            if len(pdbs) != 1 or pdbs[0]["spec"].get("maxUnavailable") != 1 or "minAvailable" in pdbs[0]["spec"] \
                    or not labels_match(pdbs[0]["spec"]["selector"].get("matchLabels"), podlabels):
                fail("3: with 3 replicas memcached needs exactly one PodDisruptionBudget, maxUnavailable 1, selecting its "
                     "pods; got %s" % [p["spec"] for p in pdbs])
    rc, out, err = helm_template(w, "memcached", {})
    if rc != 0:
        fail("3: memcached does not render at its defaults: %s" % err.strip()[:200])
    else:
        docs = [d for d in yaml.safe_load_all(out) if d]
        svcs = [d for d in docs if d.get("kind") == "Service"]
        if [d["spec"].get("sessionAffinity") for d in svcs] != ["ClientIP"] \
                or any(d.get("kind") == "PodDisruptionBudget" for d in docs) \
                or [d["spec"]["serviceName"] for d in docs if d.get("kind") == "StatefulSet"] != ["memcached"]:
            fail("3: at its defaults memcached must render as before: one Service with ClientIP, no PDB, the StatefulSet "
                 "governed by memcached")
    want_mq = sorted("%s-%d.rabbitmq.%s:5672" % (topo["messagingStatefulset"], i, SUFFIX) for i in range(3))
    want_mc = sorted("%s-%d.%s.%s:11211" % (topo["cacheStatefulset"], i, topo["cacheDiscovery"], SUFFIX) for i in range(3))
    checked = {}
    for chart in sorted(set(topo["messaging"]) | set(topo["cache"])):
        w = stage(os.path.join(BP, chart, "chart"), tmp, "c-" + chart)
        rc, out, err = helm_template(w, chart + "-gate1234", ha_specs.get(chart) or {})
        if rc != 0:
            fail("3: %s does not render with the spec the umbrella builds: %s" % (chart, err.strip()[:200]))
            continue
        nmq = nmc = 0
        tooz_seen, redis_urls = [], []
        for d in yaml.safe_load_all(out):
            if not d:
                continue
            for text in doc_texts(d):
                for line in text.splitlines():
                    m = TOOZ_RX.match(line)
                    if m and m.group(2):
                        tooz_seen.append(m.group(2))
                        why = tooz_problem(m.group(2))
                        if why:
                            fail("3: %s renders %s = %s, which %s" % (chart, m.group(1), m.group(2)[:160], why))
                    m = REDIS_URL_RX.match(line)
                    if m:
                        redis_urls.append(m.group(1))
                    if chart in counts["builds"] and "rabbit://" in line and re.search(r"@[A-Za-z0-9.-]+:5672\b", line):
                        # Port 5672: a transport URL. rabbit-init's management connection (port 15672) is one host by
                        # design and is not a client's broker list.
                        hosts = sorted(re.findall(r"@([A-Za-z0-9.-]+:\d+)", line))
                        nmq += 1
                        if hosts != want_mq:
                            fail("3: %s renders a RabbitMQ URL listing %s, want %s: %s"
                                 % (chart, hosts, want_mq, line.strip()[:160]))
                    if ":11211" in line:
                        hosts = sorted(re.findall(r"([A-Za-z0-9.-]+:11211)", line))
                        nmc += 1
                        if hosts != want_mc:
                            fail("3: %s renders a memcache list naming %s, want %s: %s"
                                 % (chart, hosts, want_mc, line.strip()[:160]))
        # gnocchi coordinates on its redis metric store when coordination_url is unset (gnocchi/service.py
        # prepare_service): then that redis URL is the lock backend and must satisfy the same rule.
        if chart == "gnocchi":
            if not tooz_seen and not redis_urls:
                fail("3: gnocchi renders no coordination_url and no redis_url with the ha spec: no lock backend at all")
            for u in ([] if tooz_seen else redis_urls):
                why = tooz_problem(u)
                if why:
                    fail("3: gnocchi coordinates on its redis_url %s, which %s" % (u[:160], why))
            counts["tooz"] = counts.get("tooz", 0) + len(tooz_seen or redis_urls)
        else:
            counts["tooz"] = counts.get("tooz", 0) + len(tooz_seen)
        if chart in counts.get("builds", set()) and not nmq:
            fail("3: %s builds a transport URL from endpoints.oslo_messaging.statefulset but renders none" % chart)
        if chart in topo["cache"] and not nmc:
            fail("3: %s is in haTopology.cache but renders no memcache server list at its defaults" % chart)
        checked[chart] = (nmq, nmc)
    counts["3"] = checked


# --- 4. the node refusal --------------------------------------------------------------------------------------------
def part4(u):
    comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
    nodes_at = comp.find("{{- $nodes := list }}")
    call = comp.find('include "osh.requireHaNodes" (list $ $nodes)')
    loop = comp.find("{{- range $c := .Values.components }}")
    if call < 0 or nodes_at < 0 or not (nodes_at < call < loop):
        fail("4: compositions.yaml does not call osh.requireHaNodes with the node list it looks up, above the component "
             "loop: the refusal would never see a cluster")
    probe = os.path.join(u, "templates", "zz-gate-ha-probe.yaml")
    if os.path.exists(probe):
        os.unlink(probe)
    open(os.path.join(u, "templates", "zz-gate-ha-nodes.yaml"), "w").write(
        '{{- $nodes := list }}\n'
        '{{- range $l := (.Values.gateNodes | default list) }}'
        '{{- $nodes = append $nodes (dict "metadata" (dict "labels" $l)) }}{{- end }}\n'
        '{{- include "osh.requireHaNodes" (list $ $nodes) -}}\n')
    lab = {uv_topo["nodeLabel"]["key"]: str(uv_topo["nodeLabel"]["value"])}
    other = {"kubernetes.io/os": "linux"}
    cases = (
        ("ha on, 2 of 3 nodes labelled", {"ha": {"enabled": True}, "gateNodes": [lab, lab, other]}, False),
        ("ha on, 3 nodes labelled", {"ha": {"enabled": True}, "gateNodes": [lab, lab, lab]}, True),
        ("ha off, no node labelled", {"ha": {"enabled": False}, "gateNodes": [other]}, True),
        ("ha on, no node list (not measured)", {"ha": {"enabled": True}, "gateNodes": []}, True),
    )
    for label, vals, ok in cases:
        rc, _, err = helm_template(u, "openstack-gate", vals)
        if ok and rc != 0:
            fail("4: %s is refused: %s" % (label, err.strip()[:200]))
        if not ok:
            if rc == 0:
                fail("4: %s renders; it must be refused" % label)
            elif "%s=%s" % (uv_topo["nodeLabel"]["key"], uv_topo["nodeLabel"]["value"]) not in err:
                fail("4: %s is refused without naming the label: %s" % (label, err.strip()[:200]))
    counts["4"] = len(cases)


uv_topo = {}


def main():
    global uv_topo
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    uv_topo = uv.get("haTopology") or {}
    if not uv.get("components") or not uv_topo.get("messaging") or not uv_topo.get("cache"):
        print("FAILED MEASUREMENT: the umbrella lists no component or no haTopology")
        sys.exit(2)
    with tempfile.TemporaryDirectory() as tmp:
        part1(uv, tmp)
        ha_specs, u = part2(uv, tmp)
        if ha_specs is not None:
            part3(uv, ha_specs, tmp)
            part4(u)
    for k in ("1", "2", "3", "4", "tooz"):
        if not counts.get(k):
            fail("part %s checked nothing" % k)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    mq, mc = counts["1"]
    print("OK: haTopology matches the charts (%d hold the RabbitMQ host list, %d build a memcache list); %d component specs: ha off "
          "and one RabbitMQ replica pass nothing, RabbitMQ at 3 and ha move only their readers, the refusals hold; "
          "%d charts rendered with the ha spec list every RabbitMQ and memcached pod; %d tooz coordination URL(s) name a "
          "registered driver and one backend; the node refusal holds in %d cases"
          % (mq, mc, counts["2"], len(counts["3"]), counts["tooz"], counts["4"]))


main()
