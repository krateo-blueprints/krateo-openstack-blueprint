#!/usr/bin/env python3
"""A component that leaves the selection leaves no Keystone service, endpoint or hook Job behind (#444).

    hack/validate-deregistration.py [repo-root]

Found by the 0.3.71 -> 0.3.74 upgrade test (r074, 2026-11-21). The upgrade removed manila. Its Keystone service sharev2
and its endpoints stayed in the catalog, pointing at the deleted manila-api Service, and its hook Job pods stayed too.
Skyline's nginx crash-looped on the dead endpoint. helm uninstall runs the hooks of the deployed release (manila's
0.3.71), so the fix comes from the new release: the umbrella passes keystone what the not-selected components registered
(krateo.deregister), and keystone's post-install/post-upgrade hook deletes it.

  1. THE TABLE. blueprints/openstack/chart/templates/_keystone-registrations.tpl is current
     (hack/gen-keystone-registrations.py --check renders every chart again):
     - manila registers sharev2;
     - no service type belongs to two components;
     - every component the umbrella lists has an entry.
  2. THE UMBRELLA. The span of compositions.yaml that builds a component's spec is rendered for each profile and for an
     `enabled:` selection. keystone's krateo.deregister then:
     - names exactly the not-selected components;
     - lists every service they register, and with profile full-strict (manila not selected) that includes manila's
       sharev2;
     - never lists a type a selected component registers;
     - cannot be overridden by componentValues.
     No other component's spec carries it.
  2b. THE STATE MACHINE. osh.deregistration's lookups (osh.lookupNamed, osh.compositionPresent, osh.crdList) are stood
     in, and keystone's krateo.deregister is rendered with profile full-strict (manila not selected) in three states:
     - gone: nothing of manila exists. manila and its sharev2 are handed to keystone;
     - removing: manila's umbrella Composition still exists. manila is NOT handed over, and the pending list names it;
     - standalone: only a Service manila's chart renders (manila-api) exists, as when someone runs Manila on its own
       outside the umbrella. manila is NOT handed over: a working service is never deregistered.
  3. THE CHART. keystone, rendered with full-strict's krateo.deregister, renders one Job, keystone-deregister:
     - a post-install/post-upgrade hook;
     - its password only in the `catalog` init container, from the generated root's openrc Secret, never a literal;
     - a `jobs` container with no password;
     - a Role granting exactly list and delete on batch/jobs.
     With an empty krateo.deregister, nothing renders.
  4. THE PROGRAMS RUN. The rendered catalog program runs against a stand-in catalog holding:
     - manila's sharev2 with two endpoints;
     - a hand-made sharev2 service with another name;
     - nova's compute.
     Afterwards manila's service and endpoints are gone, and the other two are kept. A second run deletes nothing.
     The jobs program runs against a stand-in Kubernetes API holding manila's hook Jobs, a manila Job that is not a
     hook, and keystone's own hook Job. Only manila's hook Jobs are deleted, with background propagation. The
     must-fail case: the catalog check reports the stand-in catalog BEFORE the run (#444's state) as not clean.
  5. MUTANTS. Each must be caught by the checks above:
     - the umbrella merge removed;
     - the name match removed from the catalog program;
     - the hook-annotation filter removed from the jobs program;
     - manila's row removed from the table;
     - the Service lookup removed from the state machine (the standalone case is caught);
     - the Composition check removed from the state machine (the removing case is caught).

Fails when helm or PyYAML is missing, and when any part finds nothing to check.
"""
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
UMBRELLA = os.path.join(ROOT, "blueprints", "openstack", "chart")
KEYSTONE = os.path.join(ROOT, "blueprints", "keystone", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
TABLE = os.path.join(UMBRELLA, "templates", "_keystone-registrations.tpl")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
counts = {}


def stage(chart_dir, tmp, name, edit=None):
    w = os.path.join(tmp, name)
    shutil.copytree(chart_dir, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in txt:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    for rel, old, new in edit or []:
        p = os.path.join(w, rel)
        s = open(p).read()
        if s.count(old) != 1:
            return None
        open(p, "w").write(s.replace(old, new))
    return w


def helm_template(chart, release, values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    try:
        r = subprocess.run(["helm", "template", release, chart, "--namespace", "openstack", "--kube-version", "1.36.0",
                            "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def table_of(text):
    m = re.search(r'\{\{- define "osh.keystoneRegistrations" -\}\}\n(.*?)\{\{- end -\}\}', text, re.S)
    return yaml.safe_load(m.group(1)) if m else None


# ---- 1. the table ----------------------------------------------------------------------------------------------------
def check_table(table, names, fails, label=""):
    if not isinstance(table, dict):
        fails.append("1%s: _keystone-registrations.tpl defines no osh.keystoneRegistrations table" % label)
        return
    if {"type": "sharev2", "name": "manilav2"} not in (table.get("manila") or []):
        fails.append("1%s: the table does not have manila registering sharev2 (manilav2): the case #444 was found on"
                     % label)
    owner = {}
    for comp, regs in table.items():
        for r in regs or []:
            if r["type"] in owner and owner[r["type"]] != comp:
                fails.append("1%s: service type %s is registered by both %s and %s: deregistering one would delete the "
                             "other's" % (label, r["type"], owner[r["type"]], comp))
            owner[r["type"]] = comp
    missing = sorted(set(names) - set(table))
    if missing:
        fails.append("1%s: components with no entry in the table: %s" % (label, missing))
    counts["1"] = counts.get("1", 0) + 1


# ---- 2. the umbrella -------------------------------------------------------------------------------------------------
def umbrella_probe(tmp, name, comp_text):
    start = comp_text.find("{{- $ov := ")
    mark = comp_text.find("osh.deregistration", start)
    end = comp_text.find("{{- end }}", mark) if mark >= 0 else -1
    if start < 0 or mark < 0 or end < 0:
        return None
    u = stage(UMBRELLA, tmp, name)
    open(os.path.join(u, "templates", "compositions.yaml"), "w").write(comp_text)
    open(os.path.join(u, "templates", "zz-gate-dereg-probe.yaml"), "w").write(
        '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
        '# DRSEL {{ $sel | toJson }}\n'
        '{{- range $c := .Values.components }}\n' + comp_text[start:end + len("{{- end }}")] + '\n'
        '# DRSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n')
    return u


def probe(u, vals):
    rc, out, err = helm_template(u, "openstack-gate", vals)
    if rc != 0:
        return None, None, err
    sel = next((json.loads(l.split(" ", 2)[2]) for l in out.splitlines() if l.startswith("# DRSEL ")), None)
    specs = {l.split(" ", 3)[2]: json.loads(l.split(" ", 3)[3]) for l in out.splitlines() if l.startswith("# DRSPEC ")}
    return sel, specs, ""


def check_umbrella(u, table, names, fails, label=""):
    if u is None:
        fails.append("2%s: compositions.yaml builds no spec through osh.deregistration: nothing reaches keystone" % label)
        return {}
    got = {}
    for key, vals in (("full-strict", {"profile": "full-strict"}), ("full", {"profile": "full"}),
                      ("identity", {"profile": "identity"}), ("enabled", {"enabled": ["nova"]}),
                      ("override", {"profile": "full-strict", "componentValues": {"keystone": {"krateo": {"deregister": {
                          "services": [], "components": []}}}}})):
        sel, specs, err = probe(u, vals)
        if specs is None:
            fails.append("2%s: the umbrella probe does not render with %s: %s" % (label, key, err.strip()[:200]))
            continue
        d = ((specs.get("keystone") or {}).get("krateo") or {}).get("deregister")
        if not isinstance(d, dict):
            fails.append("2%s: with %s keystone's spec carries no krateo.deregister" % (label, key))
            continue
        notsel = sorted(set(names) - set(sel))
        if sorted(d.get("components") or []) != notsel:
            fails.append("2%s: with %s krateo.deregister.components is %s, want the not-selected %s"
                         % (label, key, d.get("components"), notsel))
        selected_types = {r["type"] for n in sel for r in table.get(n) or []}
        want = sorted((n, r["type"], r["name"]) for n in notsel for r in table.get(n) or [] if r["type"] not in selected_types)
        have = sorted((s["component"], s["type"], s["name"]) for s in d.get("services") or [])
        if have != want:
            fails.append("2%s: with %s krateo.deregister.services is %s, want %s" % (label, key, have[:6], want[:6]))
        if any(s["type"] in selected_types for s in d.get("services") or []):
            fails.append("2%s: with %s a selected component's service type is listed for deletion" % (label, key))
        others = [n for n, sp in specs.items() if n != "keystone" and "deregister" in ((sp.get("krateo") or {}))]
        if others:
            fails.append("2%s: krateo.deregister reaches components other than keystone: %s" % (label, others))
        got[key] = d
        got[key + ":keystone"] = specs.get("keystone") or {}
    fs = got.get("full-strict") or {}
    if not any(s["type"] == "sharev2" and s["component"] == "manila" for s in fs.get("services") or []):
        fails.append("2%s: with profile full-strict (manila not selected) keystone is not told to deregister manila's "
                     "sharev2: #444's catalog would stay" % label)
    if got.get("override") and got["override"] != fs:
        fails.append("2%s: componentValues.keystone.krateo.deregister overrides the umbrella's list" % label)
    counts["2"] = counts.get("2", 0) + len([k for k in got if ":" not in k])
    return got


# ---- 2b. the state machine -------------------------------------------------------------------------------------------
STANDINS = {
    "osh.lookupNamed": '{{- index ((index . 0).Values.gateObj | default dict) (printf "%s/%s" (index . 2) (index . 3)) '
                       '| default dict | toJson -}}',
    "osh.compositionPresent": '{{- if index ((index . 0).Values.gateObj | default dict) (printf "%s/%s" (index . 1).kind '
                              '(index . 1).name) }}true{{- end -}}',
    "osh.crdList": '{{- list | toJson -}}',
}


def fsm_probe(tmp, name, comp_text, mutate=None):
    u = umbrella_probe(tmp, name, comp_text)
    if u is None:
        return None
    hp = os.path.join(u, "templates", "_helpers.tpl")
    h = open(hp).read()
    for n, body in STANDINS.items():
        rx = re.compile(r'(\{\{- define "%s" -\}\}).*?(\{\{- end -\}\})' % re.escape(n), re.S)
        if not rx.search(h):
            return None
        h = rx.sub(lambda m: m.group(1) + "\n" + body + "\n" + m.group(2), h, count=1)
    if mutate:
        h2 = mutate(h)
        if h2 == h:
            return None
        h = h2
    open(hp, "w").write(h)
    pp = os.path.join(u, "templates", "zz-gate-dereg-probe.yaml")
    open(pp, "a").write('# DRSTATE {{ include "osh.deregistration" (list $ (splitList " " (include "osh.selected" '
                        '(list $)))) | fromYaml | toJson }}\n')
    return u


def check_fsm(u, kinds, fails, label=""):
    if u is None:
        fails.append("2b%s: the state machine cannot be stood in (a lookup define is missing, or a mutant changed "
                     "nothing)" % label)
        return
    mk = kinds.get("manila")
    cases = (("gone", {}, True),
             ("removing", {"%s/manila" % mk: {"metadata": {"name": "manila"}}}, False),
             ("standalone", {"Service/manila-api": {"metadata": {"name": "manila-api"}}}, False))
    for key, objs, handed in cases:
        rc, out, err = helm_template(u, "openstack-gate", {"profile": "full-strict", "gateObj": objs})
        if rc != 0:
            fails.append("2b%s: %s does not render: %s" % (label, key, err.strip()[:200]))
            continue
        specs = {l.split(" ", 3)[2]: json.loads(l.split(" ", 3)[3]) for l in out.splitlines() if l.startswith("# DRSPEC ")}
        st = next((json.loads(l.split(" ", 2)[2]) for l in out.splitlines() if l.startswith("# DRSTATE ")), None)
        d = ((specs.get("keystone") or {}).get("krateo") or {}).get("deregister") or {}
        has = "manila" in (d.get("components") or []) and any(
            s.get("type") == "sharev2" for s in d.get("services") or [])
        if has != handed:
            fails.append("2b%s: %s: manila %s handed to keystone, want %s (components %s)"
                         % (label, key, "is" if has else "is not", "handed over" if handed else "kept",
                            d.get("components")))
        pending = (st or {}).get("removing") or {}
        if not handed and "manila" not in pending:
            fails.append("2b%s: %s: the pending list does not name manila (%s)" % (label, key, sorted(pending)))
        if "removing" in d:
            fails.append("2b%s: %s: keystone's spec carries the pending list: every removal step would upgrade "
                         "keystone" % (label, key))
        counts["2b"] = counts.get("2b", 0) + 1


# ---- 3. the chart ----------------------------------------------------------------------------------------------------
def env_of(c):
    return {e["name"]: e for e in c.get("env") or []}


def check_chart(w, spec, fails, label=""):
    """keystone rendered with the spec the umbrella builds for it (credential references included)"""
    rc, out, err = helm_template(w, "keystone-gate1234", spec)
    if rc != 0:
        fails.append("3%s: keystone does not render with krateo.deregister: %s" % (label, err.strip()[:300]))
        return None
    docs = [d for d in yaml.safe_load_all(out) if d]
    jobs = [d for d in docs if d.get("kind") == "Job" and d["metadata"]["name"] == "keystone-deregister"]
    if len(jobs) != 1:
        fails.append("3%s: keystone renders %d keystone-deregister Job(s), want 1" % (label, len(jobs)))
        return None
    j = jobs[0]
    ann = j["metadata"].get("annotations") or {}
    if ann.get("helm.sh/hook") != "post-install,post-upgrade":
        fails.append("3%s: keystone-deregister is not a post-install,post-upgrade hook (%s)" % (label, ann.get("helm.sh/hook")))
    pod = j["spec"]["template"]["spec"]
    cat = next((c for c in pod.get("initContainers") or [] if c["name"] == "catalog"), None)
    jc = next((c for c in pod.get("containers") or [] if c["name"] == "jobs"), None)
    if not cat or not jc:
        fails.append("3%s: keystone-deregister lacks its catalog init container or its jobs container" % label)
        return None
    pw = env_of(cat).get("OS_PASSWORD") or {}
    ref = (pw.get("valueFrom") or {}).get("secretKeyRef")
    if "value" in pw or not ref:
        fails.append("3%s: the catalog step's OS_PASSWORD is not a secretKeyRef (%s): never a literal (#228)" % (label, pw))
    if any("PASSWORD" in n for n in env_of(jc)):
        fails.append("3%s: the jobs container carries a password variable" % label)
    roles = [d for d in docs if d.get("kind") == "Role" and d["metadata"]["name"] == "keystone-deregister-jobs"]
    rules = roles[0].get("rules") if roles else None
    if rules != [{"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["list", "delete"]}]:
        fails.append("3%s: the keystone-deregister Role is %s, want exactly list and delete on batch/jobs" % (label, rules))
    empty = json.loads(json.dumps(spec))
    empty.setdefault("krateo", {})["deregister"] = {"services": [], "components": []}
    rc, out0, _ = helm_template(w, "keystone-gate1234", empty)
    if rc != 0 or "keystone-deregister" in out0:
        fails.append("3%s: with an empty krateo.deregister keystone still renders the deregistration" % label)
    counts["3"] = counts.get("3", 0) + 1
    script = cat["command"][2]
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", script, re.S)
    return {"catalog": m.group(1) if m else None, "jobs": jc["command"][2], "services": json.loads(env_of(cat)["DEREGISTER_SERVICES"]["value"]),
            "components": json.loads(env_of(jc)["DEREGISTER_COMPONENTS"]["value"])}


# ---- 4. the programs -------------------------------------------------------------------------------------------------
STUB = r'''
import json, os, types
STATE = os.environ["STUB_CATALOG"]
class O(types.SimpleNamespace):
    pass
class Identity:
    def _load(self):
        return json.load(open(STATE))
    def _save(self, d):
        json.dump(d, open(STATE, "w"))
    def services(self):
        return [O(**s) for s in self._load()["services"]]
    def endpoints(self):
        return [O(**e) for e in self._load()["endpoints"]]
    def delete_endpoint(self, e, ignore_missing=True):
        d = self._load(); d["endpoints"] = [x for x in d["endpoints"] if x["id"] != e.id]; d["log"].append("endpoint " + e.id); self._save(d)
    def delete_service(self, s, ignore_missing=True):
        d = self._load()
        if any(x["service_id"] == s.id for x in d["endpoints"]):
            raise RuntimeError("deleted a service that still has endpoints: " + s.id)
        d["services"] = [x for x in d["services"] if x["id"] != s.id]; d["log"].append("service " + s.id); self._save(d)
class Conn:
    identity = Identity()
def connect(cloud=None):
    return Conn()
'''
CATALOG = {"services": [{"id": "s-manila", "type": "sharev2", "name": "manilav2"},
                        {"id": "s-handmade", "type": "sharev2", "name": "someone-elses-share"},
                        {"id": "s-nova", "type": "compute", "name": "nova"}],
           "endpoints": [{"id": "e-m-int", "service_id": "s-manila", "interface": "internal",
                          "url": "http://manila-api.openstack.svc.cluster.local:8786/v2"},
                         {"id": "e-m-adm", "service_id": "s-manila", "interface": "admin",
                          "url": "http://manila-api.openstack.svc.cluster.local:8786/v2"},
                         {"id": "e-hand", "service_id": "s-handmade", "interface": "public", "url": "https://shares.example/v2"},
                         {"id": "e-nova", "service_id": "s-nova", "interface": "internal",
                          "url": "http://nova-api.openstack.svc.cluster.local:8774/v2.1"}],
           "log": []}


def catalog_clean(cat, services):
    """the bar: no service of a not-selected component (type AND name) is left, nor an endpoint of one"""
    gone = {(s["type"], s["name"]) for s in services}
    left = [s for s in cat["services"] if (s["type"], s["name"]) in gone]
    ids = {s["id"] for s in left}
    return not left and not any(e["service_id"] in ids for e in cat["endpoints"]), left


def run_catalog(program, tmp, services, fails, label=""):
    d = tempfile.mkdtemp(dir=tmp)
    os.makedirs(os.path.join(d, "openstack"))
    open(os.path.join(d, "openstack", "__init__.py"), "w").write(STUB)
    state = os.path.join(d, "catalog.json")
    json.dump(CATALOG, open(state, "w"))
    ok0, _ = catalog_clean(json.load(open(state)), services)
    if ok0 and not label:
        fails.append("4: the catalog check calls #444's catalog (manila's sharev2 left) clean: it cannot see the defect")
    p = os.path.join(d, "catalog.py")
    open(p, "w").write(program)
    env = dict(os.environ, PYTHONPATH=d, STUB_CATALOG=state, DEREGISTER_SERVICES=json.dumps(services))
    for run in (1, 2):
        r = subprocess.run([sys.executable, p], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            fails.append("4%s: the catalog program failed (run %d): %s" % (label, run, (r.stderr or r.stdout).strip()[-300:]))
            return
        cat = json.load(open(state))
        if run == 1:
            first = list(cat["log"])
        ok, left = catalog_clean(cat, services)
        if not ok:
            fails.append("4%s: after run %d the catalog keeps a not-selected component's service: %s" % (label, run, left))
        kept = {s["id"] for s in cat["services"]}
        if not {"s-handmade", "s-nova"} <= kept or "e-hand" not in {e["id"] for e in cat["endpoints"]}:
            fails.append("4%s: after run %d a service that is not a not-selected component's was deleted (kept: %s)"
                         % (label, run, sorted(kept)))
    if cat["log"] != first:
        fails.append("4%s: a second run deleted more (%s): the program is not idempotent" % (label, cat["log"][len(first):]))
    counts["4"] = counts.get("4", 0) + 1


class K8s(http.server.BaseHTTPRequestHandler):
    jobs, deletes = [], []

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        sel = (q.get("labelSelector") or [""])[0]
        m = re.match(r"application in \((.*)\)$", sel)
        names = set(m.group(1).split(",")) if m else None
        items = [j for j in K8s.jobs if names is None or (j["metadata"].get("labels") or {}).get("application") in names]
        body = json.dumps({"items": items}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        n = int(self.headers.get("Content-Length") or 0)
        K8s.deletes.append((urllib.parse.unquote(self.path.rsplit("/", 1)[1]), json.loads(self.rfile.read(n) or b"{}")))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def run_jobs(program, tmp, components, fails, label=""):
    hook = {"helm.sh/hook": "post-install,post-upgrade"}
    K8s.jobs = [{"metadata": {"name": n, "labels": {"application": a}, "annotations": an}} for n, a, an in (
        ("manila-db-sync", "manila", hook), ("manila-ks-service", "manila", hook),
        ("manila-not-a-hook", "manila", {}), ("keystone-db-sync", "keystone", hook))]
    K8s.deletes = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), K8s)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d = tempfile.mkdtemp(dir=tmp)
    open(os.path.join(d, "token"), "w").write("stand-in-token")
    open(os.path.join(d, "ca.crt"), "w").write("")
    # the harness's own transport: plain HTTP to the stand-in, the token directory here. Nothing else is changed.
    prog = program.replace('"/var/run/secrets/kubernetes.io/serviceaccount"', repr(d)).replace('"https://%s:%s', '"http://%s:%s')
    prog = prog.replace("ctx = ssl.create_default_context(cafile=sa + \"/ca.crt\")", "ctx = None")
    p = os.path.join(d, "jobs.py")
    open(p, "w").write(prog)
    env = dict(os.environ, KUBERNETES_SERVICE_HOST="127.0.0.1", KUBERNETES_SERVICE_PORT=str(srv.server_address[1]),
               DEREGISTER_NAMESPACE="openstack", DEREGISTER_COMPONENTS=json.dumps(components))
    try:
        r = subprocess.run([sys.executable, p], env=env, capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
    if r.returncode != 0:
        fails.append("4%s: the jobs program failed: %s" % (label, (r.stderr or r.stdout).strip()[-300:]))
        return
    names = sorted(n for n, _ in K8s.deletes)
    if names != ["manila-db-sync", "manila-ks-service"]:
        fails.append("4%s: the jobs program deleted %s, want exactly manila's two hook Jobs" % (label, names))
    if any(b.get("propagationPolicy") != "Background" for _, b in K8s.deletes):
        fails.append("4%s: a Job was deleted without background propagation: its pods would stay" % label)
    counts["4"] = counts.get("4", 0) + 1


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    names = sorted(c["name"] for c in uv.get("components") or [])
    if "manila" not in names or "keystone" not in names:
        print("FAILED MEASUREMENT: the umbrella lists no manila or no keystone")
        sys.exit(2)
    fails = []
    r = subprocess.run([sys.executable, os.path.join(ROOT, "hack", "gen-keystone-registrations.py"), ROOT, "--check"],
                       capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if r.returncode != 0:
        fails.append("1: %s" % (r.stdout + r.stderr).strip()[-300:])
    table = table_of(open(TABLE).read()) if os.path.exists(TABLE) else None
    check_table(table, names, fails)
    comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
    with tempfile.TemporaryDirectory() as tmp:
        got = check_umbrella(umbrella_probe(tmp, "umbrella", comp), table or {}, names, fails)
        kinds = {c["name"]: c.get("kind") for c in uv.get("components") or []}
        check_fsm(fsm_probe(tmp, "umbrella-fsm", comp), kinds, fails)
        fs = got.get("full-strict")
        progs = None
        if fs:
            progs = check_chart(stage(KEYSTONE, tmp, "keystone"), got["full-strict:keystone"], fails)
        if progs and progs["catalog"]:
            run_catalog(progs["catalog"], tmp, progs["services"], fails)
            run_jobs(progs["jobs"], tmp, progs["components"], fails)
        elif progs:
            fails.append("3: the catalog program is not found in the catalog step's command")
        # 5. mutants
        caught = []

        def mutant(what, mf):
            if not mf:
                fails.append("5: mutant '%s' passes every check: the checks cannot see it" % what)
            else:
                caught.append("%s -> %s" % (what, mf[0][:110]))
            counts["5"] = counts.get("5", 0) + 1

        merge = '{{- $ov = mergeOverwrite (deepCopy $ov) (dict "krateo" (dict "deregister" (dict "services" ($dr.services | default list) "components" ($dr.components | default list)))) }}'
        if comp.count(merge) == 1:
            mf = []
            check_umbrella(umbrella_probe(tmp, "umbrella-m", comp.replace(merge, "{{- /* osh.deregistration removed */ -}}")),
                           table or {}, names, mf, " (mutant)")
            mutant("the umbrella merge removed", mf)
        else:
            fails.append("5: the umbrella merge is not in compositions.yaml exactly once")
        if progs and progs["catalog"]:
            old = '        if s.name != w["name"]:'
            if progs["catalog"].count(old) == 1:
                mf = []
                run_catalog(progs["catalog"].replace(old, "        if False:"), tmp, progs["services"], mf, " (mutant)")
                mutant("the name match removed", mf)
            else:
                fails.append("5: the catalog program's name match is not there exactly once")
            old = '    if app not in components or "helm.sh/hook" not in (md.get("annotations") or {}):'
            if progs["jobs"].count(old) == 1:
                mf = []
                run_jobs(progs["jobs"].replace(old, "    if app not in components:"), tmp, progs["components"], mf, " (mutant)")
                mutant("the hook-annotation filter removed", mf)
            else:
                fails.append("5: the jobs program's hook filter is not there exactly once")
        svc_look = '{{- if (include "osh.lookupNamed" (list $top "v1" "Service" $s) | fromJson).metadata }}'
        mf = []
        check_fsm(fsm_probe(tmp, "umbrella-fsm-m1", comp, lambda h: h.replace(svc_look, "{{- if false }}")),
                  kinds, mf, " (mutant)")
        mutant("the Service lookup removed from the state machine", mf)
        comp_look = '{{- if eq (include "osh.compositionPresent" (list $top $c $crds)) "true" }}'
        mf = []
        check_fsm(fsm_probe(tmp, "umbrella-fsm-m2", comp, lambda h: h.replace(comp_look, "{{- if false }}")),
                  kinds, mf, " (mutant)")
        mutant("the Composition check removed from the state machine", mf)
        if table:
            mt = {k: v for k, v in table.items() if k != "manila"}
            mf = []
            check_table(mt, names, mf, " (mutant)")
            mutant("manila's row removed from the table", mf)
        for c in caught:
            print("   mutant caught: " + c)
    for k in ("1", "2", "2b", "3", "4", "5"):
        if not counts.get(k):
            fails.append("part %s checked nothing" % k)
    if fails:
        for f in fails:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: the registrations table is current and manila registers sharev2; keystone's krateo.deregister checked in %d "
          "selection(s); the state machine keeps manila while its Composition or a Service exists and hands it over "
          "once gone (%d state(s)); keystone-deregister rendered (hook, root-only password, exact Role); the catalog and jobs "
          "programs run (%d run(s)), #444's catalog fails the bar before and passes after; %d mutant(s) caught"
          % (counts["2"], counts["2b"], counts["4"], counts["5"]))


main()
