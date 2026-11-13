#!/usr/bin/env python3
"""Every install's credentials are its own: no published OpenStack-Helm default reaches a rendered
object, and the generated roots are never renamed or removed (issue #228).

    hack/validate-credentials.py [repo-root]

The umbrella generates each credential in .Values.credentials.items once, with External Secrets
Operator, and passes the components that list it under components[].credentials a REFERENCE (a
SecretStore, a root Secret and a key) at the item's values path. The component charts project the
root into the Secrets and config drop-ins they already consume. That only protects an install if
four things hold, and each is checked here:

  1. The umbrella passes exactly the reference this gate tests the charts with. The lines of
     compositions.yaml that build a component's spec ($ov, from componentValues and
     osh.credentialRefs, through the line calling mergeOverwrite) are EXTRACTED and rendered for every component through a probe -- the
     helper alone is not enough, because dropping the merge left the helper correct and every
     component on the default -- and compared with the reference built from the item (store,
     secret, key at `path`). An explicit componentValues entry must win over the reference.
  2. CLOSURE. A credential consumed by a chart the umbrella does not list stays on the default.
     For identity-admin: the components listing it are exactly the charts that render an openrc
     Secret for the Keystone admin user (templates/secret-keystone.yaml).
  3. NO VALUE FROM VALUES. Each listed chart is rendered with its references AND with the admin
     password in values replaced by a unique sentinel. The sentinel must appear NOWHERE: not in any
     rendered text (configs, scripts) and not in any Secret's data once base64-decoded. A substring
     search for the published default cannot do this job: it is a common word that other
     credentials share and that appears as a non-secret value (OS_AUTH_TYPE: password), so it
     matches everywhere and proves nothing about THIS credential. The admin openrc must also be an
     ExternalSecret projecting the root through the store, with no Secret of that name.
  4. ROOTS ARE PERMANENT. Every root Secret name shipped in the latest release tag is still an item
     here. Renaming or removing one makes ESO generate a new password that no backend accepts
     (measured on ESO 2.15.0, #228).
  5. MACHINE USERS CANNOT BE LOCKED OUT. keystone.conf enables failed-login lockout, and while the
     admin password rotates, clients still holding the previous value fail: five failures locked the
     admin account for 30 minutes at a time (measured upgrading 0.3.51 to 0.3.52). The keystone
     chart's db-sync script must set ignore_lockout_failure_attempts on the bootstrap user through
     Keystone's provider API, BEFORE keystone-manage bootstrap (so the exemption holds before the
     reset can make anyone fail) and AFTER it (a fresh install, where bootstrap creates the user).
  6. TORN DOWN WITH THE RELEASE. composition-dynamic-controller finalizes a release's children that
     carry finalizers BEFORE helm uninstall. An ExternalSecret carries ESO's, so it was deleted first
     and took its Secret with it -- and glance's pre-delete hook then could not start (#254). Every
     ExternalSecret rendered, the umbrella's roots and every component's projections, must carry
     krateo.io/teardown: with-release (core-provider's opt-out contract, exactly that value).

Fails when helm or PyYAML is missing, when the umbrella defines no credential, when a listed chart
does not render, and when no chart was checked.
"""
import base64
import json
import os
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

if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

failures = []


def fail(msg):
    failures.append(msg)


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


def helm_template(chart, release, values=None):
    args = ["helm", "template", release, chart, "--namespace", "openstack", "--kube-version", "1.36.0"]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
        vf = f.name
    try:
        r = subprocess.run(args + ["-f", vf], capture_output=True, text=True)
    finally:
        os.unlink(vf)
    return r.returncode, r.stdout, r.stderr


def nested(path, value):
    out = node = {}
    parts = path.split(".")
    for p in parts[:-1]:
        node[p] = {}
        node = node[p]
    node[parts[-1]] = value
    return out


def deep_merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            deep_merge(a[k], v)
        else:
            a[k] = v
    return a


