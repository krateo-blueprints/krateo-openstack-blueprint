#!/usr/bin/env python3
"""publicEndpoints exposes every public endpoint of the selected components, and nothing else (#311).

    hack/validate-public-endpoints.py [repo-root] [--components a,b,...]

The umbrella value publicEndpoints renders one Gateway API Gateway, an HTTPRoute per public endpoint, a cert-manager
Certificate and an ingress NetworkPolicy for the install namespace, and moves each component's public endpoint to its
hostname (templates/public-endpoints.yaml, osh.publicEndpointValues in templates/_public-endpoints.tpl). The
route table (publicEndpointRoutes) and the per-component table (publicEndpointComponents) are properties of the
charts, so they are checked against the charts here, not trusted.

  1. OFF CHANGES NOTHING. With publicEndpoints absent, and with publicEndpoints.enabled false and every other field set,
     the umbrella renders byte-identical, renders none of the four kinds, and every component's spec (the
     lines of compositions.yaml that build it, rendered for every component) is identical to the spec built without
     the publicEndpoints merge.
  2. THE EDGE (umbrella render, profile full, publicEndpoints on):
     a. every catalog service a component chart registers (its job-ks-endpoint*.yaml service types, and keystone's
        identity) plus Horizon and the noVNC proxy has exactly one HTTPRoute, at <host>.<baseDomain>, and there is no
        other route;
     b. no route sends to metadata, RabbitMQ, MariaDB or memcached (by Service name or port);
     c. every HTTPS route sets Strict-Transport-Security (max-age > 0) on every response and X-Forwarded-Proto: https on
        every request; the HTTP listener only redirects to https; the HTTPS listener's certificate is the
        Certificate's Secret, and that Certificate names every routed hostname;
     d. the NetworkPolicy set denies ingress by default and allows exactly: this namespace, the node CIDRs, the
        Gateway's proxy pods (to the routed backends' target ports), the internet to those proxy pods only, and
        allowNamespaces. Any other peer that crosses into the namespace fails. "Every pod" must be the block-style
        selector on krateo.io/networkpolicy-exempt (DoesNotExist; a same-namespace peer may add its Exists
        complement); an empty selector `{}`, or any flow collection in the rendered file, fails;
     e. the render is refused, naming the reason, when baseDomain, the issuer or (with the policy on) nodeCIDRs is
        missing, and, on a live cluster (cluster reads stubbed), when the Gateway API or cert-manager CRDs are absent.
  3. THE COMPONENTS: each component chart rendered with the spec the umbrella gives it:
     a. every route's backend is a Service the chart renders, on that port;
     b. the public catalog URL (ks-endpoints, keystone's bootstrap) is https://<host>.<baseDomain><path>, with no port;
     c. no Ingress, and no Service selecting the ingress controller (app: ingress-api), is rendered;
     d. every oslo.middleware service behind a route parses proxy headers ([oslo_middleware]
        enable_proxy_headers_parsing = true in its rendered <name>.conf);
     e. Horizon renders no NodePort or LoadBalancer Service, and its settings are Secure and trust only
        https://horizon.<baseDomain>; nova's noVNC base URL is https://novncproxy.<baseDomain>/...; glance's CORS
        origin is Horizon's public hostname; ironic's swift endpoint is swift's;
     f. publicEndpoints sets no client interface/endpoint_type option in any component, so in-cluster clients stay
        on the internal endpoint (hack/validate-client-endpoint-interface.py, #222).
  4. THE TABLES AGAINST THE CHARTS (static): publicEndpointComponents.<c>.ingressManifests is exactly every
     manifests.* switch of chart <c> that names an ingress; every chart template that renders another component's
     routed public endpoint lists it in referencedEndpoints; every routed oslo component sets proxyHeaders.
  M. THE CHECKS SEE WHAT THEY GUARD: each check of 2 and 3e is run again on a mutated copy of the rendered objects
     (a route dropped, a metadata route added, HSTS removed, Horizon given a NodePort, a namespaceSelector peer added,
     the node or gateway rule removed, the internet allowed to every pod, a plain-http catalog URL, an empty `{}`
     selector restored on the default deny and on the same-namespace peer), and must report
     THAT defect, not merely something.

--components limits part 3 to the named components, to keep a local run light; CI runs it whole. Fails when helm
or PyYAML is missing, when a chart does not render, and when any part finds nothing to check.
"""
import argparse
import configparser
import copy
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

DOMAIN = "cloud.example.test"
ISSUER = "gate-ca"
NODE_CIDRS = ["192.168.10.0/24", "fd00:10::/64"]
NS = "openstack"
ON = {
    "profile": "full",
    "publicEndpoints": {
        "enabled": True,
        "baseDomain": DOMAIN,
        "tls": {"issuerRef": {"name": ISSUER}},
        "networkPolicy": {"nodeCIDRs": NODE_CIDRS},
    },
}
# Public endpoints that are not catalog services: (component, endpoints key).
UI_ENDPOINTS = {("horizon", "dashboard"), ("nova", "compute_novnc_proxy")}
DENY_NAME = re.compile(r"^(nova-metadata|mariadb.*|rabbitmq.*|memcached.*)$")
DENY_PORTS = {8775, 3306, 4444, 4567, 4568, 5672, 15672, 25672, 4369, 11211}
# Routed components that are not oslo.middleware services, so carry no proxyHeaders.
NOT_OSLO = {
    "horizon": "Django: SECURE_PROXY_SSL_HEADER, set by the dashboard switch",
    "swift": "swift's proxy-server is not an oslo.middleware application",
}
INTERNET = {"0.0.0.0/0", "::/0"}
GW_LABEL = "gateway.networking.k8s.io/gateway-name"
# "Every pod", in block style: every pod without the opt-out label. Its complement (Exists) completes a peer that must
# admit every pod of the namespace. An empty selector `{}` is refused: the repo writes YAML in block style only.
EXEMPT = "krateo.io/networkpolicy-exempt"
ALL_PODS = {"matchExpressions": [{"key": EXEMPT, "operator": "DoesNotExist"}]}
EXEMPT_PODS = {"matchExpressions": [{"key": EXEMPT, "operator": "Exists"}]}

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


