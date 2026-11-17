#!/usr/bin/env python3
"""Krateo KOG operators get credentials from the install's own generated admin root, and only they get them (issue #303).

    hack/validate-kog-credentials.py [repo-root]

kogCredentials.enabled (default false) has keystone render, in the release's namespace, from the identity-admin root it
already receives: openstack-kog-clouds, a clouds.yaml for the auth proxies of keystone-, neutron- and glance-kog (an
ExternalSecret); openstack-kog-token, an admin token for nova-kog; and openstack-kog-endpoints, a ConfigMap with nova's
and keystone's internal URLs and the region (nova-kog's nova.url). The token and the endpoints are written by one
program, run by a post-install,post-upgrade hook Job and a CronJob. Nothing is copied by hand (#228).

  1. THE UMBRELLA. The lines of compositions.yaml that build a component's spec are rendered for every component with
     kogCredentials absent, disabled and enabled: disabled changes no spec; enabled gives keystone alone kog_credentials
     {enabled: true}. kogCredentials.namespace is refused by the schema: cross-namespace is not supported.
  2. OFF IS NOTHING. keystone rendered with its references and kog_credentials.enabled false is byte-identical to the
     render without the key, and renders no KOG object.
  3. THE CLOUDS FILE. Enabled, every KOG object is in the release's namespace; the ExternalSecret openstack-kog-clouds
     reads exactly the admin root through the install's store, holds exactly clouds.yaml, and its password is the root's
     placeholder: filled with a known value it parses, logs in as the admin user at keystone's internal endpoint,
     interface internal, and no value the chart publishes appears in it.
  4. THE TOKEN AND THE ENDPOINTS. The hook Job (post-install,post-upgrade) and the CronJob (concurrencyPolicy Forbid)
     both run backoffLimit 0, and their pod holds the admin password in exactly one container, the mint step (the admin
     openrc): no other container references a Secret, and the pod mounts no Secret. The mint step tries at most 4 times
     (a bounded `for` and no other loop: keystone locks a user after 5 failures). The writer patches
     openstack-kog-token and openstack-kog-endpoints, and its Role grants get and patch on exactly those two and nothing
     else. The Secret holds only token; the ConfigMap holds exactly nova_url, keystone_url and region_name, and in a
     render (nothing live) every one is empty: no value is assembled by the chart.
  4m. THE MINT PROGRAM, RUN. The program the mint step writes is run against a stand-in `openstack` module whose session
     answers like keystoneauth1's (measured on keystoneauth1 5.18.1, and in the ks_user image, openstacksdk 4.10.0 with
     keystoneauth1 5.13.1, against a stand-in keystone: get_endpoint returns the catalog URL as is, raises
     EndpointNotFound for an entry the catalog lacks, and each run logs in once). It must log in through the openrc (cloud envvars); write
     the token, and endpoints with nova_url = nova's catalog URL plus the token's project id (not doubled when the
     catalog already carries it), keystone's internal URL and the openrc's region; write neither the token nor the
     password into the endpoints; exit 0 with nova_url empty while the catalog has no compute entry (keystone installs
     before nova); and fail on a compute URL that kept an unresolved template.
  5. NO OTHER READER. No pod keystone renders mounts or references openstack-kog-clouds, openstack-kog-token or
     openstack-kog-endpoints: their readers are the KOGs, in their own releases.

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
KEYSTONE = os.path.join(BP, "keystone", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
KNOWN = "GateKogAdmin7f3c"
NS = "openstack"
ENDPOINT_KEYS = ["keystone_url", "nova_url", "region_name"]
KOG_OBJECTS = {"openstack-kog-clouds", "openstack-kog-token", "openstack-kog-endpoints"}
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


def run_mint(program, tmp, label):
    """4m: run the mint program against stand-in sessions."""
    region = "GateRegion"
    pid = "0f3c9a7d2b1e4c5a8d6f"
    nova = "http://nova-api.openstack.svc.cluster.local:8774/v2.1/"
    ks = "http://keystone-api.openstack.svc.cluster.local:5000/v3"
    cases = [
        ("compute registered", {"compute/internal/" + region: nova}, 0, nova.rstrip("/") + "/" + pid),
        ("compute already per project", {"compute/internal/" + region: nova + pid}, 0, nova + pid),
        ("compute not yet registered", {}, 0, ""),
        ("compute template unresolved", {"compute/internal/" + region: nova + "%(tenant_id)s"}, "fail", None),
    ]
    ran = 0
    for name, extra, want_rc, want_nova in cases:
        d = tempfile.mkdtemp(dir=tmp)
        catalog = {"identity/internal/" + region: ks}
        catalog.update(extra)
        case = {"token": "gAAAA-gate-token-81c2", "project": pid, "catalog": catalog}
        open(os.path.join(d, "case.json"), "w").write(json.dumps(case))
        src = program.replace("/run/kog/", d + "/")
        open(os.path.join(d, "mint.py"), "w").write(STUB + "\n" + src)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GATE_CASE": os.path.join(d, "case.json"),
               "OS_REGION_NAME": region, "OS_PASSWORD": KNOWN, "OS_USERNAME": "admin"}
        r = subprocess.run([sys.executable, "-I", os.path.join(d, "mint.py")], capture_output=True, text=True, env=env,
                           cwd=d)
        ran += 1
        if want_rc == "fail":
            if r.returncode == 0:
                fail("4m%s: %s: the mint program exits 0 on a compute URL that kept a template keystone did not "
                     "resolve" % (label, name))
            continue
        if r.returncode != 0:
            last = ((r.stderr or r.stdout).strip().splitlines() or [""])[-1]
            fail("4m%s: %s: the mint program exits %d: %s" % (label, name, r.returncode, last[:200]))
            continue
        tok = open(os.path.join(d, "token")).read() if os.path.exists(os.path.join(d, "token")) else None
        raw = open(os.path.join(d, "endpoints.json")).read() if os.path.exists(os.path.join(d, "endpoints.json")) \
            else "{}"
        ep = json.loads(raw)
        if tok != case["token"]:
            fail("4m%s: %s: the mint program wrote the token %r, want the session's" % (label, name, tok))
        want = {"nova_url": want_nova, "keystone_url": ks, "region_name": region}
        if ep != want:
            fail("4m%s: %s: the mint program wrote the endpoints %s, want %s" % (label, name, ep, want))
        if case["token"] in raw or KNOWN in raw:
            fail("4m%s: %s: the endpoints the mint program writes hold the token or the password: they go into a "
                 "ConfigMap" % (label, name))
    counts["4m"] = counts.get("4m", 0) + ran


def check_enabled(docs, root, admin, auth_url, pub, tmp, label):
    by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    for d in docs:
        if (d["metadata"].get("namespace") or NS) != NS:
            fail("3%s: %s/%s is in namespace %s: everything is in the release's (cross-namespace is not supported)"
                 % (label, d["kind"], d["metadata"]["name"], d["metadata"]["namespace"]))
        for s in d.get("subjects") or []:
            if s.get("namespace") not in (None, NS):
                fail("3%s: %s/%s binds a subject in namespace %s" % (label, d["kind"], d["metadata"]["name"],
                                                                    s.get("namespace")))
    if by.get(("SecretStore", "openstack-kog-credentials")) or by.get(("ServiceAccount", "openstack-kog-credentials-reader")):
        fail("3%s: a store or reader of its own is rendered for KOG credentials: there is one namespace and one store" % label)
    es = by.get(("ExternalSecret", "openstack-kog-clouds"))
    if not es:
        fail("3%s: no ExternalSecret openstack-kog-clouds" % label)
        return
    data = es["spec"].get("data") or []
    if [(x.get("remoteRef") or {}).get("key") for x in data] != [root["secret"]] or \
            [(x.get("remoteRef") or {}).get("property") for x in data] != [root["key"]]:
        fail("3%s: openstack-kog-clouds reads %s, not exactly the admin root %s/%s" % (label, data, root["secret"], root["key"]))
    store = es["spec"]["secretStoreRef"]["name"]
    if store != root["store"]:
        fail("3%s: openstack-kog-clouds reads through %s, not the install's store %s" % (label, store, root["store"]))
    tdata = ((es["spec"].get("target") or {}).get("template") or {}).get("data") or {}
    if sorted(tdata) != ["clouds.yaml"]:
        fail("3%s: openstack-kog-clouds holds %s, want exactly clouds.yaml" % (label, sorted(tdata)))
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
            fail("3%s: clouds.yaml holds a value the chart publishes (%d of them): the password must come from the root"
                 % (label, len(set(scalars) & pub)))
        if t.count("{{ .password | toJson }}") != 1 or re.search(r"\{\{(?! \.password \| toJson \}\})", t):
            fail("3%s: clouds.yaml does not take its one password from the root's placeholder" % label)
        else:
            clouds = (parsed or {}).get("clouds") or {}
            if len(clouds) != 1:
                fail("3%s: clouds.yaml names %d clouds, want 1" % (label, len(clouds)))
            for name, c in clouds.items():
                a = c.get("auth") or {}
                got = (a.get("password"), a.get("username"), a.get("auth_url"), c.get("interface"), a.get("project_name"))
                want = (KNOWN, admin["username"], auth_url, "internal", admin["project_name"])
                if got != want:
                    fail("3%s: clouds.yaml cloud %s logs in as %s, want %s" % (label, name, got[1:], want[1:]))
        counts["3"] = counts.get("3", 0) + 1
    # 4. the token and the endpoints
    programs = set()
    for kind in ("Job", "CronJob"):
        d = by.get((kind, "keystone-kog-token"))
        if not d:
            fail("4%s: no %s keystone-kog-token" % (label, kind))
            continue
        spec = d["spec"] if kind == "Job" else d["spec"]["jobTemplate"]["spec"]
        if spec.get("backoffLimit") != 0:
            fail("4%s: %s keystone-kog-token has backoffLimit %s, want 0: each pod retry would add up to 4 more failed "
                 "logins" % (label, kind, spec.get("backoffLimit")))
        if kind == "Job" and (d["metadata"].get("annotations") or {}).get("helm.sh/hook") != "post-install,post-upgrade":
            fail("4%s: the Job keystone-kog-token is not a post-install,post-upgrade hook" % label)
        if kind == "CronJob" and d["spec"].get("concurrencyPolicy") != "Forbid":
            fail("4%s: the CronJob keystone-kog-token does not forbid concurrent runs" % label)
        pod = spec["template"]["spec"]
        holders = [c["name"] for c in (pod.get("initContainers") or []) + (pod.get("containers") or []) if secret_refs(c)]
        if holders != ["mint"]:
            fail("4%s: in %s keystone-kog-token the containers that reference a Secret are %s, want only the mint step"
                 % (label, kind, holders))
        mint = [c for c in pod.get("initContainers") or [] if c["name"] == "mint"]
        if mint and secret_refs(mint[0]) != {"keystone-keystone-admin"}:
            fail("4%s: the mint step reads %s, want the admin openrc (keystone-keystone-admin) only" % (label,
                                                                                                    secret_refs(mint[0])))
        if any("secret" in v or "projected" in v for v in pod.get("volumes") or []):
            fail("4%s: %s keystone-kog-token mounts a Secret volume" % (label, kind))
        script = (mint[0].get("command") or [""])[-1] if mint else ""
        loops = re.findall(r"(?:^\s*|[;&|]\s*)(for|while|until)\b(.*)$", script, re.M)
        bounded = [l for l in loops if l[0] == "for" and re.match(r"\s+\w+\s+in\s+([0-9 ]+);", l[1])]
        if not loops or len(loops) != len(bounded) or any(len(re.match(r"\s+\w+\s+in\s+([0-9 ]+);", l[1]).group(1).split()) > 4
                                                          for l in bounded):
            fail("4%s: the mint step's retries are not capped at 4 (loops: %s): keystone locks a user after 5 failures"
                 % (label, loops))
        prog = re.search(r"^cat > /run/kog/mint\.py <<'PY'\n(.*?)^PY$", script, re.M | re.S)
        if not prog:
            fail("4%s: the mint step in %s writes no program to /run/kog/mint.py, so nothing could be run" % (label, kind))
        else:
            programs.add(prog.group(1))
        writer = [c for c in pod.get("containers") or [] if c["name"] == "write"]
        wsrc = (writer[0].get("command") or [""])[-1] if writer else ""
        patched = sorted(re.findall(r'\bpatch\("([a-z]+/[a-z0-9-]+)"', wsrc))
        if patched != ["configmaps/openstack-kog-endpoints", "secrets/openstack-kog-token"]:
            fail("4%s: the writer in %s patches %s, want openstack-kog-token and openstack-kog-endpoints" % (label, kind,
                                                                                                         patched))
        counts["4"] = counts.get("4", 0) + 1
    if len(programs) > 1:
        fail("4%s: the hook Job and the CronJob run different mint programs" % label)
    for p in programs:
        run_mint(p, tmp, label)
    role = by.get(("Role", "keystone-kog-token-writer"))
    rules = (role or {}).get("rules") or []
    want_rules = [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["openstack-kog-token"],
                   "verbs": ["get", "patch"]},
                  {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["openstack-kog-endpoints"],
                   "verbs": ["get", "patch"]}]
    key = lambda r: json.dumps(r, sort_keys=True)
    if not role or sorted(map(key, rules)) != sorted(map(key, want_rules)):
        fail("4%s: the writer's Role is %s, want get and patch on openstack-kog-token and openstack-kog-endpoints, "
             "nothing else" % (label, rules))
    tok = by.get(("Secret", "openstack-kog-token"))
    if not tok or sorted((tok.get("data") or {})) != ["token"]:
        fail("4%s: no Secret openstack-kog-token holding only token" % label)
    cm = by.get(("ConfigMap", "openstack-kog-endpoints"))
    if not cm or sorted((cm.get("data") or {})) != ENDPOINT_KEYS:
        fail("4%s: no ConfigMap openstack-kog-endpoints holding exactly %s" % (label, ENDPOINT_KEYS))
    elif any(v for v in cm["data"].values()) or set(cm["data"].values()) & pub:
        fail("4%s: openstack-kog-endpoints renders values %s with nothing live: they come from keystone, written by the "
             "mint program, never from the chart" % (label, cm["data"]))
    # 5. no other reader
    for d, pod in pod_specs(docs):
        extra = object_refs(pod) & KOG_OBJECTS
        if extra:
            fail("5%s: %s/%s reads %s: its readers are the KOGs, in their own releases" % (label, d["kind"],
                                                                                        d["metadata"]["name"], sorted(extra)))
        counts["5"] = counts.get("5", 0) + 1


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    names = [c["name"] for c in uv.get("components") or []]
    items = {i["id"]: i for i in (uv.get("credentials") or {}).get("items") or []}
    store = (uv.get("credentials") or {}).get("store")
    if not names or "identity-admin" not in items:
        print("FAILED MEASUREMENT: the umbrella lists no component or no identity-admin credential")
        sys.exit(2)
    root = {"store": store, "secret": items["identity-admin"]["secret"], "key": items["identity-admin"]["key"]}
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
                '{{- range $c := .Values.components }}\n' + comp[start:end + len("{{- end }}")] + '\n'
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
                if n == "keystone":
                    want["kog_credentials"] = {"enabled": True}
                if (specs["on"].get(n) or {}) != want:
                    fail("1: kogCredentials on gives %s the spec %s, want %s" % (n, json.dumps(specs["on"].get(n))[:160],
                                                                                json.dumps(want)[:160]))
            counts["1"] = len(names)
        # 2-5. keystone
        kv = yaml.safe_load(open(os.path.join(KEYSTONE, "values.yaml")))
        admin = kv["endpoints"]["identity"]["auth"]["admin"]
        pub = published(kv)
        refs = {"endpoints": {"identity": {"auth": {"admin": {"passwordFrom": dict(root)}}}}}
        w = stage(KEYSTONE, tmp, "keystone")
        probe = os.path.join(w, "templates", "zz-gate-authurl.yaml")
        open(probe, "w").write('# AUTHURL {{ tuple "identity" "internal" "api" . | include '
                               '"helm-toolkit.endpoints.keystone_endpoint_uri_lookup" }}\n')
        rc, out, err = helm_template(w, "keystone-gate1234", refs)
        auth_url = (re.findall(r"^# AUTHURL (\S+)$", out, re.M) or [None])[0]
        os.unlink(probe)
        r0 = helm_template(w, "keystone-gate1234", refs)
        roff = helm_template(w, "keystone-gate1234", json.loads(json.dumps(refs)) | {"kog_credentials": {"enabled": False}})
        if r0[0] != 0 or roff[0] != 0:
            fail("2: keystone does not render with kog_credentials off: %s" % (r0[2] or roff[2]).strip()[:200])
        elif r0[1] != roff[1] or "kog" in r0[1].lower():
            fail("2: keystone with kog_credentials.enabled false renders differently from the default, or renders a KOG "
                 "object: off must be nothing")
        vals = json.loads(json.dumps(refs))
        vals["kog_credentials"] = {"enabled": True}
        rc, out, err = helm_template(w, "keystone-gate1234", vals)
        if rc != 0:
            fail("3: keystone does not render with KOG credentials on: %s" % err.strip()[:200])
        else:
            check_enabled([d for d in yaml.safe_load_all(out) if d], root, admin, auth_url, pub, tmp, "")
        vals["kog_credentials"]["namespace"] = "kogs"
        rc, _, _ = helm_template(w, "keystone-gate1234", vals)
        if rc == 0:
            fail("3: keystone renders kog_credentials.namespace; its schema must refuse it (cross-namespace is not "
                 "supported)")
        rc, _, err = helm_template(w, "keystone-gate1234", {"kog_credentials": {"enabled": True}})
        if rc == 0 or "passwordFrom" not in err:
            fail("3: KOG credentials render without the admin root's reference; they must be refused (never a password "
                 "from values)")
    for part in ("1", "3", "4", "4m", "5"):
        if not counts.get(part):
            fail("%s: the check had no input" % part)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: kogCredentials: %d component spec(s) compared; clouds.yaml from the admin root checked %d time(s); the "
          "token Job and CronJob checked %d time(s) (password only in the mint step, at most 4 tries, writer patches "
          "two named objects); the mint program run in %d case(s); %d pod(s) read none of the KOG objects"
          % (counts["1"], counts["3"], counts["4"], counts["4m"], counts["5"]))


main()