def git(*args):
    r = subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, text=True)
    return r.returncode, r.stdout


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    creds = uv.get("credentials") or {}
    items = {i["id"]: i for i in (creds.get("items") or [])}
    if not items:
        print("FAILED MEASUREMENT: the umbrella defines no credentials.items, so nothing protects any install")
        sys.exit(2)
    components = uv.get("components") or []

    def refs_for(c):
        out = {}
        for cid in c.get("credentials") or []:
            i = items[cid]
            deep_merge(out, nested(i["path"], {"store": creds["store"], "secret": i["secret"], "key": i["key"]}))
        return out

    with tempfile.TemporaryDirectory() as tmp:
        # 1. what the umbrella passes == what this gate tests with
        u = stage(UMBRELLA, tmp, "umbrella")
        comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
        start = comp.find("{{- $ov := ")
        merge = comp.find("mergeOverwrite", start)
        if start < 0 or merge < 0:
            fail("umbrella: compositions.yaml no longer builds $ov and merges the credential references "
                 "into it (the span from `{{- $ov := ` to the line that calls mergeOverwrite), so no component would "
                 "receive one")
            span = "{{- $ov := dict }}"
        else:
            span = comp[start:comp.index("\n", merge)]
        with open(os.path.join(u, "templates", "zz-gate-probe.yaml"), "w") as f:
            f.write('{{- range $c := .Values.components }}\n' + span + '\n'
                    '# CREDSPEC {{ $c.name }} {{ $ov | toJson }}\n'
                    '{{- end }}\n')
        override = {"endpoints": {"identity": {"auth": {"admin": {"passwordFrom": {"store": "gate-explicit-store"}}}}}}
        rc, out, err = helm_template(u, "openstack-gate", {"componentValues": {"keystone": override}})
        if rc != 0:
            print("FAILED MEASUREMENT: the umbrella does not render: %s" % err.strip()[:400])
            sys.exit(2)
        probed = {}
        for line in out.splitlines():
            if line.startswith("# CREDSPEC "):
                _, _, name, js = line.split(" ", 3)
                probed[name] = json.loads(js)
        for c in components:
            want = refs_for(c)
            if c["name"] == "keystone":
                want = deep_merge(want, json.loads(json.dumps(override)))
            if probed.get(c["name"]) != want:
                fail("umbrella: the spec compositions.yaml builds for %s is %s, want %s"
                     % (c["name"], probed.get(c["name"]), want))
        roots = [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "ExternalSecret"]
        root_names = {d["metadata"]["name"] for d in roots}
        for i in items.values():
            if i["secret"] not in root_names:
                fail("umbrella: no root ExternalSecret %s for credential %s" % (i["secret"], i["id"]))
        for d in roots:
            if (d["metadata"].get("annotations") or {}).get("krateo.io/teardown") != "with-release":
                fail("umbrella: root %s lacks krateo.io/teardown: with-release (#254)" % d["metadata"]["name"])
            if (d["metadata"].get("annotations") or {}).get("helm.sh/resource-policy") != "keep":
                fail("umbrella: root %s lacks helm.sh/resource-policy: keep" % d["metadata"]["name"])
            if (d.get("spec") or {}).get("refreshPolicy") != "CreatedOnce":
                fail("umbrella: root %s is not refreshPolicy CreatedOnce (it would regenerate)" % d["metadata"]["name"])

        # 2. closure for identity-admin
        listed = {c["name"] for c in components if "identity-admin" in (c.get("credentials") or [])}
        rendering = {d for d in os.listdir(BP)
                     if os.path.isfile(os.path.join(BP, d, "chart", "templates", "secret-keystone.yaml"))}
        for name in sorted(rendering - listed):
            fail("closure: %s renders a Keystone admin openrc Secret but the umbrella does not pass it "
                 "identity-admin, so it would keep the published default" % name)
        for name in sorted(listed - rendering):
            fail("closure: the umbrella passes identity-admin to %s, which renders no admin openrc Secret" % name)

        # 3. no default
        checked = 0
        for c in components:
            if not c.get("credentials"):
                continue
            chart_dir = os.path.join(BP, c["name"], "chart")
            cv = yaml.safe_load(open(os.path.join(chart_dir, "values.yaml")))
            admin_secret = ((cv.get("secrets") or {}).get("identity") or {}).get("admin")
            if not admin_secret:
                fail("%s: values.yaml declares no secrets.identity.admin" % c["name"])
                continue
            sentinel = "gate-admin-sentinel-" + c["name"] + "-7f3c9e"
            values = deep_merge(refs_for(c), nested("endpoints.identity.auth.admin.password", sentinel))
            w = stage(chart_dir, tmp, c["name"])
            rc, out, err = helm_template(w, c["name"] + "-gate1234", values)
            if rc != 0:
                fail("%s: does not render with its credential references: %s" % (c["name"], err.strip()[:300]))
                continue
            checked += 1
            docs = [d for d in yaml.safe_load_all(out) if d]
            if sentinel in out:
                fail("%s: the admin password from values appears in the rendered text" % c["name"])
            for d in docs:
                if d.get("kind") == "Secret":
                    for k, v in (d.get("data") or {}).items():
                        try:
                            if sentinel in base64.b64decode(v).decode("utf-8", "replace"):
                                fail("%s: Secret %s key %s carries the admin password from values"
                                     % (c["name"], d["metadata"]["name"], k))
                        except Exception:
                            pass
            for d in docs:
                if d.get("kind") == "ExternalSecret" and \
                        (d["metadata"].get("annotations") or {}).get("krateo.io/teardown") != "with-release":
                    fail("%s: ExternalSecret %s lacks krateo.io/teardown: with-release, so it is finalized "
                         "before the release's pre-delete hooks run and takes its Secret with it (#254)"
                         % (c["name"], d["metadata"]["name"]))
            es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == admin_secret]
            sec = [d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"] == admin_secret]
            item = items["identity-admin"]
            if sec:
                fail("%s: still renders Secret %s itself" % (c["name"], admin_secret))
            if len(es) != 1:
                fail("%s: renders %d ExternalSecret(s) named %s, want 1" % (c["name"], len(es), admin_secret))
            else:
                spec = es[0]["spec"]
                ref = (spec.get("data") or [{}])[0].get("remoteRef") or {}
                if spec.get("secretStoreRef", {}).get("name") != creds["store"] or ref.get("key") != item["secret"] \
                        or ref.get("property") != item["key"]:
                    fail("%s: ExternalSecret %s does not read %s/%s through store %s"
                         % (c["name"], admin_secret, item["secret"], item["key"], creds["store"]))
                if spec.get("target", {}).get("creationPolicy") != "Owner":
                    fail("%s: ExternalSecret %s is not creationPolicy Owner (Orphan loses the Secret on a "
                         "retried upgrade, #228)" % (c["name"], admin_secret))
        # 5. the admin user is exempt from lockout, before and after bootstrap
        w = stage(os.path.join(BP, "keystone", "chart"), tmp, "keystone-lockout")
        rc, out, err = helm_template(w, "keystone-gate1234")
        script = None
        if rc == 0:
            for d in yaml.safe_load_all(out):
                if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "keystone-bin":
                    script = (d.get("data") or {}).get("db-sync.sh")
        if script is None:
            fail("keystone: no db-sync.sh in the keystone-bin ConfigMap (render rc=%s %s)" % (rc, err.strip()[:200]))
        else:
            lines = [l.strip() for l in script.splitlines()]
            calls = [i for i, l in enumerate(lines) if l == "ensure_admin_lockout_exempt"]
            boot = [i for i, l in enumerate(lines) if l == "bootstrap"]
            if '"ignore_lockout_failure_attempts": True' not in script or "identity.update_user(" not in script:
                fail("keystone: db-sync.sh does not set ignore_lockout_failure_attempts through identity_api.update_user")
            if len(boot) != 1:
                fail("keystone: db-sync.sh runs keystone-manage bootstrap %d times, want 1" % len(boot))
            elif not any(c < boot[0] for c in calls) or not any(c > boot[0] for c in calls):
                fail("keystone: db-sync.sh must call ensure_admin_lockout_exempt before AND after bootstrap "
                     "(calls at lines %s, bootstrap at %s)" % (calls, boot[0]))

        if checked == 0:
            print("FAILED MEASUREMENT: no chart was rendered with credential references")
            sys.exit(2)

    # 4. roots shipped in the latest release are still items
    rc, tags = git("tag", "--list", "0.*", "--sort=-v:refname")
    latest = tags.split()[0] if rc == 0 and tags.split() else None
    if latest:
        rc, old = git("show", "%s:blueprints/openstack/chart/values.yaml" % latest)
        if rc == 0:
            shipped = {i["secret"] for i in ((yaml.safe_load(old).get("credentials") or {}).get("items") or [])}
            current = {i["secret"] for i in items.values()}
            for gone in sorted(shipped - current):
                fail("roots: %s shipped in %s and is no longer an item; every install running %s holds its "
                     "live password there" % (gone, latest, latest))

    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: %d credential item(s); %d chart(s) rendered with references carry no admin password from values; "
          "umbrella references, closure, roots, the admin lockout exemption and teardown with the release consistent" % (len(items), checked))


main()
