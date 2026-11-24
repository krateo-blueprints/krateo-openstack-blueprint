#!/usr/bin/env python3
"""Every URL the charts register in Keystone's catalog is served, with publicEndpoints off (#451).

    hack/validate-catalog-endpoints.py [repo-root] [--self-test]

Measured on GKE (0.3.71 to 0.3.74): with publicEndpoints off, none of the 22 public endpoints was served. 15 named a Service
that is never created, 5 one that selects no pod, 2 a port it does not have. A check that a Service of that name exists
called those 5 fine.

Every component chart is rendered at its defaults. From its ks-endpoints Job (OS_SERVICE_TYPE, OS_SVC_ENDPOINT, OS_SERVICE_ENDPOINT)
and keystone's db-sync Job (OS_BOOTSTRAP_*_URL), every URL must:
  1. name <service>.openstack.svc.cluster.local, a Service rendered by the same chart;
  2. use a port that Service has;
  3. reach a pod: the Service's selector matches the labels of a rendered workload's pods, and the Service port's targetPort
     is a port of one of its containers.
For every service type, admin, internal and public must all be registered. And the fallback rule, helm-toolkit's served_endpoint,
rendered through a probe chart: an explicit host_fqdn_override, an IP or a map keeps the endpoint's own address.

Fails when helm or PyYAML is missing, when a chart does not render, and when fewer than MIN_TYPES service types are found.
"""
import copy
import os
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
ROOT = os.path.abspath(ARGS[0] if ARGS else ".")
sys.path.insert(0, os.path.join(ROOT, "hack"))
import gate_values  # noqa: E402

TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
NS, SUFFIX = "openstack", ".openstack.svc.cluster.local"
MIN_TYPES = 22
INTERFACES = ("admin", "internal", "public")


def env(container):
    return {e["name"]: e.get("value") for e in container.get("env") or [] if "value" in e}


def catalog(docs):
    """[(type, interface, url)] the chart registers."""
    out = []
    for d in docs:
        if d.get("kind") != "Job":
            continue
        for c in d["spec"]["template"]["spec"].get("containers") or []:
            e = env(c)
            if e.get("OS_SERVICE_ENDPOINT") and e.get("OS_SVC_ENDPOINT") and e.get("OS_SERVICE_TYPE"):
                out.append((e["OS_SERVICE_TYPE"], e["OS_SVC_ENDPOINT"], e["OS_SERVICE_ENDPOINT"]))
            for i in INTERFACES:
                if e.get("OS_BOOTSTRAP_%s_URL" % i.upper()):
                    out.append(("identity", i, e["OS_BOOTSTRAP_%s_URL" % i.upper()]))
    return out


def pods(docs):
    for d in docs:
        if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet"):
            t = d["spec"]["template"]
            yield t["metadata"].get("labels") or {}, t["spec"].get("containers") or []


def serves(svc, port, docs):
    """None if a rendered pod answers on this Service port, else why not."""
    sp = [p for p in svc["spec"].get("ports") or [] if p.get("port") == port]
    if not sp:
        return "Service %s has no port %s (it has %s)" % (svc["metadata"]["name"], port, [p.get("port") for p in svc["spec"].get("ports") or []])
    sel = svc["spec"].get("selector") or {}
    if not sel:
        return "Service %s has no selector" % svc["metadata"]["name"]
    hit = [cs for labels, cs in pods(docs) if all(labels.get(k) == v for k, v in sel.items())]
    if not hit:
        return "Service %s selects no rendered pod (%s)" % (svc["metadata"]["name"], sel)
    want = sp[0].get("targetPort", port)
    for cs in hit:
        for c in cs:
            for p in c.get("ports") or []:
                if want in (p.get("name"), p.get("containerPort")):
                    return None
    return "Service %s port %s goes to %r, which no container of its pods has" % (svc["metadata"]["name"], port, want)


