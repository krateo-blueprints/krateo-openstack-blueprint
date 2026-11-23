#!/usr/bin/env python3
"""OpenStack services call each other on the internal endpoints (#462).

    hack/validate-internal-clients.py [repo-root] [--self-test]

Measured on GKE 2026-11-23 (0.3.74): trove answered 500 to every instance list, because its clients default to
`publicURL` and nothing serves the public endpoints while publicEndpoints is off. The defaults live in the services, not
in our charts, so a chart that says nothing inherits them; no render can show that.

CLIENTS is that list, read from each service's stable/2026.1 source on 2026-11-24. For every entry, the chart is rendered at
its defaults and the option must be in the rendered <file> with the expected value.
  * An entry the chart does not render fails. So does one that reads `public`.
  * Every other `*endpoint_type` / `*interface(s)` / `*catalog*info` option rendered as public must be in PUBLIC_ON_PURPOSE
    with a reason (a URL handed to something outside the cluster), and every PUBLIC_ON_PURPOSE entry must still be rendered.
  * The umbrella: a service that keeps one Keystone session for its process's lifetime (its catalog is then the one of
    its first call) depends on what it calls. CACHES_SESSION names them and their dependencies.

Fails when helm or PyYAML is missing, and when a chart does not render.
"""
import base64
import configparser
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

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
ROOT = os.path.abspath(ARGS[0] if ARGS else ".")
sys.path.insert(0, os.path.join(ROOT, "hack"))
import gate_values  # noqa: E402

TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")

# chart -> (rendered config file, {(section, option): required value})
CLIENTS = {
    "trove": ("trove.conf", {
        ("DEFAULT", "nova_compute_endpoint_type"): "internalURL",
        ("DEFAULT", "neutron_endpoint_type"): "internalURL",
        ("DEFAULT", "cinder_endpoint_type"): "internalURL",
        ("DEFAULT", "glance_endpoint_type"): "internalURL",
        ("DEFAULT", "trove_endpoint_type"): "internalURL"}),
    "masakari": ("masakari.conf", {("DEFAULT", "nova_catalog_admin_info"): "compute:nova:internalURL"}),
    "magnum": ("magnum.conf", {("%s_client" % s, "endpoint_type"): "internalURL"
                               for s in ("nova", "neutron", "heat", "glance", "barbican", "cinder", "octavia")}),
    "designate": ("designate.conf", {("network_api:neutron", "endpoint_type"): "internalURL"}),
    "octavia": ("octavia.conf", {
        ("nova", "endpoint_type"): "internalURL", ("cinder", "endpoint_type"): "internalURL",
        ("glance", "endpoint_type"): "internalURL", ("certificates", "endpoint_type"): "internalURL",
        ("neutron", "valid_interfaces"): "internal"}),
    "mistral": ("mistral.conf", {("openstack_actions", "os_actions_endpoint_type"): "internal"}),
    "heat": ("heat.conf", {("clients", "endpoint_type"): "internalURL", ("clients_keystone", "endpoint_type"): "internalURL"}),
    "manila": ("manila.conf", {(s, "endpoint_type"): "internalURL" for s in ("nova", "neutron", "cinder", "glance")}),
    "cinder": ("cinder.conf", {("nova", "interface"): "internal"}),
}
# (chart, file, section, option) -> why this one stays public
PUBLIC_ON_PURPOSE = {
    ("heat", "heat.conf", "clients_heat", "endpoint_type"): "the URL heat gives to the instances of a stack, for signals",
}
# component -> the components its long-lived Keystone session calls
CACHES_SESSION = {
    "trove": ("nova", "neutron", "glance"),     # trove/common/clients_admin.py _SESSION, ADMIN_*_CLIENT
    "octavia": ("nova", "neutron", "glance"),   # octavia/common/clients.py class attributes
}
OPTION = re.compile(r"(endpoint_type|interfaces?|catalog.*info)$")
PUBLIC = re.compile(r"(^|:)public(URL)?$")


def ini(text):
    p = configparser.RawConfigParser(strict=False, default_section="\0none", interpolation=None)
    p.optionxform = str
    p.read_string(text)
    return {(s, o): v.strip() for s in p.sections() for o, v in p.items(s)}


def judge(chart, files, clients, on_purpose):
    """Failures for one chart. files: {name: {(section, option): value}} rendered by it."""
    bad = []
    want = clients.get(chart)
    if want:
        name, opts = want
        if name not in files:
            return ["%s renders no %s, so its clients were not checked" % (chart, name)]
        for (s, o), v in sorted(opts.items()):
            got = files[name].get((s, o))
            if got != v:
                bad.append("%s %s [%s] %s is %r, want %r: the service's own default is the public endpoint" % (chart, name, s, o, got, v))
    for name, conf in sorted(files.items()):
        for (s, o), v in sorted(conf.items()):
            if OPTION.search(o) and any(PUBLIC.search(x.strip()) for x in v.split(",")):
                if (chart, name, s, o) not in on_purpose:
                    bad.append("%s %s [%s] %s = %s: a client on the public endpoint, with no reason in PUBLIC_ON_PURPOSE" % (chart, name, s, o, v))
    for (c, name, s, o) in on_purpose:
        if c == chart and not PUBLIC.search(files.get(name, {}).get((s, o), "")):
            bad.append("PUBLIC_ON_PURPOSE %s %s [%s] %s is not rendered as public: remove the entry" % (c, name, s, o))
    return bad


