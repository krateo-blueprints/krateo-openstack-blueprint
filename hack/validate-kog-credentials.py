#!/usr/bin/env python3
"""Krateo KOG operators get credentials from the install's own generated admin root, and only they get them (issue #303).

    hack/validate-kog-credentials.py [repo-root]

kogCredentials.enabled (default false) has keystone render, in the release's namespace, from the identity-admin root it
already receives: openstack-kog-clouds, a clouds.yaml for the auth proxies of keystone-, neutron- and glance-kog (an
ExternalSecret); openstack-kog-token, an admin token for nova-kog; and openstack-kog-endpoints, a ConfigMap with nova's
and keystone's internal URLs and the region (nova-kog's nova.url). One pod spec, helm-toolkit.snippets.kog_endpoints_pod,
writes them: in mode keystone (keystone's hook Job and CronJob: the token, keystone_url, region_name, and nova_url once
nova is in the catalog) and in mode nova (nova's hook Job, once nova registered its endpoint: nova_url only). Nothing is
copied by hand (#228).

  1. THE UMBRELLA. The lines of compositions.yaml that build a component's spec are rendered for every component with
     kogCredentials absent, disabled and enabled: disabled changes no spec; enabled gives keystone and nova alone
     kog_credentials {enabled: true}. kogCredentials.namespace is refused by the schema: cross-namespace is not
     supported.
  2. OFF IS NOTHING. keystone and nova rendered with kog_credentials.enabled false are byte-identical to their renders
     without the key, and render no KOG object.
  3. THE CLOUDS FILE. Enabled, every KOG object is in the release's namespace; the ExternalSecret openstack-kog-clouds
     reads exactly the admin root through the install's store, holds exactly clouds.yaml, and its password is the root's
     placeholder: filled with a known value it parses, logs in as the admin user at keystone's internal endpoint,
     interface internal, and no value the chart publishes appears in it.
  4. THE JOBS. keystone's hook Job (post-install,post-upgrade) and CronJob (concurrencyPolicy Forbid), and nova's hook
     Job, all run backoffLimit 0, and their pod holds the admin password in exactly one container, the mint step (the
     chart's admin openrc): no other container references a Secret, and the pod mounts no Secret. The mint step tries at
     most 4 times (a bounded `for` and no other loop: keystone locks a user after 5 failures). All three run the same
     mint program. nova's Job waits for keystone, for nova's API and for nova-ks-endpoints. keystone's writer Role grants
     get and patch on exactly openstack-kog-token and openstack-kog-endpoints; nova's on exactly openstack-kog-endpoints.
     The Secret holds only token; the ConfigMap holds exactly nova_url, keystone_url and region_name, and in a render
     (nothing live) every one is empty: no value is assembled by the chart. nova renders none of the KOG objects.
  4m. THE MINT PROGRAM, RUN. It is run against a stand-in `openstack` module whose session answers like keystoneauth1's
     (measured on keystoneauth1 5.18.1, and in the ks_user image, openstacksdk 4.10.0 with keystoneauth1 5.13.1, against
     a stand-in keystone: get_endpoint returns the catalog URL as is, raises EndpointNotFound for an entry the catalog
     lacks, and each run logs in once). It must log in through the openrc (cloud envvars); write nova_url = nova's
     catalog URL plus the token's project id (not doubled when the catalog already carries it), keystone's internal URL
     and the openrc's region, and neither the token nor the password into the endpoints; fail on a compute URL that
     kept an unresolved template. Mode keystone writes the token and exits 0 with nova_url empty while the catalog has
     no compute entry (keystone installs before nova); mode nova writes no token and fails without compute.
  4w. THE WRITERS, RUN, AGAINST EACH OTHER. Each chart's writer program is run against a stand-in API server (TLS with
     a CA of the gate's own, merge patches applied as RFC 7386 says) in the order an install takes (keystone before
     nova registered, then nova's hook, then keystone's CronJob) and in the opposite order. nova's writer sends exactly
     one request, a merge patch of {nova_url} on openstack-kog-endpoints; keystone's never names nova_url while it has
     none; and in every order the ConfigMap ends with nova's nova_url, keystone's keystone_url and region_name, and the
     Secret with the token.
  5. NO OTHER READER. No pod keystone or nova renders mounts or references openstack-kog-clouds, openstack-kog-token or
     openstack-kog-endpoints: their readers are the KOGs, in their own releases.

Fails when helm, openssl or PyYAML is missing, and when any part finds nothing to check.
"""
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
BP = os.path.join(ROOT, "blueprints")
UMBRELLA = os.path.join(BP, "openstack", "chart")
KEYSTONE = os.path.join(BP, "keystone", "chart")
NOVA = os.path.join(BP, "nova", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
for tool in ("helm", "openssl"):
    if shutil.which(tool) is None:
        print("FAILED MEASUREMENT: %s not on PATH, so nothing was checked" % tool)
        sys.exit(2)
KNOWN = "GateKogAdmin7f3c"
NS = "openstack"
ENDPOINT_KEYS = ["keystone_url", "nova_url", "region_name"]
KOG_OBJECTS = {"openstack-kog-clouds", "openstack-kog-token", "openstack-kog-endpoints"}
CM_PATH = "configmaps/openstack-kog-endpoints"
SECRET_PATH = "secrets/openstack-kog-token"
REGION = "GateRegion"
PID = "0f3c9a7d2b1e4c5a8d6f"
TOKEN = "gAAAA-gate-token-81c2"
NOVA_CAT = "http://nova-api.openstack.svc.cluster.local:8774/v2.1/"
KS_CAT = "http://keystone-api.openstack.svc.cluster.local:5000/v3"
failures = []
counts = {}


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


def published(values):
    out = set()

    def walk(v, key=""):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, str(k))
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and v and re.search(r"(^|_)(password|passwd|secret|secret_key)$", key.lower()) \
                and len(v) >= 6 and "{{" not in v:
            out.add(v)
    walk(values)
    return out


