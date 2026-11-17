#!/usr/bin/env python3
"""Krateo KOG operators get credentials from the install's own generated admin root, and only they get them (issue #303).

    hack/validate-kog-credentials.py [repo-root]

kogCredentials.enabled (default false) has keystone render, from the identity-admin root it already receives:
openstack-kog-clouds, a clouds.yaml for the auth proxies of keystone-, neutron- and glance-kog (an ExternalSecret), and
openstack-kog-token, an admin token for nova-kog, minted by a post-install,post-upgrade hook Job and re-minted by a
CronJob. Nothing is copied by hand (#228).

  1. THE UMBRELLA. The lines of compositions.yaml that build a component's spec are rendered for every component with
     kogCredentials absent, disabled, enabled, and enabled for another namespace: disabled changes no spec; enabled
     gives keystone alone kog_credentials {enabled: true, namespace: <the release's, or the one named>}. A namespace that
     is not a DNS label is refused by the schema.
  2. OFF IS NOTHING. keystone rendered with its references and kog_credentials.enabled false is byte-identical to the
     render without the key.
  3. THE CLOUDS FILE. Enabled (same namespace, and another), the ExternalSecret openstack-kog-clouds reads the admin root
     through the store (a store of its own, reading that one root, in another namespace), holds exactly clouds.yaml, and
     its password is the root's placeholder: filled with a known value it parses, logs in as the admin user at keystone's
     internal endpoint, interface internal, and no value the chart publishes appears in it.
  4. THE TOKEN. The hook Job (post-install,post-upgrade) and the CronJob (concurrencyPolicy Forbid) both run backoffLimit
     0, and their pod holds the admin password in exactly one container, the mint step (the admin openrc): no other
     container references a Secret, and the pod mounts no Secret. The mint step tries at most 4 times (a bounded `for` and
     no other loop: keystone locks a user after 5 failures). The writer's Role grants get and patch on openstack-kog-token
     and nothing else.
  5. NO OTHER READER. No pod keystone renders mounts or references openstack-kog-clouds or openstack-kog-token: their
     readers are the KOGs, in their own releases.

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
KEYSTONE = os.path.join(BP, "keystone", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
KNOWN = "GateKogAdmin7f3c"
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
        r = subprocess.run(["helm", "template", release, chart, "--namespace", "openstack", "--kube-version", "1.36.0",
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


def check_enabled(docs, ns, root, admin, auth_url, pub, label):
    by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    es = by.get(("ExternalSecret", "openstack-kog-clouds"))
    if not es:
        fail("3%s: no ExternalSecret openstack-kog-clouds" % label)
        return
    if es["metadata"].get("namespace") != ns:
        fail("3%s: openstack-kog-clouds is in namespace %s, want %s" % (label, es["metadata"].get("namespace"), ns))
    data = es["spec"].get("data") or []
    if [(x.get("remoteRef") or {}).get("key") for x in data] != [root["secret"]] or \
            [(x.get("remoteRef") or {}).get("property") for x in data] != [root["key"]]:
        fail("3%s: openstack-kog-clouds reads %s, not exactly the admin root %s/%s" % (label, data, root["secret"], root["key"]))
    store = es["spec"]["secretStoreRef"]["name"]
    if ns == "openstack" and store != root["store"]:
        fail("3%s: openstack-kog-clouds reads through %s, not the install's store %s" % (label, store, root["store"]))
    if ns != "openstack":
        ss = by.get(("SecretStore", store))
        role = by.get(("Role", "openstack-kog-credentials-reader"))
        rules = (role or {}).get("rules") or []
        names = [n for r in rules if "secrets" in (r.get("resources") or []) for n in (r.get("resourceNames") or [])]
        if not ss or ss["metadata"].get("namespace") != ns or (ss["spec"]["provider"]["kubernetes"].get("remoteNamespace")
                                                               != "openstack"):
            fail("3%s: no SecretStore %s in %s reading the release's namespace" % (label, store, ns))
        if names != [root["secret"]] or any(set(r.get("verbs") or []) - {"get"} for r in rules if "secrets" in (r.get("resources") or [])):
            fail("3%s: the KOG namespace's store may read %s, want exactly get on %s" % (label, names, root["secret"]))
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
            cl = yaml.safe_load(t.replace("{{ .password | toJson }}", json.dumps(KNOWN)))
            clouds = (cl or {}).get("clouds") or {}
            if len(clouds) != 1:
                fail("3%s: clouds.yaml names %d clouds, want 1" % (label, len(clouds)))
            for name, c in clouds.items():
                a = c.get("auth") or {}
                got = (a.get("password"), a.get("username"), a.get("auth_url"), c.get("interface"), a.get("project_name"))
                want = (KNOWN, admin["username"], auth_url, "internal", admin["project_name"])
                if got != want:
                    fail("3%s: clouds.yaml cloud %s logs in as %s, want %s" % (label, name, got[1:], want[1:]))
        counts["3"] = counts.get("3", 0) + 1
    # 4. the token
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
        counts["4"] = counts.get("4", 0) + 1
    role = by.get(("Role", "keystone-kog-token-writer"))
    rules = (role or {}).get("rules") or []
    if not role or role["metadata"].get("namespace") != ns or rules != [{"apiGroups": [""], "resources": ["secrets"],
                                                                         "resourceNames": ["openstack-kog-token"],
                                                                         "verbs": ["get", "patch"]}]:
        fail("4%s: the writer's Role is %s, want get and patch on openstack-kog-token in %s, nothing else" % (label, rules, ns))
    tok = by.get(("Secret", "openstack-kog-token"))
    if not tok or tok["metadata"].get("namespace") != ns or sorted((tok.get("data") or {})) != ["token"]:
        fail("4%s: no Secret openstack-kog-token in %s holding only token" % (label, ns))
    # 5. no other reader
    for d, pod in pod_specs(docs):
        refs = set()
        for c in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            refs |= secret_refs(c)
        for v in pod.get("volumes") or []:
            if (v.get("secret") or {}).get("secretName"):
                refs.add(v["secret"]["secretName"])
            for s in (v.get("projected") or {}).get("sources") or []:
                if (s.get("secret") or {}).get("name"):
                    refs.add(s["secret"]["name"])
        extra = refs & {"openstack-kog-clouds", "openstack-kog-token"}
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
                              ("on", {"kogCredentials": {"enabled": True}}),
                              ("there", {"kogCredentials": {"enabled": True, "namespace": "kogs"}})):
                rc, out, err = helm_template(u, "openstack-gate", vals)
                if rc != 0:
                    fail("1: the umbrella probe does not render with kogCredentials %s: %s" % (key, err.strip()[:200]))
                    continue
                specs[key] = {l.split(" ", 3)[2]: json.loads(l.split(" ", 3)[3]) for l in out.splitlines()
                              if l.startswith("# KGSPEC ")}
            rc, _, _ = helm_template(u, "openstack-gate", {"kogCredentials": {"enabled": True, "namespace": "Not_A_Label"}})
            if rc == 0:
                fail("1: kogCredentials.namespace Not_A_Label renders; the schema must refuse a namespace that is no DNS label")
        if len(specs) == 4:
            for n in names:
                base = specs["absent"].get(n) or {}
                if (specs["off"].get(n) or {}) != base:
                    fail("1: kogCredentials.enabled false changes %s's spec" % n)
                for key, ns in (("on", "openstack"), ("there", "kogs")):
                    want = json.loads(json.dumps(base))
                    if n == "keystone":
                        want["kog_credentials"] = {"enabled": True, "namespace": ns}
                    if (specs[key].get(n) or {}) != want:
                        fail("1: kogCredentials %s gives %s the spec %s, want %s" % (key, n,
                                                                                    json.dumps(specs[key].get(n))[:160],
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
        for label, ns in ((" (same namespace)", ""), (" (namespace kogs)", "kogs")):
            vals = json.loads(json.dumps(refs))
            vals["kog_credentials"] = {"enabled": True, "namespace": ns}
            rc, out, err = helm_template(w, "keystone-gate1234", vals)
            if rc != 0:
                fail("3%s: keystone does not render with KOG credentials on: %s" % (label, err.strip()[:200]))
                continue
            check_enabled([d for d in yaml.safe_load_all(out) if d], ns or "openstack", root, admin, auth_url, pub, label)
        rc, _, err = helm_template(w, "keystone-gate1234", {"kog_credentials": {"enabled": True}})
        if rc == 0 or "passwordFrom" not in err:
            fail("3: KOG credentials render without the admin root's reference; they must be refused (never a password "
                 "from values)")
    for part in ("1", "3", "4", "5"):
        if not counts.get(part):
            fail("%s: the check had no input" % part)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: kogCredentials: %d component spec(s) compared; clouds.yaml from the admin root checked %d time(s); the "
          "token Job and CronJob checked %d time(s) (password only in the mint step, at most 4 tries); %d pod(s) read "
          "neither Secret" % (counts["1"], counts["3"], counts["4"], counts["5"]))


main()