def fail(msg):
    failures.append(msg)


def count(part, n=1):
    counts[part] = counts.get(part, 0) + n


def measurement_failed(msg):
    print("FAILED MEASUREMENT: " + msg)
    sys.exit(2)


def stage(src, tmp, name, toolkit):
    w = os.path.join(tmp, name)
    shutil.copytree(src, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in txt:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        if not os.path.exists(os.path.join(w, "charts", "helm-toolkit")):
            shutil.copytree(toolkit, os.path.join(w, "charts", "helm-toolkit"))
    return w


def helm_template(chart, release, values, *extra):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    try:
        r = subprocess.run(["helm", "template", release, chart, "--namespace", NS, "--kube-version", "1.36.0",
                            *gate_values.helm_args(chart), "-f", f.name, *extra], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def docs_of(out):
    return [d for d in yaml.safe_load_all(out) if isinstance(d, dict)]


def stub_define(src, name, body):
    start = src.find('{{- define "%s" -}}' % name)
    if start < 0:
        measurement_failed("%s is not defined in _public-endpoints.tpl, so the cluster read cannot be stubbed" % name)
    nxt = src.find("\n{{- define ", start + 1)
    nxt = len(src) if nxt < 0 else nxt
    close = src.rfind("{{- end -}}", start, nxt)
    if close < 0:
        measurement_failed("cannot find the closing marker of %s" % name)
    return src[:start] + '{{- define "%s" -}}%s{{- end -}}' % (name, body) + src[close + len("{{- end -}}"):]


# ---------------------------------------------------------------------------------------------------- the checks
# Each takes rendered objects and returns [(tag, message)]. The tags are what part M asserts.

def check_routes(docs, routes_table, expected):
    """2a/2b: one HTTPS route per expected public endpoint, no other, none to a denied backend."""
    out = []
    https = [d for d in docs if d.get("kind") == "HTTPRoute" and
             any(p.get("sectionName") == "https" for p in d["spec"].get("parentRefs") or [])]
    by_host = {}
    for d in https:
        for h in d["spec"].get("hostnames") or []:
            by_host.setdefault(h, []).append(d)
    for comp, key in sorted(expected):
        ent = [r for r in routes_table if r["component"] == comp and
               (r.get("catalog") == key or (key == r["endpoint"] and (comp, key) in UI_ENDPOINTS))]
        if len(ent) != 1:
            out.append(("route-missing", "%s %s has %d entries in publicEndpointRoutes, needs exactly one"
                        % (comp, key, len(ent))))
            continue
        fqdn = "%s.%s" % (ent[0]["host"], DOMAIN)
        got = by_host.get(fqdn, [])
        if len(got) != 1:
            out.append(("route-missing", "%s %s: %d HTTPRoute(s) serve %s, needs exactly one" % (comp, key, len(got), fqdn)))
            continue
        refs = [b for rule in got[0]["spec"].get("rules") or [] for b in rule.get("backendRefs") or []]
        if [(b.get("name"), b.get("port")) for b in refs] != [(ent[0]["backend"]["name"], ent[0]["backend"]["port"])]:
            out.append(("route-backend", "%s: the route sends to %s, the table says %s:%s"
                        % (fqdn, [(b.get("name"), b.get("port")) for b in refs], ent[0]["backend"]["name"],
                           ent[0]["backend"]["port"])))
    want_hosts = set()
    for comp, key in expected:
        for r in routes_table:
            if r["component"] == comp and (r.get("catalog") == key or ((comp, key) in UI_ENDPOINTS and key == r["endpoint"])):
                want_hosts.add("%s.%s" % (r["host"], DOMAIN))
    for h in sorted(set(by_host) - want_hosts):
        out.append(("route-extra", "%s is routed but is no public endpoint of a selected component" % h))
    for d in docs:
        if d.get("kind") != "HTTPRoute":
            continue
        for rule in d["spec"].get("rules") or []:
            for b in rule.get("backendRefs") or []:
                if DENY_NAME.match(str(b.get("name"))) or b.get("port") in DENY_PORTS:
                    out.append(("route-denied", "%s routes to %s:%s, which must never be public"
                                % (d["metadata"]["name"], b.get("name"), b.get("port"))))
        for h in d["spec"].get("hostnames") or []:
            if h.split(".")[0] in ("metadata", "rabbitmq", "mariadb", "memcached"):
                out.append(("route-denied", "%s serves %s" % (d["metadata"]["name"], h)))
    return out


def check_tls(docs):
    """2c: HSTS and X-Forwarded-Proto on every HTTPS rule, redirect-only HTTP, certificate covers every host."""
    out = []
    gws = [d for d in docs if d.get("kind") == "Gateway"]
    certs = [d for d in docs if d.get("kind") == "Certificate"]
    if len(gws) != 1 or len(certs) != 1:
        return [("tls-objects", "%d Gateway(s) and %d Certificate(s) rendered, want one each" % (len(gws), len(certs)))]
    listeners = {l["name"]: l for l in gws[0]["spec"]["listeners"]}
    https_l, http_l = listeners.get("https"), listeners.get("http")
    if not https_l or https_l.get("protocol") != "HTTPS" or https_l.get("port") != 443:
        out.append(("tls-listener", "the Gateway has no HTTPS listener on 443 named https"))
    else:
        refs = [r.get("name") for r in (https_l.get("tls") or {}).get("certificateRefs") or []]
        if refs != [certs[0]["spec"]["secretName"]]:
            out.append(("tls-listener", "the HTTPS listener serves %s, the Certificate writes %s"
                        % (refs, certs[0]["spec"]["secretName"])))
    if certs[0]["spec"].get("issuerRef", {}).get("name") != ISSUER:
        out.append(("tls-issuer", "the Certificate is not issued by publicEndpoints.tls.issuerRef"))
    dns = set(certs[0]["spec"].get("dnsNames") or [])
    for d in docs:
        if d.get("kind") != "HTTPRoute":
            continue
        sections = {p.get("sectionName") for p in d["spec"].get("parentRefs") or []}
        name = d["metadata"]["name"]
        if sections == {"https"}:
            for h in d["spec"].get("hostnames") or []:
                if h not in dns:
                    out.append(("tls-cert-host", "%s serves %s, which the Certificate does not name" % (name, h)))
            for rule in d["spec"].get("rules") or []:
                hsts = xfp = False
                for f in rule.get("filters") or []:
                    if f.get("type") == "ResponseHeaderModifier":
                        for s in (f.get("responseHeaderModifier") or {}).get("set") or []:
                            m = re.match(r"^max-age=(\d+)$", str(s.get("value")))
                            if s.get("name") == "Strict-Transport-Security" and m and int(m.group(1)) > 0:
                                hsts = True
                    if f.get("type") == "RequestHeaderModifier":
                        for s in (f.get("requestHeaderModifier") or {}).get("set") or []:
                            if s.get("name") == "X-Forwarded-Proto" and s.get("value") == "https":
                                xfp = True
                if not hsts:
                    out.append(("hsts", "%s: a rule sets no Strict-Transport-Security max-age" % name))
                if not xfp:
                    out.append(("xfp", "%s: a rule does not set X-Forwarded-Proto: https" % name))
        elif sections == {"http"}:
            for rule in d["spec"].get("rules") or []:
                redirect = [f for f in rule.get("filters") or [] if f.get("type") == "RequestRedirect"
                            and (f.get("requestRedirect") or {}).get("scheme") == "https"]
                if rule.get("backendRefs") or not redirect:
                    out.append(("http-not-redirect", "%s: a plain-HTTP rule does something other than redirect to "
                                "https" % name))
        else:
            out.append(("route-parent", "%s attaches to %s, not exactly one listener" % (name, sorted(sections))))
    return out


def check_netpol(docs, node_cidrs, proxy_ns, allow_ns, gw_name, target_ports):
    """2d: default deny, and exactly the allowed peers."""
    out = []
    pols = [d for d in docs if d.get("kind") == "NetworkPolicy"]
    deny = [p for p in pols if p["spec"].get("podSelector") == ALL_PODS and "Ingress" in p["spec"].get("policyTypes", [])
            and not p["spec"].get("ingress")]
    if not deny:
        out.append(("np-no-default-deny", "no policy denies ingress to every pod by default"))
    seen_nodes, seen_gw, seen_internet = set(), False, False
    for p in pols:
        name = p["metadata"]["name"]
        sel = p["spec"].get("podSelector")
        if sel is None or sel == {}:
            out.append(("np-empty-selector", "%s selects its pods with an empty selector; write every pod as %s"
                        % (name, json.dumps(ALL_PODS))))
            sel = {}
        selects_gw = sel == {"matchLabels": {GW_LABEL: gw_name}}
        if sel and sel != ALL_PODS and not selects_gw:
            out.append(("np-selector", "%s selects %s, neither every pod nor the Gateway's proxy pods"
                        % (name, json.dumps(sel, sort_keys=True))))
        if "Egress" in p["spec"].get("policyTypes", []):
            out.append(("np-egress", "%s restricts egress; the phase-1 policy is ingress only" % name))
        for rule in p["spec"].get("ingress") or []:
            peers = rule.get("from")
            if not peers:
                out.append(("np-open", "%s has an ingress rule with no `from`: it admits every source" % name))
                continue
            for peer in peers:
                ipb, nss, pods = peer.get("ipBlock"), peer.get("namespaceSelector"), peer.get("podSelector")
                if ipb:
                    cidr = ipb.get("cidr")
                    if ipb.get("except"):
                        out.append(("np-peer", "%s: an ipBlock with except" % name))
                    if cidr in node_cidrs and not selects_gw:
                        seen_nodes.add(cidr)
                    elif cidr in INTERNET and selects_gw and proxy_ns == NS:
                        seen_internet = True
                    else:
                        out.append(("np-peer", "%s admits %s%s, which is neither a node CIDR nor the internet to "
                                    "the Gateway's own proxy pods" % (name, cidr, "" if selects_gw else
                                                                      " to every selected pod")))
                elif nss is not None:
                    ns = (nss.get("matchLabels") or {}).get("kubernetes.io/metadata.name")
                    extra = {k: v for k, v in nss.items() if k != "matchLabels"} or \
                        {k: v for k, v in (nss.get("matchLabels") or {}).items() if k != "kubernetes.io/metadata.name"}
                    if ns is None or extra:
                        out.append(("np-cross-namespace", "%s admits a namespaceSelector %s that names no single "
                                    "namespace" % (name, json.dumps(nss, sort_keys=True))))
                    elif ns == proxy_ns and (pods or {}).get("matchLabels") == {GW_LABEL: gw_name}:
                        seen_gw = True
                        ports = sorted(int(x.get("port")) for x in rule.get("ports") or [])
                        if target_ports is not None and set(ports) != set(target_ports):
                            out.append(("np-gateway-ports", "%s lets the Gateway reach ports %s; the routed "
                                        "backends listen on %s" % (name, ports, sorted(target_ports))))
                        if not ports:
                            out.append(("np-gateway-ports", "%s lets the Gateway reach every port" % name))
                    elif ns in allow_ns and pods is None:
                        pass
                    else:
                        out.append(("np-cross-namespace", "%s admits namespace %s, which is not the Gateway's and "
                                    "not in allowNamespaces" % (name, ns)))
                elif pods is not None:
                    if pods == {}:
                        out.append(("np-empty-selector", "%s admits a peer with an empty podSelector; write every pod "
                                    "as %s or its complement" % (name, json.dumps(ALL_PODS))))
                    # any other podSelector-only peer is a same-namespace peer
                else:
                    out.append(("np-peer", "%s has a peer of no known shape: %s" % (name, peer)))
    if seen_nodes != set(node_cidrs):
        out.append(("np-no-nodes", "the node CIDRs admitted are %s, publicEndpoints.networkPolicy.nodeCIDRs is %s"
                    % (sorted(seen_nodes), sorted(node_cidrs))))
    if not seen_gw:
        out.append(("np-no-gateway", "no policy admits the Gateway's proxy pods (%s=%s in namespace %s)"
                    % (GW_LABEL, gw_name, proxy_ns)))
    if proxy_ns == NS and not seen_internet:
        out.append(("np-no-listeners", "the proxy runs in this namespace but no policy admits the internet to it, "
                    "so the default deny cuts off the Gateway's own listeners"))
    return out


def check_horizon(docs):
    """3e: no NodePort/LoadBalancer, and Secure settings trusting only the public origin."""
    out = []
    for d in docs:
        if d.get("kind") == "Service":
            t = d["spec"].get("type", "ClusterIP")
            if t in ("NodePort", "LoadBalancer") or any("nodePort" in p for p in d["spec"].get("ports") or []):
                out.append(("horizon-nodeport", "horizon renders Service %s of type %s" % (d["metadata"]["name"], t)))
    settings = ""
    for d in docs:
        if d.get("kind") in ("ConfigMap", "Secret"):
            for k, v in (d.get("data") or {}).items():
                if k == "local_settings":
                    settings = base64.b64decode(v).decode() if d["kind"] == "Secret" else v
    if not settings:
        return out + [("horizon-settings", "horizon renders no local_settings")]
    want = ["USE_SSL = True", "CSRF_COOKIE_SECURE = True", "SESSION_COOKIE_SECURE = True",
            "SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')",
            "CSRF_TRUSTED_ORIGINS = ['https://horizon.%s']" % DOMAIN]
    for w in want:
        if w not in settings:
            out.append(("horizon-settings", "horizon's local_settings lacks `%s`" % w))
    return out


def public_urls(docs):
    """{service type: public URL} from ks-endpoints Jobs, and keystone's bootstrap public URL as identity."""
    urls = {}
    for d in docs:
        if d.get("kind") != "Job":
            continue
        for c in d["spec"]["template"]["spec"].get("containers") or []:
            env = {e["name"]: e.get("value") for e in c.get("env") or [] if "value" in e}
            if env.get("OS_SVC_ENDPOINT") == "public" and env.get("OS_SERVICE_TYPE"):
                urls[env["OS_SERVICE_TYPE"]] = env.get("OS_SERVICE_ENDPOINT")
            if env.get("OS_BOOTSTRAP_PUBLIC_URL"):
                urls["identity"] = env["OS_BOOTSTRAP_PUBLIC_URL"]
    return urls


def check_catalog(urls, entries):
    out = []
    for r in entries:
        if not r.get("catalog"):
            continue
        u = urls.get(r["catalog"])
        want = "https://%s.%s" % (r["host"], DOMAIN)
        if u is None:
            out.append(("catalog-missing", "%s registers no public %s endpoint" % (r["component"], r["catalog"])))
        elif not (u == want or u.startswith(want + "/")):
            out.append(("catalog-url", "%s's public %s endpoint is %s, want %s<path>" % (r["component"], r["catalog"],
                                                                                         u, want)))
    return out


def conf_files(docs):
    files = {}
    for d in docs:
        if d.get("kind") not in ("ConfigMap", "Secret"):
            continue
        for k, v in (d.get("data") or {}).items():
            if d["kind"] == "Secret":
                try:
                    v = base64.b64decode(v).decode("utf-8", "replace")
                except Exception:
                    continue
            files.setdefault(k, []).append(str(v))
    return files


# ---------------------------------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--components", default="")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    bp = os.path.join(root, "blueprints")
    umbrella = os.path.join(bp, "openstack", "chart")
    toolkit = os.path.join(root, "charts", "helm-toolkit")
    if shutil.which("helm") is None:
        measurement_failed("helm not on PATH, so nothing was rendered")
    uv = yaml.safe_load(open(os.path.join(umbrella, "values.yaml")))
    names = [c["name"] for c in uv.get("components") or []]
    table = uv.get("publicEndpointRoutes") or []
    ctable = uv.get("publicEndpointComponents") or {}
    if not names or not table or not ctable:
        measurement_failed("the umbrella lists no components, no publicEndpointRoutes or no publicEndpointComponents")
    only = set(x for x in a.components.split(",") if x)

    # The public endpoints each chart actually has: what it registers in the catalog, plus the UI endpoints.
    expected = set()
    for n in names:
        tdir = os.path.join(bp, n, "chart", "templates")
        if not os.path.isdir(tdir):
            continue
        for f in sorted(os.listdir(tdir)):
            if f.startswith("job-ks-endpoint") and f.endswith(".yaml"):
                s = open(os.path.join(tdir, f)).read()
                t = re.search(r'serviceTypes" \( tuple ([^)]*)\)', s)
                types = re.findall(r'"([a-z0-9-]+)"', t.group(1)) if t else []
                if not types:
                    fail("2a: %s/%s registers catalog endpoints but no service type could be read from it" % (n, f))
                for ty in types:
                    expected.add((n, ty))
    if "keystone" in names:
        expected.add(("keystone", "identity"))
    expected |= {u for u in UI_ENDPOINTS if u[0] in names}
    if len(expected) < 10:
        measurement_failed("only %d public endpoints were found in the charts" % len(expected))

    with tempfile.TemporaryDirectory() as tmp:
        u = stage(umbrella, tmp, "umbrella", toolkit)

        # ---------------- part 1: off changes nothing
        off_variants = [
            ("publicEndpoints absent", {"profile": "full"}),
            ("publicEndpoints.enabled false, every other field set",
             {"profile": "full", "publicEndpoints": dict(copy.deepcopy(ON["publicEndpoints"]), enabled=False)}),
        ]
        outs = []
        for label, vals in off_variants:
            rc, out, err = helm_template(u, "openstack-gate", vals)
            if rc != 0:
                fail("1: %s: the umbrella does not render: %s" % (label, err.strip()[-300:]))
                continue
            outs.append(out)
            for d in docs_of(out):
                if d.get("kind") in ("Gateway", "HTTPRoute", "Certificate", "NetworkPolicy"):
                    fail("1: %s renders %s %s" % (label, d["kind"], d["metadata"]["name"]))
            count("1")
        if len(outs) == 2 and outs[0] != outs[1]:
            fail("1: setting publicEndpoints with enabled false changes the umbrella's render")
        comp = open(os.path.join(umbrella, "templates", "compositions.yaml")).read()
        start = comp.find("{{- $ov := ")
        mark = comp.find('include "osh.publicEndpointValues"', start)
        end = comp.find("{{- end }}", mark) if mark >= 0 else -1
        if start < 0 or end < 0:
            measurement_failed("compositions.yaml does not merge osh.publicEndpointValues into $ov (the span from "
                               "`{{- $ov := ` through that merge), so no component receives it")
        span = comp[start:end + len("{{- end }}")]
        block_start = comp.rfind("{{- /* Public endpoints (#311)", start, mark)
        if block_start < 0:
            measurement_failed("cannot find the start of the publicEndpoints block in compositions.yaml")
        without = comp[start:block_start]
        probe = ('{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
                 '{{- range $c := .Values.components }}\n{{- if has $c.name $sel }}\n%s\n'
                 '# PESPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n{{- end }}\n')
        specs = {}
        for key, body, vals in (("off-without", without, {"profile": "full"}), ("off-with", span, {"profile": "full"}),
                                ("on", span, ON)):
            open(os.path.join(u, "templates", "zz-gate-pe-probe.yaml"), "w").write(probe % body)
            rc, out, err = helm_template(u, "openstack-gate", vals, "--show-only", "templates/zz-gate-pe-probe.yaml")
            if rc != 0:
                measurement_failed("the compositions.yaml probe does not render (%s): %s" % (key, err.strip()[-400:]))
            specs[key] = {l.split(" ", 3)[2]: l.split(" ", 3)[3] for l in out.splitlines() if l.startswith("# PESPEC ")}
        os.unlink(os.path.join(u, "templates", "zz-gate-pe-probe.yaml"))
        if sorted(specs["off-with"]) != sorted(names):
            fail("1: the probe saw %d component specs, the umbrella lists %d" % (len(specs["off-with"]), len(names)))
        for n in names:
            if specs["off-with"].get(n) != specs["off-without"].get(n):
                fail("1: with publicEndpoints off, %s's spec differs from the spec built without the merge" % n)
            count("1")

        # 3f: publicEndpoints sets no client endpoint-interface option, so hack/validate-client-endpoint-interface.py's
        # guarantee (in-cluster clients use the internal endpoint, #222) holds with it on.
        for n in names:
            on_s, off_s = specs["on"].get(n) or "", specs["off-with"].get(n) or ""
            for opt in ("interface", "endpoint_type", "valid_interfaces", "os_endpoint_type"):
                if on_s.count('"%s"' % opt) != off_s.count('"%s"' % opt):
                    fail("3f: publicEndpoints changes %s's client option %s; in-cluster clients must stay internal"
                         % (n, opt))
            count("3f")

        # ---------------- part 2: the edge
        rc, out, err = helm_template(u, "openstack-gate", ON)
        if rc != 0:
            measurement_failed("the umbrella does not render with publicEndpoints on: %s" % err.strip()[-400:])
        on_docs = docs_of(out)
        edge = [d for d in on_docs if d.get("kind") in ("Gateway", "HTTPRoute", "Certificate", "NetworkPolicy")]
        rc, edge_text, err = helm_template(u, "openstack-gate", ON, "--show-only", "templates/public-endpoints.yaml")
        for i, line in enumerate(edge_text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r":\s*(\{\}|\[\])\s*$", code) or re.search(r"^\s*-\s*(\{|\[)", code):
                fail("2d [flow-style]: templates/public-endpoints.yaml renders a flow collection at line %d: %s"
                     % (i, line.strip()))
        count("2f")
        if not any(d["kind"] == "HTTPRoute" for d in edge):
            measurement_failed("publicEndpoints on rendered no HTTPRoute at all")
        r_out = check_routes(edge, table, expected)
        t_out = check_tls(edge)
        for tag, m in r_out:
            fail("2a/b [%s]: %s" % (tag, m))
        for tag, m in t_out:
            fail("2c [%s]: %s" % (tag, m))
        count("2a", len(expected))
        count("2c", sum(1 for d in edge if d["kind"] == "HTTPRoute"))

        # 2e: refusals
        for label, vals, needle in (
                ("no baseDomain", {"publicEndpoints": {"enabled": True, "tls": {"issuerRef": {"name": ISSUER}},
                                                       "networkPolicy": {"nodeCIDRs": NODE_CIDRS}}}, "baseDomain"),
                ("no issuer", {"publicEndpoints": {"enabled": True, "baseDomain": DOMAIN,
                                                   "networkPolicy": {"nodeCIDRs": NODE_CIDRS}}}, "issuerRef.name"),
                ("policy without nodeCIDRs", {"publicEndpoints": {"enabled": True, "baseDomain": DOMAIN,
                                                                  "tls": {"issuerRef": {"name": ISSUER}}}},
                 "nodeCIDRs is empty")):
            rc, _, err = helm_template(u, "openstack-gate", vals)
            if rc == 0 or needle not in err:
                fail("2e: %s: the render is not refused with a message naming %s" % (label, needle))
            count("2e")
        rc, _, err = helm_template(u, "openstack-gate", {"publicEndpoints": {
            "enabled": True, "baseDomain": DOMAIN, "tls": {"issuerRef": {"name": ISSUER}},
            "networkPolicy": {"enabled": False}}})
        if rc != 0:
            fail("2e: publicEndpoints with the policy off and no nodeCIDRs is refused: %s" % err.strip()[-200:])
        tplf = os.path.join(u, "templates", "_public-endpoints.tpl")
        orig = open(tplf).read()
        for label, served, want_ok in (("live, no Gateway API and no cert-manager", "", False),
                                       ("live, everything served", "true", True)):
            s = stub_define(orig, "osh.publicLiveCluster", "true")
            s = stub_define(s, "osh.publicCrdServed", served)
            open(tplf, "w").write(s)
            rc, _, err = helm_template(u, "openstack-gate", ON)
            if want_ok and rc != 0:
                fail("2e: %s: the render is refused: %s" % (label, err.strip()[-200:]))
            if not want_ok:
                names_missing = ["gateways.gateway.networking.k8s.io", "httproutes.gateway.networking.k8s.io",
                                 "certificates.cert-manager.io"]
                if rc == 0 or not all(x in err for x in names_missing):
                    fail("2e: %s: the render is not refused naming every absent CRD" % label)
            count("2e")
        open(tplf, "w").write(orig)

        # ---------------- part 3: the components, rendered with the spec the umbrella gives them
        target_ports = set()
        horizon_docs = None
        comp_docs = {}
        routed = sorted({r["component"] for r in table} | set(ctable))
        for n in routed:
            if n not in names:
                fail("3: publicEndpoint tables name %s, which is not an umbrella component" % n)
                continue
            if only and n not in only:
                continue
            spec = json.loads(specs["on"].get(n) or "{}")
            c = stage(os.path.join(bp, n, "chart"), tmp, "c-" + n, toolkit)
            rc, out, err = helm_template(c, "%s-gate" % n, spec)
            if rc != 0:
                fail("3: %s does not render with the spec publicEndpoints gives it: %s" % (n, err.strip()[-400:]))
                continue
            docs = docs_of(out)
            comp_docs[n] = docs
            count("3")
            entries = [r for r in table if r["component"] == n]
            svcs = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Service"}
            for r in entries:
                s = svcs.get(r["backend"]["name"])
                ports = [p for p in (s or {}).get("spec", {}).get("ports") or [] if p.get("port") == r["backend"]["port"]]
                if not ports:
                    fail("3a: %s: the route's backend %s:%s is not a Service port the chart renders"
                         % (n, r["backend"]["name"], r["backend"]["port"]))
                    continue
                tp = ports[0].get("targetPort", ports[0]["port"])
                if not isinstance(tp, int):
                    fail("3a: %s: %s's targetPort is the name %r; the gateway policy needs a number" % (n, s["metadata"]["name"], tp))
                else:
                    target_ports.add(tp)
                    count("3a")
            urls = public_urls(docs)
            for tag, m in check_catalog(urls, entries):
                fail("3b [%s]: %s" % (tag, m))
            count("3b", sum(1 for r in entries if r.get("catalog") and r["catalog"] in urls))
            for d in docs:
                if d.get("kind") == "Ingress":
                    fail("3c: %s still renders Ingress %s" % (n, d["metadata"]["name"]))
                if d.get("kind") == "Service" and (d["spec"].get("selector") or {}).get("app") == "ingress-api":
                    fail("3c: %s still renders Service %s, which selects the ingress controller" % (n, d["metadata"]["name"]))
            ph = (ctable.get(n) or {}).get("proxyHeaders")
            if ph:
                found = False
                files = conf_files(docs)
                texts = files.get(ph + ".conf", []) + files.get(ph + "-api.conf", [])
                if not texts:
                    fail("3d: %s renders neither %s.conf nor %s-api.conf, so its proxy-header setting was not measured"
                         % (n, ph, ph))
                for text in texts:
                    cp = configparser.RawConfigParser(strict=False)
                    try:
                        cp.read_string(text)
                    except configparser.Error:
                        continue
                    if cp.has_option("oslo_middleware", "enable_proxy_headers_parsing") and \
                            cp.get("oslo_middleware", "enable_proxy_headers_parsing").strip().lower() == "true":
                        found = True
                if found:
                    count("3d")
                else:
                    fail("3d: %s's rendered %s.conf does not set [oslo_middleware] enable_proxy_headers_parsing = true"
                         % (n, ph))
            if n == "horizon":
                horizon_docs = docs
                for tag, m in check_horizon(docs):
                    fail("3e [%s]: %s" % (tag, m))
            if n == "nova":
                want = "novncproxy_base_url = https://novncproxy.%s/" % DOMAIN
                if not any(want in t for k, ts in conf_files(docs).items() if k == "nova.conf" for t in ts):
                    fail("3e: nova.conf does not carry `%s...`" % want)
            if n == "ironic" and "swift" in names:
                want = "swift_endpoint_url = https://swift.%s" % DOMAIN
                if not any(want in t for t in conf_files(docs).get("ironic.conf", [])):
                    fail("3e: ironic.conf does not carry `%s`" % want)
            if n == "glance" and "horizon" in names:
                if not any("https://horizon.%s" % DOMAIN in t for t in conf_files(docs).get("glance-api.conf", [])):
                    fail("3e: glance-api.conf's CORS allowed_origin does not name https://horizon.%s" % DOMAIN)
        if not only:
            for tag, m in check_netpol(edge, NODE_CIDRS, NS, [], "openstack-gateway", sorted(target_ports)):
                fail("2d [%s]: %s" % (tag, m))
            count("2d")
        else:
            for tag, m in check_netpol(edge, NODE_CIDRS, NS, [], "openstack-gateway", None):
                fail("2d [%s]: %s" % (tag, m))
            count("2d")

    # ---------------- part 4: the tables against the charts (static)
    routed_eps = {r["endpoint"]: r["component"] for r in table}
    for n in names:
        vf = os.path.join(bp, n, "chart", "values.yaml")
        if not os.path.isfile(vf):
            continue
        v = yaml.safe_load(open(vf)) or {}
        ing = sorted(k for k in (v.get("manifests") or {}) if "ingress" in k)
        have = sorted((ctable.get(n) or {}).get("ingressManifests") or [])
        if ing != have:
            fail("4: %s's ingress switches are %s, publicEndpointComponents.%s.ingressManifests is %s" % (n, ing, n, have))
        count("4")
        for r in (x for x in table if x["component"] == n):
            ep = (v.get("endpoints") or {}).get(r["endpoint"])
            if not isinstance(ep, dict) or r["port"] not in (ep.get("port") or {}):
                fail("4: %s has no endpoints.%s.port.%s, which publicEndpointRoutes moves" % (n, r["endpoint"], r["port"]))
            if r["component"] not in NOT_OSLO and not (ctable.get(n) or {}).get("proxyHeaders"):
                fail("4: %s is routed but sets no proxyHeaders" % n)
        tdir = os.path.join(bp, n, "chart", "templates")
        refs = set()
        for f in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []:
            if "ingress" in f or f.startswith("job-ks-"):
                continue
            p = os.path.join(tdir, f)
            if not os.path.isfile(p):
                continue
            for m in re.finditer(r'tuple "([a-z0-9_-]+)" "public"', open(p).read()):
                ep = m.group(1).replace("-", "_")
                if ep in routed_eps and routed_eps[ep] != n:
                    refs.add(ep)
        listed = set((ctable.get(n) or {}).get("referencedEndpoints") or [])
        if refs != listed:
            fail("4: %s renders the public endpoint(s) %s of other components; referencedEndpoints lists %s"
                 % (n, sorted(refs), sorted(listed)))

    # ---------------- part M: each check reports the defect it guards
    def expect(part, tag, results):
        if not any(t == tag for t, _ in results):
            fail("M: the mutation for [%s] was not reported as %s (got %s)" % (tag, tag, sorted({t for t, _ in results})))
        count("M")

    def mutated(fn):
        d = copy.deepcopy(edge)
        fn(d)
        return d

    def drop_keystone(d):
        d[:] = [x for x in d if not (x.get("kind") == "HTTPRoute" and "keystone.%s" % DOMAIN in x["spec"].get("hostnames", [])
                                     and x["spec"]["parentRefs"][0].get("sectionName") == "https")]

    def add_metadata(d):
        r = copy.deepcopy(next(x for x in d if x.get("kind") == "HTTPRoute" and x["spec"]["parentRefs"][0].get("sectionName") == "https"))
        r["metadata"]["name"] = "evil-metadata"
        r["spec"]["hostnames"] = ["metadata.%s" % DOMAIN]
        r["spec"]["rules"][0]["backendRefs"] = [{"name": "nova-metadata", "port": 8775}]
        d.append(r)

    def drop_hsts(d):
        r = next(x for x in d if x.get("kind") == "HTTPRoute" and x["spec"]["parentRefs"][0].get("sectionName") == "https")
        r["spec"]["rules"][0]["filters"] = [f for f in r["spec"]["rules"][0]["filters"] if f["type"] != "ResponseHeaderModifier"]

    def policy(d, name):
        return next(x for x in d if x.get("kind") == "NetworkPolicy" and x["metadata"]["name"] == name)

    def any_namespace(d):
        policy(d, "openstack-allow-nodes")["spec"]["ingress"][0]["from"].append({"namespaceSelector": {}})

    def drop_nodes(d):
        d[:] = [x for x in d if not (x.get("kind") == "NetworkPolicy" and x["metadata"]["name"] == "openstack-allow-nodes")]

    def drop_gateway(d):
        d[:] = [x for x in d if not (x.get("kind") == "NetworkPolicy" and x["metadata"]["name"] == "openstack-allow-gateway")]

    def internet_everywhere(d):
        policy(d, "openstack-allow-nodes")["spec"]["ingress"][0]["from"].append({"ipBlock": {"cidr": "0.0.0.0/0"}})

    def empty_selector(d):
        policy(d, "openstack-default-deny-ingress")["spec"]["podSelector"] = {}

    def empty_peer(d):
        policy(d, "openstack-allow-same-namespace")["spec"]["ingress"][0]["from"] = [{"podSelector": {}}]

    tp = sorted(target_ports) if not only else None
    expect("M", "np-empty-selector", check_netpol(mutated(empty_selector), NODE_CIDRS, NS, [], "openstack-gateway", tp))
    expect("M", "np-no-default-deny", check_netpol(mutated(empty_selector), NODE_CIDRS, NS, [], "openstack-gateway", tp))
    expect("M", "np-empty-selector", check_netpol(mutated(empty_peer), NODE_CIDRS, NS, [], "openstack-gateway", tp))
    expect("M", "route-missing", check_routes(mutated(drop_keystone), table, expected))
    expect("M", "route-denied", check_routes(mutated(add_metadata), table, expected))
    expect("M", "hsts", check_tls(mutated(drop_hsts)))
    expect("M", "np-cross-namespace", check_netpol(mutated(any_namespace), NODE_CIDRS, NS, [], "openstack-gateway", tp))
    expect("M", "np-no-nodes", check_netpol(mutated(drop_nodes), NODE_CIDRS, NS, [], "openstack-gateway", tp))
    expect("M", "np-no-gateway", check_netpol(mutated(drop_gateway), NODE_CIDRS, NS, [], "openstack-gateway", tp))
    expect("M", "np-peer", check_netpol(mutated(internet_everywhere), NODE_CIDRS, NS, [], "openstack-gateway", tp))
    expect("M", "catalog-url", check_catalog({"identity": "http://keystone.%s/v3" % DOMAIN},
                                              [r for r in table if r["component"] == "keystone"]))
    if horizon_docs is not None:
        hd = copy.deepcopy(horizon_docs)
        for x in hd:
            if x.get("kind") == "Service":
                x["spec"]["type"] = "NodePort"
                x["spec"]["ports"][0]["nodePort"] = 31000
        expect("M", "horizon-nodeport", check_horizon(hd))
        hd2 = copy.deepcopy(horizon_docs)
        for x in hd2:
            if x.get("kind") in ("ConfigMap", "Secret") and "local_settings" in (x.get("data") or {}):
                v = x["data"]["local_settings"]
                txt = base64.b64decode(v).decode() if x["kind"] == "Secret" else v
                txt = txt.replace("SESSION_COOKIE_SECURE = True", "SESSION_COOKIE_SECURE = False")
                x["data"]["local_settings"] = base64.b64encode(txt.encode()).decode() if x["kind"] == "Secret" else txt
        expect("M", "horizon-settings", check_horizon(hd2))

    for part in ("1", "2a", "2c", "2d", "2e", "2f", "4", "M"):
        if not counts.get(part):
            failures.append("%s: checked nothing" % part)
    if not only:
        for part in ("3", "3a", "3b", "3d"):
            if not counts.get(part):
                failures.append("%s: checked nothing" % part)
    print("checked: " + ", ".join("%s=%d" % (k, counts[k]) for k in sorted(counts)))
    if failures:
        print("FAIL (%d):" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: publicEndpoints exposes every public endpoint of the selected components over HTTPS, and nothing else")
    return 0


if __name__ == "__main__":
    sys.exit(main())