def pod_specs(docs):
    for d in docs:
        k = d.get("kind")
        spec = d.get("spec") or {}
        if k == "CronJob":
            yield d, spec["jobTemplate"]["spec"]["template"]["spec"]
        elif k in ("Job", "Deployment", "StatefulSet", "DaemonSet"):
            yield d, spec["template"]["spec"]
        elif k == "Pod":
            yield d, spec


def secret_refs(c):
    refs = set()
    for e in c.get("env") or []:
        n = ((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
        if n:
            refs.add(n)
    for e in c.get("envFrom") or []:
        if (e.get("secretRef") or {}).get("name"):
            refs.add(e["secretRef"]["name"])
    return refs


def object_refs(pod):
    """Every Secret and ConfigMap a pod references, by env or by volume."""
    refs = set()
    for c in (pod.get("initContainers") or []) + (pod.get("containers") or []):
        refs |= secret_refs(c)
        for e in c.get("env") or []:
            n = ((e.get("valueFrom") or {}).get("configMapKeyRef") or {}).get("name")
            if n:
                refs.add(n)
        for e in c.get("envFrom") or []:
            if (e.get("configMapRef") or {}).get("name"):
                refs.add(e["configMapRef"]["name"])
    for v in pod.get("volumes") or []:
        if (v.get("secret") or {}).get("secretName"):
            refs.add(v["secret"]["secretName"])
        if (v.get("configMap") or {}).get("name"):
            refs.add(v["configMap"]["name"])
        for s in (v.get("projected") or {}).get("sources") or []:
            for kind in ("secret", "configMap"):
                if (s.get(kind) or {}).get("name"):
                    refs.add(s[kind]["name"])
    return refs


def env_value(c, name):
    return next((e.get("value") for e in c.get("env") or [] if e["name"] == name), None)


STUB = r'''
import json, os, sys, types
case = json.load(open(os.environ["GATE_CASE"]))


class EndpointNotFound(Exception):
    pass


class Session:
    def get_token(self):
        return case["token"]

    def get_project_id(self):
        return case["project"]

    def get_endpoint(self, service_type, interface, region_name):
        url = case["catalog"].get("%s/%s/%s" % (service_type, interface, region_name))
        if url is None:
            raise EndpointNotFound("%s endpoint for %s service in %s region not found" % (interface, service_type,
                                                                                        region_name))
        return url


class Conn:
    session = Session()


def connect(cloud=None, **kw):
    if cloud != "envvars" or kw:
        sys.exit("the mint step must log in through the admin openrc (cloud envvars), got cloud=%r %r" % (cloud, kw))
    return Conn()


openstack = types.ModuleType("openstack")
openstack.connect = connect
ksa = types.ModuleType("keystoneauth1")
exc = types.ModuleType("keystoneauth1.exceptions")
exc.EndpointNotFound = EndpointNotFound
ksa.exceptions = exc
sys.modules.update({"openstack": openstack, "keystoneauth1": ksa, "keystoneauth1.exceptions": exc})
'''


def mint(program, tmp, mode, compute):
    """Run the mint program in `mode` against a stand-in session whose catalog has `compute` (None: no entry)."""
    d = tempfile.mkdtemp(dir=tmp)
    catalog = {"identity/internal/" + REGION: KS_CAT}
    if compute is not None:
        catalog["compute/internal/" + REGION] = compute
    open(os.path.join(d, "case.json"), "w").write(json.dumps({"token": TOKEN, "project": PID, "catalog": catalog}))
    open(os.path.join(d, "mint.py"), "w").write(STUB + "\n" + program.replace("/run/kog/", d + "/"))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GATE_CASE": os.path.join(d, "case.json"),
           "OS_REGION_NAME": REGION, "OS_PASSWORD": KNOWN, "OS_USERNAME": "admin", "KOG_MODE": mode}
    r = subprocess.run([sys.executable, "-I", os.path.join(d, "mint.py")], capture_output=True, text=True, env=env, cwd=d)
    last = ((r.stderr or r.stdout).strip().splitlines() or [""])[-1]
    return r.returncode, d, last


def read(d, name):
    p = os.path.join(d, name)
    return open(p).read() if os.path.exists(p) else None


def run_mint(program, tmp):
    """4m: run the mint program in both modes against stand-in sessions."""
    resolved = NOVA_CAT.rstrip("/") + "/" + PID
    cases = [
        ("keystone", "compute registered", NOVA_CAT, resolved),
        ("keystone", "compute already per project", NOVA_CAT + PID, NOVA_CAT + PID),
        ("keystone", "compute not yet registered", None, ""),
        ("keystone", "compute template unresolved", NOVA_CAT + "%(tenant_id)s", "fail"),
        ("nova", "compute registered", NOVA_CAT, resolved),
        ("nova", "compute not yet registered", None, "fail"),
        ("nova", "compute template unresolved", NOVA_CAT + "%(tenant_id)s", "fail"),
    ]
    for mode, name, compute, want_nova in cases:
        rc, d, last = mint(program, tmp, mode, compute)
        counts["4m"] = counts.get("4m", 0) + 1
        label = "4m: mode %s, %s" % (mode, name)
        if want_nova == "fail":
            if rc == 0:
                fail("%s: the mint program exits 0; it must fail (an unresolved template, or nova's hook without "
                     "nova's own endpoint)" % label)
            continue
        if rc != 0:
            fail("%s: the mint program exits %d: %s" % (label, rc, last[:200]))
            continue
        tok, raw = read(d, "token"), read(d, "endpoints.json") or "{}"
        if mode == "keystone" and tok != TOKEN:
            fail("%s: the mint program wrote the token %r, want the session's" % (label, tok))
        if mode == "nova" and tok is not None:
            fail("%s: the mint program wrote a token in mode nova, which writes nova_url only" % label)
        want = {"nova_url": want_nova, "keystone_url": KS_CAT, "region_name": REGION}
        if json.loads(raw) != want:
            fail("%s: the mint program wrote the endpoints %s, want %s" % (label, json.loads(raw), want))
        if TOKEN in raw or KNOWN in raw:
            fail("%s: the endpoints the mint program writes hold the token or the password: they go into a ConfigMap"
                 % label)


def merge_patch(target, patch):
    """RFC 7386."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


class Api:
    """A stand-in Kubernetes API: the two objects keystone renders, merge-patched, every request recorded."""

    def __init__(self, tmp):
        self.dir = tempfile.mkdtemp(dir=tmp)
        self.cert, key = os.path.join(self.dir, "ca.crt"), os.path.join(self.dir, "key.pem")
        r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out",
                            self.cert, "-days", "1", "-subj", "/CN=127.0.0.1", "-addext",
                            "subjectAltName=IP:127.0.0.1"], capture_output=True, text=True)
        if r.returncode != 0:
            print("FAILED MEASUREMENT: openssl could not make the stand-in API server's certificate: " + r.stderr[-300:])
            sys.exit(2)
        self.sa_token = "gate-sa-token-5d1e"
        open(os.path.join(self.dir, "token"), "w").write(self.sa_token)
        self.reset()
        api = self

        class H(BaseHTTPRequestHandler):
            def do_PATCH(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                prefix = "/api/v1/namespaces/%s/" % NS
                path = self.path[len(prefix):] if self.path.startswith(prefix) else self.path
                api.requests.append((path, self.headers.get("Content-Type"), self.headers.get("Authorization"),
                                     json.loads(body or b"null")))
                if path not in api.objects or self.headers.get("Authorization") != "Bearer " + api.sa_token or \
                        self.headers.get("Content-Type") != "application/merge-patch+json":
                    self.send_response(404 if path not in api.objects else 400)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                api.objects[path] = merge_patch(api.objects[path], json.loads(body))
                data = json.dumps(api.objects[path]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self):
        # what keystone renders with nothing live
        self.objects = {SECRET_PATH: {"data": {"token": ""}}, CM_PATH: {"data": {k: "" for k in ENDPOINT_KEYS}}}
        self.requests = []

    def write(self, program, mode, run_dir):
        sa = os.path.join(self.dir, "sa")
        os.makedirs(sa, exist_ok=True)
        shutil.copy(self.cert, os.path.join(sa, "ca.crt"))
        shutil.copy(os.path.join(self.dir, "token"), os.path.join(sa, "token"))
        src = program.replace("/var/run/secrets/kubernetes.io/serviceaccount", sa).replace("/run/kog/", run_dir + "/")
        p = os.path.join(run_dir, "write.py")
        open(p, "w").write(src)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "KUBERNETES_SERVICE_HOST": "127.0.0.1",
               "KUBERNETES_SERVICE_PORT": str(self.server.server_address[1]), "KOG_NAMESPACE": NS, "KOG_MODE": mode}
        before = len(self.requests)
        r = subprocess.run([sys.executable, "-I", p], capture_output=True, text=True, env=env, cwd=run_dir)
        return r.returncode, ((r.stderr or r.stdout).strip().splitlines() or [""])[-1], self.requests[before:]


def run_writers(mint_program, writers, tmp):
    """4w: keystone's and nova's writers, run against each other in both orders."""
    if set(writers) != {"keystone", "nova"}:
        fail("4w: no writer program for %s, so the two writers could not be run against each other"
             % sorted({"keystone", "nova"} - set(writers)))
        return
    api = Api(tmp)
    resolved = NOVA_CAT.rstrip("/") + "/" + PID
    want = {"nova_url": resolved, "keystone_url": KS_CAT, "region_name": REGION}
    orders = (("install: keystone, then nova's hook, then keystone's CronJob",
               (("keystone", None), ("nova", NOVA_CAT), ("keystone", NOVA_CAT))),
              ("nova's hook first, then keystone before nova is in its catalog",
               (("nova", NOVA_CAT), ("keystone", None))))
    for oname, steps in orders:
        api.reset()
        for mode, compute in steps:
            rc, d, last = mint(mint_program, tmp, mode, compute)
            if rc != 0:
                fail("4w: %s: the mint program (mode %s) exits %d: %s" % (oname, mode, rc, last[:200]))
                break
            rc, last, reqs = api.write(writers[mode], mode, d)
            counts["4w"] = counts.get("4w", 0) + 1
            if rc != 0:
                fail("4w: %s: %s's writer exits %d: %s" % (oname, mode, rc, last[:200]))
                break
            bodies = [(p, b) for p, _, _, b in reqs]
            if mode == "nova" and bodies != [(CM_PATH, {"data": {"nova_url": resolved}})]:
                fail("4w: %s: nova's writer sent %s, want exactly one merge patch of {nova_url: %s} on %s"
                     % (oname, bodies, resolved, CM_PATH))
            if mode == "keystone":
                cm = [b for p, b in bodies if p == CM_PATH]
                if any(set((b or {}).get("data") or {}) - set(ENDPOINT_KEYS) for b in cm) or \
                        [p for p, _ in bodies if p not in (CM_PATH, SECRET_PATH)]:
                    fail("4w: %s: keystone's writer sent %s: only openstack-kog-token and the endpoint keys" % (oname,
                                                                                                             bodies))
                if compute is None and any("nova_url" in ((b or {}).get("data") or {}) for b in cm):
                    fail("4w: %s: keystone's writer names nova_url while it resolved none: its merge patch would clear "
                         "nova's" % oname)
                if compute is not None and not any(((b or {}).get("data") or {}).get("nova_url") == resolved
                                                   for b in cm):
                    fail("4w: %s: keystone's writer, with nova in the catalog, does not write the nova_url nova writes"
                         % oname)
        else:
            if api.objects[CM_PATH]["data"] != want:
                fail("4w: %s: openstack-kog-endpoints ends as %s, want %s" % (oname, api.objects[CM_PATH]["data"], want))
            if api.objects[SECRET_PATH]["data"] != {"token": base64.b64encode(TOKEN.encode()).decode()}:
                fail("4w: %s: openstack-kog-token ends as %s, want the minted token" % (oname,
                                                                                       api.objects[SECRET_PATH]["data"]))
    api.server.shutdown()


def check_job(d, pod, kind, chart, want_secret, label):
    """4: one Job or CronJob pod that runs the kog_endpoints pod spec. Returns (mint program, writer program, mode)."""
    holders = [c["name"] for c in (pod.get("initContainers") or []) + (pod.get("containers") or []) if secret_refs(c)]
    if holders != ["mint"]:
        fail("4%s: in %s %s the containers that reference a Secret are %s, want only the mint step" % (
            label, kind, d["metadata"]["name"], holders))
    mint_c = [c for c in pod.get("initContainers") or [] if c["name"] == "mint"]
    write_c = [c for c in pod.get("containers") or [] if c["name"] == "write"]
    if mint_c and secret_refs(mint_c[0]) != {want_secret}:
        fail("4%s: %s's mint step reads %s, want the admin openrc (%s) only" % (label, chart, secret_refs(mint_c[0]),
                                                                              want_secret))
    if any("secret" in v or "projected" in v for v in pod.get("volumes") or []):
        fail("4%s: %s %s mounts a Secret volume" % (label, kind, d["metadata"]["name"]))
    script = (mint_c[0].get("command") or [""])[-1] if mint_c else ""
    loops = re.findall(r"(?:^\s*|[;&|]\s*)(for|while|until)\b(.*)$", script, re.M)
    bounded = [l for l in loops if l[0] == "for" and re.match(r"\s+\w+\s+in\s+([0-9 ]+);", l[1])]
    if not loops or len(loops) != len(bounded) or any(len(re.match(r"\s+\w+\s+in\s+([0-9 ]+);", l[1]).group(1).split()) > 4
                                                      for l in bounded):
        fail("4%s: %s's mint step's retries are not capped at 4 (loops: %s): keystone locks a user after 5 failures"
             % (label, chart, loops))
    prog = re.search(r"^cat > /run/kog/mint\.py <<'PY'\n(.*?)^PY$", script, re.M | re.S)
    if not prog:
        fail("4%s: %s's mint step writes no program to /run/kog/mint.py, so nothing could be run" % (label, chart))
    wsrc = (write_c[0].get("command") or [""])[-1] if write_c else ""
    patched = set(re.findall(r'\bpatch\("([a-z]+/[a-z0-9-]+)"', wsrc))
    if patched - {CM_PATH, SECRET_PATH} or CM_PATH not in patched:
        fail("4%s: %s's writer patches %s, want openstack-kog-endpoints (and keystone's openstack-kog-token) only"
             % (label, chart, sorted(patched)))
    modes = {env_value(c, "KOG_MODE") for c in mint_c + write_c}
    if modes != {chart}:
        fail("4%s: %s's mint and write run in mode %s, want %s" % (label, chart, sorted(map(str, modes)), chart))
    counts["4"] = counts.get("4", 0) + 1
    return (prog.group(1) if prog else None), wsrc, chart


def exact_role(by, name, want_rules, label):
    role = by.get(("Role", name))
    rules = (role or {}).get("rules") or []
    key = lambda r: json.dumps(r, sort_keys=True)
    if not role or sorted(map(key, rules)) != sorted(map(key, want_rules)):
        fail("4%s: the Role %s is %s, want exactly %s" % (label, name, rules, want_rules))


def rule(resource, name):
    return {"apiGroups": [""], "resources": [resource], "resourceNames": [name], "verbs": ["get", "patch"]}


def check_namespaces(docs, label):
    for d in docs:
        if (d["metadata"].get("namespace") or NS) != NS:
            fail("3%s: %s/%s is in namespace %s: everything is in the release's (cross-namespace is not supported)"
                 % (label, d["kind"], d["metadata"]["name"], d["metadata"]["namespace"]))
        for s in d.get("subjects") or []:
            if s.get("namespace") not in (None, NS):
                fail("3%s: %s/%s binds a subject in namespace %s" % (label, d["kind"], d["metadata"]["name"],
                                                                    s.get("namespace")))


def check_keystone(docs, root, admin, auth_url, pub, programs, writers):
    label = ""
    by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    check_namespaces(docs, label)
    if by.get(("SecretStore", "openstack-kog-credentials")) or by.get(("ServiceAccount", "openstack-kog-credentials-reader")):
        fail("3: a store or reader of its own is rendered for KOG credentials: there is one namespace and one store")
    es = by.get(("ExternalSecret", "openstack-kog-clouds"))
    if not es:
        fail("3: no ExternalSecret openstack-kog-clouds")
    else:
        data = es["spec"].get("data") or []
        if [(x.get("remoteRef") or {}).get("key") for x in data] != [root["secret"]] or \
                [(x.get("remoteRef") or {}).get("property") for x in data] != [root["key"]]:
            fail("3: openstack-kog-clouds reads %s, not exactly the admin root %s/%s" % (data, root["secret"], root["key"]))
        store = es["spec"]["secretStoreRef"]["name"]
        if store != root["store"]:
            fail("3: openstack-kog-clouds reads through %s, not the install's store %s" % (store, root["store"]))
        tdata = ((es["spec"].get("target") or {}).get("template") or {}).get("data") or {}
        if sorted(tdata) != ["clouds.yaml"]:
            fail("3: openstack-kog-clouds holds %s, want exactly clouds.yaml" % sorted(tdata))
        else:
            t = tdata["clouds.yaml"]
            # every scalar of the file, with the root's placeholder filled: none may be a value the chart publishes
            try:
                parsed = yaml.safe_load(t.replace("{{ .password | toJson }}", json.dumps(KNOWN)))
            except yaml.YAMLError:
                parsed = None
            scalars = []

            def walk(v):
                if isinstance(v, dict):
                    for x in v.values():
                        walk(x)
                elif isinstance(v, list):
                    for x in v:
                        walk(x)
                else:
                    scalars.append(str(v))
            walk(parsed)
            if set(scalars) & pub:
                fail("3: clouds.yaml holds a value the chart publishes (%d of them): the password must come from the root"
                     % len(set(scalars) & pub))
            if t.count("{{ .password | toJson }}") != 1 or re.search(r"\{\{(?! \.password \| toJson \}\})", t):
                fail("3: clouds.yaml does not take its one password from the root's placeholder")
            else:
                clouds = (parsed or {}).get("clouds") or {}
                if len(clouds) != 1:
                    fail("3: clouds.yaml names %d clouds, want 1" % len(clouds))
                for name, c in clouds.items():
                    a = c.get("auth") or {}
                    got = (a.get("password"), a.get("username"), a.get("auth_url"), c.get("interface"),
                           a.get("project_name"))
                    want = (KNOWN, admin["username"], auth_url, "internal", admin["project_name"])
                    if got != want:
                        fail("3: clouds.yaml cloud %s logs in as %s, want %s" % (name, got[1:], want[1:]))
            counts["3"] = counts.get("3", 0) + 1
    for kind in ("Job", "CronJob"):
        d = by.get((kind, "keystone-kog-token"))
        if not d:
            fail("4: no %s keystone-kog-token" % kind)
            continue
        spec = d["spec"] if kind == "Job" else d["spec"]["jobTemplate"]["spec"]
        if spec.get("backoffLimit") != 0:
            fail("4: %s keystone-kog-token has backoffLimit %s, want 0: each pod retry would add up to 4 more failed "
                 "logins" % (kind, spec.get("backoffLimit")))
        if kind == "Job" and (d["metadata"].get("annotations") or {}).get("helm.sh/hook") != "post-install,post-upgrade":
            fail("4: the Job keystone-kog-token is not a post-install,post-upgrade hook")
        if kind == "CronJob" and d["spec"].get("concurrencyPolicy") != "Forbid":
            fail("4: the CronJob keystone-kog-token does not forbid concurrent runs")
        prog, wsrc, mode = check_job(d, spec["template"]["spec"], kind, "keystone", "keystone-keystone-admin", label)
        if prog:
            programs.add(prog)
        writers.setdefault(mode, wsrc)
    exact_role(by, "keystone-kog-token-writer", [rule("secrets", "openstack-kog-token"),
                                                 rule("configmaps", "openstack-kog-endpoints")], label)
    tok = by.get(("Secret", "openstack-kog-token"))
    if not tok or sorted((tok.get("data") or {})) != ["token"]:
        fail("4: no Secret openstack-kog-token holding only token")
    cm = by.get(("ConfigMap", "openstack-kog-endpoints"))
    if not cm or sorted((cm.get("data") or {})) != ENDPOINT_KEYS:
        fail("4: no ConfigMap openstack-kog-endpoints holding exactly %s" % ENDPOINT_KEYS)
    elif any(v for v in cm["data"].values()) or set(cm["data"].values()) & pub:
        fail("4: openstack-kog-endpoints renders values %s with nothing live: they come from keystone, written by the "
             "mint program, never from the chart" % cm["data"])


def check_nova(docs, nova_admin, programs, writers):
    label = ""
    by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    check_namespaces([d for d in docs if "kog" in d["metadata"]["name"]], label)
    owned = [d["kind"] + "/" + d["metadata"]["name"] for d in docs if d["metadata"]["name"] in KOG_OBJECTS]
    if owned:
        fail("4: nova renders %s: keystone owns the KOG objects, nova's hook only patches nova_url" % owned)
    d = by.get(("Job", "nova-kog-endpoints"))
    if not d:
        fail("4: no Job nova-kog-endpoints")
        return
    if d["spec"].get("backoffLimit") != 0:
        fail("4: the Job nova-kog-endpoints has backoffLimit %s, want 0" % d["spec"].get("backoffLimit"))
    if (d["metadata"].get("annotations") or {}).get("helm.sh/hook") != "post-install,post-upgrade":
        fail("4: the Job nova-kog-endpoints is not a post-install,post-upgrade hook")
    pod = d["spec"]["template"]["spec"]
    init = [c for c in pod.get("initContainers") or [] if c["name"] == "init"]
    services = set((env_value(init[0], "DEPENDENCY_SERVICE") or "").split(",")) if init else set()
    jobs = set((env_value(init[0], "DEPENDENCY_JOBS") or "").split(",")) if init else set()
    if not {NS + ":keystone-api", NS + ":nova-api"} <= services or "nova-ks-endpoints" not in jobs:
        fail("4: nova-kog-endpoints waits for services %s and jobs %s; want keystone-api, nova-api and nova-ks-endpoints "
             "(it reads nova's endpoint from keystone's catalog)" % (sorted(services), sorted(jobs)))
    # helm runs hooks one weight at a time: the Job it waits for must sort before it, or the release waits forever
    weight = lambda j: int(((j or {}).get("metadata", {}).get("annotations") or {}).get("helm.sh/hook-weight", "0"))
    ks_ep = by.get(("Job", "nova-ks-endpoints"))
    if not ks_ep or weight(ks_ep) >= weight(d):
        fail("4: nova-kog-endpoints (hook weight %d) does not sort after nova-ks-endpoints (%s), which it waits for"
             % (weight(d), weight(ks_ep) if ks_ep else "not rendered"))
    prog, wsrc, mode = check_job(d, pod, "Job", "nova", nova_admin, label)
    if prog:
        programs.add(prog)
    writers.setdefault(mode, wsrc)
    exact_role(by, "nova-kog-endpoints-writer", [rule("configmaps", "openstack-kog-endpoints")], label)


def no_other_reader(docs, chart):
    for d, pod in pod_specs(docs):
        extra = object_refs(pod) & KOG_OBJECTS
        if extra:
            fail("5: %s's %s/%s reads %s: its readers are the KOGs, in their own releases" % (
                chart, d["kind"], d["metadata"]["name"], sorted(extra)))
        counts["5"] = counts.get("5", 0) + 1


def off_is_nothing(w, release, refs, chart):
    r0 = helm_template(w, release, refs)
    roff = helm_template(w, release, json.loads(json.dumps(refs)) | {"kog_credentials": {"enabled": False}})
    if r0[0] != 0 or roff[0] != 0:
        fail("2: %s does not render with kog_credentials off: %s" % (chart, (r0[2] or roff[2]).strip()[:200]))
    elif r0[1] != roff[1] or "kog" in r0[1].lower():
        fail("2: %s with kog_credentials.enabled false renders differently from the default, or renders a KOG object: "
             "off must be nothing" % chart)
    else:
        counts["2"] = counts.get("2", 0) + 1


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    names = [c["name"] for c in uv.get("components") or []]
    items = {i["id"]: i for i in (uv.get("credentials") or {}).get("items") or []}
    store = (uv.get("credentials") or {}).get("store")
    if not names or "identity-admin" not in items:
        print("FAILED MEASUREMENT: the umbrella lists no component or no identity-admin credential")
        sys.exit(2)
    root = {"store": store, "secret": items["identity-admin"]["secret"], "key": items["identity-admin"]["key"]}
    refs = {"endpoints": {"identity": {"auth": {"admin": {"passwordFrom": dict(root)}}}}}
    with tempfile.TemporaryDirectory() as tmp:
        # 1. the umbrella
        u = stage(UMBRELLA, tmp, "umbrella")
        comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
        start = comp.find("{{- $ov := ")
        mark = comp.find("$.Values.kogCredentials", start)
        end = comp.find("{{- end }}", mark) if mark >= 0 else -1
        specs = {}
        if start < 0 or end < 0:
            fail("1: compositions.yaml builds no spec from $.Values.kogCredentials, so no KOG credential is rendered")
        else:
            open(os.path.join(u, "templates", "zz-gate-kog-probe.yaml"), "w").write(
                '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
                '{{- $dr := include "osh.deregistration" (list $ (splitList " " (include "osh.selected" (list $))) (list)) | fromYaml }}\n{{- range $c := .Values.components }}\n' + comp[start:end + len("{{- end }}")] + '\n'
                '# KGSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n')
            for key, vals in (("absent", {"kogCredentials": None}), ("off", {"kogCredentials": {"enabled": False}}),
                              ("on", {"kogCredentials": {"enabled": True}})):
                rc, out, err = helm_template(u, "openstack-gate", vals)
                if rc != 0:
                    fail("1: the umbrella probe does not render with kogCredentials %s: %s" % (key, err.strip()[:200]))
                    continue
                specs[key] = {l.split(" ", 3)[2]: json.loads(l.split(" ", 3)[3]) for l in out.splitlines()
                              if l.startswith("# KGSPEC ")}
            rc, _, err = helm_template(u, "openstack-gate", {"kogCredentials": {"enabled": True, "namespace": "kogs"}})
            if rc == 0:
                fail("1: kogCredentials.namespace renders; the schema must refuse it (cross-namespace is not supported)")
        if len(specs) == 3:
            for n in names:
                base = specs["absent"].get(n) or {}
                if (specs["off"].get(n) or {}) != base:
                    fail("1: kogCredentials.enabled false changes %s's spec" % n)
                want = json.loads(json.dumps(base))
                if n in ("keystone", "nova"):
                    want["kog_credentials"] = {"enabled": True}
                if (specs["on"].get(n) or {}) != want:
                    fail("1: kogCredentials on gives %s the spec %s, want %s" % (n, json.dumps(specs["on"].get(n))[:160],
                                                                                json.dumps(want)[:160]))
            counts["1"] = len(names)
        programs, writers = set(), {}
        # 2-5. keystone
        kv = yaml.safe_load(open(os.path.join(KEYSTONE, "values.yaml")))
        admin = kv["endpoints"]["identity"]["auth"]["admin"]
        pub = published(kv)
        w = stage(KEYSTONE, tmp, "keystone")
        probe = os.path.join(w, "templates", "zz-gate-authurl.yaml")
        open(probe, "w").write('# AUTHURL {{ tuple "identity" "internal" "api" . | include '
                               '"helm-toolkit.endpoints.keystone_endpoint_uri_lookup" }}\n')
        rc, out, err = helm_template(w, "keystone-gate1234", refs)
        auth_url = (re.findall(r"^# AUTHURL (\S+)$", out, re.M) or [None])[0]
        os.unlink(probe)
        off_is_nothing(w, "keystone-gate1234", refs, "keystone")
        vals = json.loads(json.dumps(refs))
        vals["kog_credentials"] = {"enabled": True}
        rc, out, err = helm_template(w, "keystone-gate1234", vals)
        if rc != 0:
            fail("3: keystone does not render with KOG credentials on: %s" % err.strip()[:200])
        else:
            docs = [d for d in yaml.safe_load_all(out) if d]
            check_keystone(docs, root, admin, auth_url, pub, programs, writers)
            no_other_reader(docs, "keystone")
        vals["kog_credentials"]["namespace"] = "kogs"
        rc, _, _ = helm_template(w, "keystone-gate1234", vals)
        if rc == 0:
            fail("3: keystone renders kog_credentials.namespace; its schema must refuse it (cross-namespace is not "
                 "supported)")
        rc, _, err = helm_template(w, "keystone-gate1234", {"kog_credentials": {"enabled": True}})
        if rc == 0 or "passwordFrom" not in err:
            fail("3: KOG credentials render without the admin root's reference; they must be refused (never a password "
                 "from values)")
        # 2, 4, 5. nova
        nv = yaml.safe_load(open(os.path.join(NOVA, "values.yaml")))
        nova_admin = nv["secrets"]["identity"]["admin"]
        n = stage(NOVA, tmp, "nova")
        off_is_nothing(n, "nova", refs, "nova")
        vals = json.loads(json.dumps(refs))
        vals["kog_credentials"] = {"enabled": True}
        rc, out, err = helm_template(n, "nova", vals)
        if rc != 0:
            fail("4: nova does not render with kog_credentials on: %s" % err.strip()[:200])
        else:
            docs = [d for d in yaml.safe_load_all(out) if d]
            check_nova(docs, nova_admin, programs, writers)
            no_other_reader(docs, "nova")
        # 4m, 4w. the programs, run
        if len(programs) > 1:
            fail("4: keystone's Job and CronJob and nova's Job do not all run the same mint program (%d different)"
                 % len(programs))
        for p in sorted(programs)[:1]:
            run_mint(p, tmp)
            run_writers(p, writers, tmp)
    for part in ("1", "2", "3", "4", "4m", "4w", "5"):
        if not counts.get(part):
            fail("%s: the check had no input" % part)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: kogCredentials: %d component spec(s) compared; off renders nothing in %d chart(s); clouds.yaml from the "
          "admin root checked %d time(s); %d KOG job pod(s) checked (password only in the mint step, at most 4 tries, "
          "exact writer Roles); the mint program run in %d case(s); the two writers run against each other in %d "
          "step(s); %d pod(s) read none of the KOG objects"
          % (counts["1"], counts["2"], counts["3"], counts["4"], counts["4m"], counts["4w"], counts["5"]))


main()