def judge(chart, docs):
    svcs = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Service"}
    bad, types = [], {}
    for t, i, url in catalog(docs):
        types.setdefault(t, set()).add(i)
        u = urlparse(url)
        host, port = u.hostname or "", u.port or {"http": 80, "https": 443}.get(u.scheme)
        where = "%s: %s/%s %s" % (chart, t, i, url)
        if not host.endswith(SUFFIX) or "." in host[:-len(SUFFIX)]:
            bad.append("%s is not a Service of this namespace" % where)
            continue
        name = host[:-len(SUFFIX)]
        if name not in svcs:
            bad.append("%s names Service %s, which the chart does not render" % (where, name))
            continue
        why = serves(svcs[name], port, docs)
        if why:
            bad.append("%s: %s" % (where, why))
    for t, seen in sorted(types.items()):
        missing = [i for i in INTERFACES if i not in seen]
        if missing:
            bad.append("%s registers no %s endpoint for %s" % (chart, "/".join(missing), t))
    return bad, types


def stage(src, tmp, name):
    w = os.path.join(tmp, name)
    shutil.copytree(src, w)
    cy = os.path.join(w, "Chart.yaml")
    text = open(cy).read()
    open(cy, "w").write(text.replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in text:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    return w


def template(w, name, extra=()):
    r = subprocess.run(["helm", "template", name, w, "--namespace", NS] + list(extra), capture_output=True, text=True)
    if r.returncode != 0:
        return None, r.stderr.strip()[-300:]
    return [d for d in yaml.safe_load_all(r.stdout) if d], ""


BASE = {"hosts": {"default": "a-api", "public": "a"}, "host_fqdn_override": {"default": None}, "scheme": {"default": "http"},
        "path": {"default": "/v1"}, "port": {"api": {"default": 1234, "public": 80}}}
RULE = [
    ("no address given: the internal endpoint", {}, "public", "http://a-api.openstack.svc.cluster.local:1234/v1"),
    ("admin, no address given", {"hosts": {"default": "a-api", "admin": "a"}}, "admin", "http://a-api.openstack.svc.cluster.local:1234/v1"),
    ("internal is untouched", {"hosts": {"default": "a", "internal": "a-int"}}, "internal", "http://a-int.openstack.svc.cluster.local:1234/v1"),
    ("publicEndpoints' override", {"host_fqdn_override": {"public": {"host": "a.example.com"}}, "scheme": {"default": "http", "public": "https"},
                                   "port": {"api": {"default": 1234, "public": 443}}}, "public", "https://a.example.com/v1"),
    ("a string override", {"host_fqdn_override": {"public": "a.example.com"}}, "public", "http://a.example.com/v1"),
    ("a default override", {"host_fqdn_override": {"default": "a.example.com"}}, "public", "http://a.example.com/v1"),
    ("barbican's override: a map with tls and no host", {"host_fqdn_override": {"default": {"tls": {"secretName": "x"}}}}, "public", "http://a-api.openstack.svc.cluster.local:1234/v1"),
    ("a default override map with a host", {"host_fqdn_override": {"default": {"host": "a.example.com"}}}, "public", "http://a.example.com/v1"),
    ("an IP address", {"hosts": {"default": "a-api", "public": "192.0.2.7"}}, "public", "http://192.0.2.7/v1"),
    ("a map host", {"hosts": {"default": "a-api", "public": {"host": "a"}}}, "public", "http://a.openstack.svc.cluster.local/v1"),
]


def rule(tmp):
    w = os.path.join(tmp, "probe")
    os.makedirs(os.path.join(w, "templates"))
    open(os.path.join(w, "Chart.yaml"), "w").write("apiVersion: v2\nname: probe\nversion: 0.0.1\n")
    shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    open(os.path.join(w, "templates", "u.yaml"), "w").write('url: {{ tuple "a" .Values.which "api" . | include "helm-toolkit.endpoints.keystone_endpoint_uri_lookup" | quote }}\n')
    bad = []
    for name, over, which, want in RULE:
        ep = copy.deepcopy(BASE)
        ep.update(over)
        vals = {"which": which, "endpoints": {"cluster_domain_suffix": "cluster.local", "a": ep}}
        p = os.path.join(tmp, "v.yaml")
        yaml.safe_dump(vals, open(p, "w"))
        docs, err = template(w, "probe", ["-f", p])
        got = docs[0].get("url") if docs else err
        if got != want:
            bad.append("the fallback rule, %s: %s, want %s" % (name, got, want))
    return bad


def self_test():
    def job(t, i, url):
        return {"kind": "Job", "spec": {"template": {"spec": {"containers": [{"env": [{"name": "OS_SERVICE_TYPE", "value": t}, {"name": "OS_SVC_ENDPOINT", "value": i},
                                                                                     {"name": "OS_SERVICE_ENDPOINT", "value": url}]}]}}}}

    def svc(name, port, sel, target="api"):
        return {"kind": "Service", "metadata": {"name": name}, "spec": {"selector": sel, "ports": [{"port": port, "targetPort": target}]}}
    dep = {"kind": "Deployment", "spec": {"template": {"metadata": {"labels": {"app": "a"}}, "spec": {"containers": [{"ports": [{"name": "api", "containerPort": 1234}]}]}}}}
    U = "http://a-api.openstack.svc.cluster.local:1234/v1"
    good = [job("x", i, U) for i in INTERFACES] + [svc("a-api", 1234, {"app": "a"}), dep]
    pub = lambda url, *more: [job("x", "admin", U), job("x", "internal", U), job("x", "public", url), svc("a-api", 1234, {"app": "a"}), dep] + list(more)
    cases = [
        ("served", good, 0),
        ("0.3.74's heat: a Service nobody renders", pub("http://a.openstack.svc.cluster.local/v1"), 1),
        ("0.3.74's keystone: a Service that selects no pod", pub("http://a.openstack.svc.cluster.local/v1", svc("a", 80, {"app": "ingress-api"})), 1),
        ("0.3.74's masakari: a port the Service lacks", pub("http://a-api.openstack.svc.cluster.local/v1"), 1),
        ("a targetPort no container has", [job("x", i, U) for i in INTERFACES] + [svc("a-api", 1234, {"app": "a"}, "other"), dep], 3),
        ("a host outside the namespace", pub("http://a.example.com/v1"), 1),
        ("an interface not registered", good[1:], 1),
        ("no selector", pub("http://b.openstack.svc.cluster.local:1234/", svc("b", 1234, {})), 1),
    ]
    wrong = [(n, judge("a", d)[0], w) for n, d, w in cases if len(judge("a", d)[0]) != w]
    for n, g, w in wrong:
        print("SELF-TEST FAIL: %s: %d finding(s), want %d: %s" % (n, len(g), w, g))
    print("self-test: %d case(s), %d wrong" % (len(cases), len(wrong)))
    return 1 if wrong else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    if shutil.which("helm") is None:
        print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
        return 2
    comps = [c["name"] for c in yaml.safe_load(open(os.path.join(ROOT, "blueprints", "openstack", "chart", "values.yaml")))["components"]]
    fails, types, n = [], set(), 0
    with tempfile.TemporaryDirectory() as tmp:
        fails += rule(tmp)
        for c in comps:
            src = os.path.join(ROOT, "blueprints", c, "chart")
            if not os.path.isdir(src):
                continue
            w = stage(src, tmp, c)
            docs, err = template(w, c, gate_values.helm_args(w))
            if docs is None:
                fails.append("%s does not render: %s" % (c, err))
                continue
            n += 1
            bad, t = judge(c, docs)
            fails += bad
            types |= set(t)
    if len(types) < MIN_TYPES:
        fails.append("only %d service types found, fewer than %d: the Jobs' shape changed and this gate sees too little" % (len(types), MIN_TYPES))
    for f in fails:
        print("FAIL: " + f)
    print("%d chart(s) rendered, %d service types x 3 interfaces, %d fallback case(s); %d failure(s)" % (n, len(types), len(RULE), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