def judge_deps(components, caches):
    deps = {c["name"]: set(c.get("deps") or []) for c in components}
    bad = []
    for c, need in sorted(caches.items()):
        if c not in deps:
            bad.append("CACHES_SESSION names %s, which is not a component" % c)
            continue
        missing = [x for x in need if x not in deps[c]]
        if missing:
            bad.append("%s can start before %s: it would keep a catalog without them until its token expires" % (c, ", ".join(missing)))
    return bad


def render(chart, tmp):
    src = os.path.join(ROOT, "blueprints", chart, "chart")
    w = os.path.join(tmp, chart)
    shutil.copytree(src, w)
    cy = os.path.join(w, "Chart.yaml")
    text = open(cy).read()
    open(cy, "w").write(text.replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in text:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    r = subprocess.run(["helm", "template", chart, w, "--namespace", "openstack"] + gate_values.helm_args(w),
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, r.stderr.strip()[-300:]
    files = {}
    for d in yaml.safe_load_all(r.stdout):
        if not d or d.get("kind") not in ("Secret", "ConfigMap"):
            continue
        for k, v in (d.get("stringData") or {}).items():
            files[k] = v
        for k, v in (d.get("data") or {}).items():
            files[k] = base64.b64decode(v).decode("utf-8", "replace") if d["kind"] == "Secret" else v
    out = {}
    for k, v in files.items():
        if k.endswith(".conf") and isinstance(v, str):
            try:
                out[k] = ini(v)
            except configparser.Error:
                pass    # Apache, mpm and uwsgi files end in .conf too
    return out, ""


def self_test():
    cl = {"a": ("a.conf", {("DEFAULT", "nova_endpoint_type"): "internalURL"})}
    op = {("a", "a.conf", "clients_x", "endpoint_type"): "why"}
    good = {"a.conf": {("DEFAULT", "nova_endpoint_type"): "internalURL", ("clients_x", "endpoint_type"): "publicURL"}}
    cases = [
        ("as it must be", judge("a", good, cl, op), 0),
        ("the option unset, 0.3.74's trove", judge("a", {"a.conf": {("clients_x", "endpoint_type"): "publicURL"}}, cl, op), 1),
        ("the option public (its own line and the public rule)", judge("a", {"a.conf": {("DEFAULT", "nova_endpoint_type"): "publicURL", ("clients_x", "endpoint_type"): "publicURL"}}, cl, op), 2),
        ("a file that is not rendered", judge("a", {}, cl, op), 1),
        ("another client made public", judge("a", dict(good, **{"b.conf": {("nova", "valid_interfaces"): "internal,public"}}), cl, op), 1),
        ("a stale exception", judge("a", {"a.conf": {("DEFAULT", "nova_endpoint_type"): "internalURL"}}, cl, op), 1),
        ("masakari's triple, public", judge("b", {"m.conf": {("DEFAULT", "nova_catalog_admin_info"): "compute:nova:publicURL"}}, {}, {}), 1),
        ("an unrelated option that contains the word", judge("b", {"m.conf": {("DEFAULT", "public_network"): "public"}}, {}, {}), 0),
        ("deps missing", judge_deps([{"name": "t", "deps": ["keystone"]}], {"t": ("nova",)}), 1),
        ("deps there", judge_deps([{"name": "t", "deps": ["nova"]}], {"t": ("nova",)}), 0),
        ("a component that is gone", judge_deps([], {"t": ("nova",)}), 1),
    ]
    bad = [(n, got, want) for n, got, want in cases if len(got) != want]
    for n, got, want in bad:
        print("SELF-TEST FAIL: %s: %d finding(s), want %d: %s" % (n, len(got), want, got))
    print("self-test: %d case(s), %d wrong" % (len(cases), len(bad)))
    return 1 if bad else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    if shutil.which("helm") is None:
        print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
        return 2
    values = yaml.safe_load(open(os.path.join(ROOT, "blueprints", "openstack", "chart", "values.yaml")))
    fails = judge_deps(values["components"], CACHES_SESSION)
    charts = sorted(c["name"] for c in values["components"]
                    if os.path.isdir(os.path.join(ROOT, "blueprints", c["name"], "chart")))
    for c in sorted(set(CLIENTS) | {x[0] for x in PUBLIC_ON_PURPOSE}):
        if c not in charts:
            fails.append("%s is in this gate's lists and is not a component" % c)
    n = 0
    with tempfile.TemporaryDirectory() as tmp:
        for c in charts:
            files, err = render(c, tmp)
            if files is None:
                fails.append("%s does not render: %s" % (c, err))
                continue
            n += 1
            fails += judge(c, files, CLIENTS, PUBLIC_ON_PURPOSE)
    for f in fails:
        print("FAIL: " + f)
    print("%d chart(s) rendered, %d client option(s) required, %d component(s) with a long-lived session; %d failure(s)"
          % (n, sum(len(o) for _, o in CLIENTS.values()), len(CACHES_SESSION), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
