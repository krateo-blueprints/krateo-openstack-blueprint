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
     secret, key at `path`, or at `pathFor.<component>` when the item names that component). An
     explicit componentValues entry must win over the reference.
  2. CLOSURE. A credential consumed by a chart the umbrella does not list stays on the default.
     Per item (RULES below): identity-admin goes to exactly the charts rendering a Keystone admin
     openrc Secret (templates/secret-keystone.yaml); oslo-db-admin to exactly the charts rendering a
     MariaDB root DB_CONNECTION Secret (templates/secret-db.yaml) and to mariadb itself;
     oslo-messaging-admin to exactly the charts rendering a RabbitMQ admin connection Secret
     (templates/secret-rabbitmq.yaml) and to rabbitmq itself, whose administrator it is (at
     auth.user); oslo-messaging-guest and oslo-messaging-erlang-cookie to rabbitmq alone.
  3. NO VALUE FROM VALUES. Each listed chart is rendered with its references AND with the admin
     password in values replaced by a unique sentinel. The sentinel must appear NOWHERE: not in any
     rendered text (configs, scripts) and not in any Secret's data once base64-decoded. A substring
     search for the published default cannot do this job: it is a common word that other
     credentials share and that appears as a non-secret value (OS_AUTH_TYPE: password), so it
     matches everywhere and proves nothing about THIS credential. Each projection (the admin openrc,
     the DB_CONNECTION, the RabbitMQ connection, rabbitmq's admin-user Secret) must also be an
     ExternalSecret reading the item's root through the store, with no Secret of that name; and
     EVERY remoteRef of every ExternalSecret a consumer renders must read a known root.
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

  7. NO DANGLING REFERENCE. Rendered with its references, no pod of a consumer references a Secret
     that the chart renders at defaults but no longer provides (as a Secret or an ExternalSecret
     target) -- e.g. mariadb's server still pointing at mariadb-dbadmin-password after the projection
     moved to mariadb-dbadmin-generated. The default-values pod-references gate cannot see this.
  8. PROJECTION FIDELITY. A projection must hold exactly what the chart would have rendered with the
     generated password in values -- the URL built from values with only the password replaced. For
     every ExternalSecret a consumer renders, each `{{ .<secretKey> }}` of its template is replaced
     by a known alphanumeric password for the root that secretKey reads, and the result must equal,
     key for key, the Secret of the same name (or of the name it replaced: RENAMED) that the chart
     renders when that password is set in values instead. A drop-in that has no Secret counterpart
     (a keystone_password_conf_snippet) is not compared; every other projection must be.
  9. NO SPLIT CLUSTER ON A COOKIE ROTATION. Every RabbitMQ node must hold the same Erlang cookie and a
     StatefulSet rolls one pod at a time, so the rabbitmq chart refuses to roll a new cookie through
     more than one replica (rabbitmq.erlang_cookie_rotation_check). Offline no lookup sees a live
     StatefulSet, so the check is rendered with stand-in live objects: refused for 3 replicas running
     the old cookie, and for 1 running with 3 wanted; allowed for 1 replica, for a StatefulSet already
     on the generated cookie, and when there is none.

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


# Per credential item: which charts must receive it (closure), where its password lives in a chart's
# values (the sentinel), and which projections a consumer must render instead of a Secret.
RULES = {
    "identity-admin": {
        "what": "renders a Keystone admin openrc Secret (templates/secret-keystone.yaml)",
        "consumes": lambda d, has: has(d, "secret-keystone.yaml"),
        "password_path": lambda chart: "endpoints.identity.auth.admin.password",
        "projections": lambda chart, cv: [((cv.get("secrets") or {}).get("identity") or {}).get("admin")],
    },
    "oslo-db-admin": {
        "what": "renders a MariaDB root DB_CONNECTION Secret (templates/secret-db.yaml) or is MariaDB itself",
        "consumes": lambda d, has: has(d, "secret-db.yaml") or d == "mariadb",
        "password_path": lambda chart: "endpoints.oslo_db.auth.admin.password",
        "projections": lambda chart, cv: (["mariadb-dbadmin-generated", "mariadb-secrets-generated"]
                                               if chart == "mariadb" else
                                               [((cv.get("secrets") or {}).get("oslo_db") or {}).get("admin")]),
    },
    # One credential under two keys: every component's rabbit-init authenticates as auth.admin against
    # the server whose administrator is the rabbitmq chart's auth.user.
    "oslo-messaging-admin": {
        "what": "renders a RabbitMQ admin connection Secret (templates/secret-rabbitmq.yaml) or is RabbitMQ itself",
        "consumes": lambda d, has: has(d, "secret-rabbitmq.yaml") or d == "rabbitmq",
        "password_path": lambda chart: ("endpoints.oslo_messaging.auth.user.password" if chart == "rabbitmq"
                                        else "endpoints.oslo_messaging.auth.admin.password"),
        # rabbitmq's Secrets are named after the chart, not the release (global.subchart_release_name).
        "projections": lambda chart, cv: (["rabbitmq-admin-user-generated"] if chart == "rabbitmq" else
                                          [((cv.get("secrets") or {}).get("oslo_messaging") or {}).get("admin")]),
        # A chart may ship the Secret disabled (keystone: manifests.secret_rabbitmq false) and an install
        # can enable it through componentValues; render it enabled, or the sentinel would pass vacuously.
        "enable": lambda chart: {} if chart == "rabbitmq" else {"manifests": {"secret_rabbitmq": True}},
    },
    "oslo-messaging-guest": {
        "what": "is RabbitMQ itself (the guest user is written into its definitions file)",
        "consumes": lambda d, has: d == "rabbitmq",
        "password_path": lambda chart: "endpoints.oslo_messaging.auth.guest.password",
        "projections": lambda chart, cv: ["rabbitmq-admin-user-generated"],
    },
    "oslo-messaging-erlang-cookie": {
        "what": "is RabbitMQ itself (the Erlang distribution cookie of its nodes)",
        "consumes": lambda d, has: d == "rabbitmq",
        "password_path": lambda chart: "endpoints.oslo_messaging.auth.erlang_cookie",
        "projections": lambda chart, cv: ["rabbitmq-erlang-cookie-generated"],
    },
}


def cookie_rotation_cases(tmp, refs):
    """9. Render the rabbitmq chart's own secret-erlang-cookie.yaml with its StatefulSet lookup replaced by
    stand-in live objects, so the call site is checked together with rabbitmq.erlang_cookie_rotation_check."""
    lookup = 'lookup "apps/v1" "StatefulSet" $envAll.Release.Namespace (printf "%s-rabbitmq" $envAll.deployment_name)'
    src = os.path.join(BP, "rabbitmq", "chart", "templates", "secret-erlang-cookie.yaml")
    text = open(src).read()
    if text.count(lookup) != 1:
        fail("rabbitmq: secret-erlang-cookie.yaml no longer looks up the live StatefulSet (%s), so nothing stops a "
             "cookie rotation from splitting a multi-replica cluster" % lookup)
        return
    cases = [  # (label, live replicas or None for no StatefulSet, mounted Secret, wanted replicas, refused)
        ("3 replicas on the old cookie", 3, "rabbitmq-erlang-cookie", 3, True),
        ("1 running, 3 wanted, old cookie", 1, "rabbitmq-erlang-cookie", 3, True),
        ("1 replica on the old cookie", 1, "rabbitmq-erlang-cookie", 1, False),
        ("3 replicas already on the generated cookie", 3, "rabbitmq-erlang-cookie-generated", 3, False),
        ("no StatefulSet yet", None, "", 3, False),
    ]
    for n, (label, live, mounted, wanted, refused) in enumerate(cases):
        chart = stage(os.path.join(BP, "rabbitmq", "chart"), tmp, "rabbitmq-cookie-%d" % n)
        if live is None:
            obj = "(dict)"
        else:
            obj = ('(dict "spec" (dict "replicas" %d "template" (dict "spec" (dict "volumes" (list (dict "name" '
                   '"rabbitmq-erlang-cookie" "secret" (dict "secretName" "%s")))))))' % (live, mounted))
        open(os.path.join(chart, "templates", "secret-erlang-cookie.yaml"), "w").write(text.replace(lookup, obj))
        values = json.loads(json.dumps(refs))
        deep_merge(values, {"pod": {"replicas": {"server": wanted}}})
        rc, out, err = helm_template(chart, "rabbitmq-gate1234", values)
        if refused and (rc == 0 or "every node of a cluster must hold the same cookie" not in err):
            fail("rabbitmq: a cookie rotation with %s is not refused (rc=%d)" % (label, rc))
        if not refused:
            if rc != 0:
                fail("rabbitmq: the cookie check refuses %s: %s" % (label, err.strip()[:200]))
                continue
            sts = [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "StatefulSet"]
            if [d["metadata"]["name"] for d in sts] != ["rabbitmq-rabbitmq"]:
                fail("rabbitmq: the StatefulSet is %s, but secret-erlang-cookie.yaml looks up <deployment>-rabbitmq "
                     "(rabbitmq-rabbitmq)" % [d["metadata"]["name"] for d in sts])


# A projection rendered under a new name (the rotation mechanism: a new name changes the pod spec),
# and the Secret it replaces at defaults.
RENAMED = {
    "mariadb-dbadmin-generated": "mariadb-dbadmin-password",
    "mariadb-secrets-generated": "mariadb-secrets",
    "rabbitmq-admin-user-generated": "rabbitmq-admin-user",
    "rabbitmq-erlang-cookie-generated": "rabbitmq-erlang-cookie",
}


def pod_secret_refs(docs):
    """Every Secret name a pod template in docs references (env, envFrom, volumes, projected)."""
    refs = set()
    for d in docs:
        spec = d.get("spec") or {}
        tpl = (spec.get("jobTemplate") or {}).get("spec", {}).get("template") if d.get("kind") == "CronJob" \
            else (spec if d.get("kind") == "Pod" else spec.get("template"))
        pod = (tpl or {}).get("spec") if d.get("kind") != "Pod" else spec
        if not isinstance(pod, dict):
            continue
        for c in (pod.get("containers") or []) + (pod.get("initContainers") or []):
            for e in c.get("env") or []:
                n = ((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
                if n:
                    refs.add(n)
            for e in c.get("envFrom") or []:
                n = (e.get("secretRef") or {}).get("name")
                if n:
                    refs.add(n)
        for v in pod.get("volumes") or []:
            if (v.get("secret") or {}).get("secretName"):
                refs.add(v["secret"]["secretName"])
            for src in (v.get("projected") or {}).get("sources") or []:
                if (src.get("secret") or {}).get("name"):
                    refs.add(src["secret"]["name"])
    return refs


def provided(docs):
    out = set()
    for d in docs:
        if d.get("kind") == "Secret":
            out.add(d["metadata"]["name"])
        elif d.get("kind") == "ExternalSecret":
            out.add(((d.get("spec") or {}).get("target") or {}).get("name") or d["metadata"]["name"])
    return out


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
            path = (i.get("pathFor") or {}).get(c["name"], i["path"])
            deep_merge(out, nested(path, {"store": creds["store"], "secret": i["secret"], "key": i["key"]}))
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

        # 2. closure, per credential item
        def has_template(d, t):
            return os.path.isfile(os.path.join(BP, d, "chart", "templates", t))
        charts = [d for d in os.listdir(BP) if os.path.isdir(os.path.join(BP, d, "chart")) and d != "openstack"]
        for cid, rule in RULES.items():
            if cid not in items:
                continue
            listed = {c["name"] for c in components if cid in (c.get("credentials") or [])}
            rendering = {d for d in charts if rule["consumes"](d, has_template)}
            for name in sorted(rendering - listed):
                fail("closure: %s %s but the umbrella does not pass it %s, so it would keep the published "
                     "default" % (name, rule["what"], cid))
            for name in sorted(listed - rendering):
                fail("closure: the umbrella passes %s to %s, which does not %s" % (cid, name, rule["what"]))
        for cid in items:
            if cid not in RULES:
                fail("credential item %s has no closure/sentinel rule in this gate" % cid)

        # 3. no value from values
        checked = 0
        root_secrets = {i["secret"]: i for i in items.values()}
        for c in components:
            cids = [cid for cid in (c.get("credentials") or []) if cid in RULES]
            if not cids:
                continue
            chart_dir = os.path.join(BP, c["name"], "chart")
            cv = yaml.safe_load(open(os.path.join(chart_dir, "values.yaml")))
            values = refs_for(c)
            enabled = {}
            for cid in cids:
                deep_merge(enabled, (RULES[cid].get("enable") or (lambda chart: {}))(c["name"]))
            deep_merge(values, json.loads(json.dumps(enabled)))
            sentinels = {}
            for cid in cids:
                sentinels[cid] = "gate-%s-sentinel-%s-7f3c9e" % (cid, c["name"])
                deep_merge(values, nested(RULES[cid]["password_path"](c["name"]), sentinels[cid]))
            w = stage(chart_dir, tmp, c["name"])
            release = c["name"] + "-gate1234"
            rc, out, err = helm_template(w, release, values)
            if rc != 0:
                fail("%s: does not render with its credential references: %s" % (c["name"], err.strip()[:300]))
                continue
            checked += 1
            docs = [d for d in yaml.safe_load_all(out) if d]
            for cid, sentinel in sentinels.items():
                if sentinel in out:
                    fail("%s: the %s password from values appears in the rendered text" % (c["name"], cid))
                for d in docs:
                    if d.get("kind") == "Secret":
                        for k, v in (d.get("data") or {}).items():
                            try:
                                if sentinel in base64.b64decode(v).decode("utf-8", "replace"):
                                    fail("%s: Secret %s key %s carries the %s password from values"
                                         % (c["name"], d["metadata"]["name"], k, cid))
                            except Exception:
                                pass
            for d in docs:
                if d.get("kind") != "ExternalSecret":
                    continue
                name = d["metadata"]["name"]
                if (d["metadata"].get("annotations") or {}).get("krateo.io/teardown") != "with-release":
                    fail("%s: ExternalSecret %s lacks krateo.io/teardown: with-release, so it is finalized "
                         "before the release's pre-delete hooks run and takes its Secret with it (#254)"
                         % (c["name"], name))
                spec = d["spec"]
                if spec.get("secretStoreRef", {}).get("name") != creds["store"]:
                    fail("%s: ExternalSecret %s does not read through store %s" % (c["name"], name, creds["store"]))
                if not spec.get("data"):
                    fail("%s: ExternalSecret %s reads nothing" % (c["name"], name))
                for entry in spec.get("data") or []:
                    ref = entry.get("remoteRef") or {}
                    item = root_secrets.get(ref.get("key"))
                    if item is None or ref.get("property") != item["key"]:
                        fail("%s: ExternalSecret %s does not read a credential root (reads %s/%s)"
                             % (c["name"], name, ref.get("key"), ref.get("property")))
                if spec.get("target", {}).get("creationPolicy") != "Owner":
                    fail("%s: ExternalSecret %s is not creationPolicy Owner (Orphan loses the Secret on a "
                         "retried upgrade, #228)" % (c["name"], name))
            # 8. projection fidelity
            known = {cid: "Gate%sFidelity%s7f3c" % (cid.replace("-", ""), c["name"].replace("-", "")) for cid in cids}
            values_g = json.loads(json.dumps(enabled))
            for cid in cids:
                deep_merge(values_g, nested(RULES[cid]["password_path"](c["name"]), known[cid]))
            rcg, outg, errg = helm_template(w, release, values_g)
            if rcg != 0:
                fail("%s: does not render with the passwords in values: %s" % (c["name"], errg.strip()[:300]))
            else:
                plain = {d["metadata"]["name"]: d for d in yaml.safe_load_all(outg)
                         if d and d.get("kind") == "Secret"}
                for d in docs:
                    if d.get("kind") != "ExternalSecret":
                        continue
                    name = d["metadata"]["name"]
                    spec = d["spec"]
                    tdata = ((spec.get("target") or {}).get("template") or {}).get("data") or {}
                    src = plain.get(RENAMED.get(name, name))
                    if src is None:
                        if any("[" in (v or "") and "]" in (v or "") for v in tdata.values()) and len(tdata) == 1:
                            continue  # a keystone_password_conf_snippet drop-in: no Secret counterpart
                        fail("%s: ExternalSecret %s has no Secret counterpart rendered from values to compare with"
                             % (c["name"], name))
                        continue
                    subst = {}
                    for entry in spec.get("data") or []:
                        item = root_secrets.get((entry.get("remoteRef") or {}).get("key"))
                        if item is not None and item["id"] in known:
                            subst[entry["secretKey"]] = known[item["id"]]
                    got = {}
                    for k, v in tdata.items():
                        for sk, pw in subst.items():
                            v = v.replace("{{ .%s }}" % sk, pw)
                        got[k] = v
                    want = {k: base64.b64decode(v).decode("utf-8", "replace")
                            for k, v in (src.get("data") or {}).items()}
                    want.update(src.get("stringData") or {})
                    if got != want:
                        diff = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
                        fail("%s: ExternalSecret %s does not project what values render with the same password "
                             "(keys differ: %s)" % (c["name"], name, diff))
                    elif any("{{" in v for v in got.values()):
                        fail("%s: ExternalSecret %s keeps a template action no root fills" % (c["name"], name))
            # 7. no dangling reference: a Secret the chart renders at defaults and a pod references must
            # still be provided (as a Secret or an ExternalSecret target) once the references are set.
            rc0, out0, _ = helm_template(w, release, enabled)
            if rc0 == 0:
                docs0 = [d for d in yaml.safe_load_all(out0) if d]
                dangling = (pod_secret_refs(docs) & provided(docs0)) - provided(docs)
                for n in sorted(dangling):
                    fail("%s: with its credential references a pod still references Secret %s, which is no "
                         "longer rendered (point it at the projection)" % (c["name"], n))
            for cid in cids:
                for want in RULES[cid]["projections"](c["name"], cv):
                    es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == want]
                    sec = [d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"] == want]
                    if sec:
                        fail("%s: still renders Secret %s itself (%s)" % (c["name"], want, cid))
                    if len(es) != 1:
                        fail("%s: renders %d ExternalSecret(s) named %s, want 1 (%s)" % (c["name"], len(es), want, cid))
                    elif items[cid]["secret"] not in [(e.get("remoteRef") or {}).get("key")
                                                      for e in es[0]["spec"].get("data") or []]:
                        fail("%s: ExternalSecret %s does not read %s" % (c["name"], want, items[cid]["secret"]))
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

        rabbit = [c for c in components if c["name"] == "rabbitmq"]
        if "oslo-messaging-erlang-cookie" in items and rabbit:
            cookie_rotation_cases(tmp, refs_for(rabbit[0]))
        elif "oslo-messaging-erlang-cookie" in items:
            fail("the umbrella defines oslo-messaging-erlang-cookie but no rabbitmq component")

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
