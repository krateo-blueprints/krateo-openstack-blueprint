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
     auth.user); oslo-messaging-guest and oslo-messaging-erlang-cookie to rabbitmq alone; oslo-db-sst,
     oslo-db-audit and oslo-db-exporter to mariadb alone.
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
  9. NO SPLIT CLUSTER, AND NO DEADLOCK, ON A COOKIE ROTATION. Every RabbitMQ node must hold the same
     Erlang cookie and a StatefulSet rolls one pod at a time, so the cookie only changes on a single
     node (rabbitmq.erlang_cookie_mode). Offline no lookup sees a live StatefulSet, so the chart is
     rendered with stand-in live objects, and what it renders is checked: refused while more than
     one replica is WANTED; HOLD (replicas 1, the old cookie still rendered and mounted) while more
     than one pod still runs -- 0.3.57 refused this case, so replicas 1 could never take effect
     (measured); rotate on one pod; normal on the generated cookie or with no StatefulSet.
  10. NO GUEST USER. RabbitMQ re-creates every user in its definitions file on every boot, and
     upstream's file declared guest, an administrator reachable from any pod, which lived until the
     next rabbit-init deleted it. The rabbitmq chart's rendered definitions script is RUN with only the
     administrator's variables and must write exactly that one user. oslo-messaging-guest is
     retired: its item stays (installs that ran 0.3.57 hold the root), marked retired, and no
     component may list it.
  11. THE GENERATED MEMCACHE KEY IS READ. keystonemiddleware's memcache_secret_key (one item per
     service, every chart whose configmap-etc.yaml sets it) arrives as a drop-in Secret. The files
     that carried the published key are found in the default render; with the reference, every
     container of a Deployment, StatefulSet or DaemonSet (the long-running servers keystonemiddleware
     runs in; Jobs and init containers never validate a token) that mounts one of them must also receive
     <service>-memcache-secret-key in a .conf.d
     directory (a projected etcSources volume or a file mount), and swift's proxy must load its
     config directory. A key in a Secret no daemon reads protects nothing.
  12. A STRATEGY. Each of those files must still set memcache_security_strategy and no longer set
     memcache_secret_key: without a strategy keystonemiddleware ignores the key and leaves the cache
     unprotected (cloudkitty, cyborg, ironic and masakari shipped without one).
  13. SHARED SECRETS ARE READ. The metadata proxy shared secret (nova [neutron], neutron's metadata
     agents [DEFAULT]) and Horizon's SECRET_KEY arrive as drop-ins. The files that carry them are found
     by rendering in values mode with a sentinel; with the reference, every long-running main
     container that mounts one must receive the drop-in where its program reads drop-ins (an
     oslo.config .conf.d, Horizon's local_settings.d), and the drop-in holds exactly the option.

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
    # Retired in 0.3.58: no guest user exists (check 10). The item stays, marked retired, because
    # installs that ran 0.3.57 hold its root; no component may list it.
    "oslo-messaging-guest": {"retired": True},
    "oslo-messaging-erlang-cookie": {
        "what": "is RabbitMQ itself (the Erlang distribution cookie of its nodes)",
        "consumes": lambda d, has: d == "rabbitmq",
        "password_path": lambda chart: "endpoints.oslo_messaging.auth.erlang_cookie",
        "projections": lambda chart, cv: ["rabbitmq-erlang-cookie-generated"],
    },
    # MariaDB's other built-in users. sst's password was also in the mariadb-etc ConfigMap
    # (wsrep_sst_auth), which the sentinel search over all rendered text now covers.
    "oslo-db-sst": {
        "what": "is MariaDB itself (the Galera state-transfer user)",
        "consumes": lambda d, has: d == "mariadb",
        "password_path": lambda chart: "endpoints.oslo_db.auth.sst.password",
        "projections": lambda chart, cv: ["mariadb-dbsst-generated"],
    },
    "oslo-db-audit": {
        "what": "is MariaDB itself (the audit user: SELECT on every database)",
        "consumes": lambda d, has: d == "mariadb",
        "password_path": lambda chart: "endpoints.oslo_db.auth.audit.password",
        "projections": lambda chart, cv: ["mariadb-dbaudit-generated"],
    },
    "oslo-db-exporter": {
        "what": "is MariaDB itself (the mysqld-exporter user)",
        "consumes": lambda d, has: d == "mariadb",
        "password_path": lambda chart: "endpoints.oslo_db.auth.exporter.password",
        "projections": lambda chart, cv: ["mysql-exporter-generated"],
        # Created only with monitoring on: render it on, or the sentinel would pass vacuously.
        "enable": lambda chart: {"monitoring": {"prometheus": {"enabled": True}}},
    },
}

# keystonemiddleware's memcache secret key, one item per service: every chart whose configmap-etc.yaml
# sets memcache_secret_key (derived from the repo, not listed by hand, so a new consumer cannot be missed).
MEMCACHE = sorted(d for d in os.listdir(BP)
                  if os.path.isfile(os.path.join(BP, d, "chart", "templates", "configmap-etc.yaml"))
                  and "memcache_secret_key" in open(os.path.join(BP, d, "chart", "templates", "configmap-etc.yaml")).read())
# The metadata proxy shared secret: one value, signed by neutron's metadata agents and verified by
# nova-api-metadata. Each chart holds it under its own conf paths (neutron: three agents' sections).
RULES["metadata-proxy-shared-secret"] = {
    "what": "renders the metadata proxy shared secret (nova [neutron], neutron's metadata agents)",
    "consumes": lambda d, has: d in ("nova", "neutron"),
    "password_path": lambda chart: (["conf.nova.neutron.metadata_proxy_shared_secret"] if chart == "nova" else
                                    ["conf.metadata_agent.DEFAULT.metadata_proxy_shared_secret",
                                     "conf.ovn_agent.DEFAULT.metadata_proxy_shared_secret",
                                     "conf.ovn_metadata_agent.DEFAULT.metadata_proxy_shared_secret"]),
    "projections": lambda chart, cv: ["%s-metadata-proxy-secret" % chart],
    "dropin": lambda chart: ("%s-metadata-proxy-secret" % chart, "81-metadata-proxy.conf",
                             "[%s]\nmetadata_proxy_shared_secret = {{ .password }}\n"
                             % ("neutron" if chart == "nova" else "DEFAULT")),
}
# Horizon's Django SECRET_KEY, delivered as a local_settings.d snippet.
RULES["horizon-secret-key"] = {
    "what": "is Horizon (its Django SECRET_KEY)",
    "consumes": lambda d, has: d == "horizon",
    "password_path": lambda chart: "conf.horizon.local_settings.config.horizon_secret_key",
    "projections": lambda chart, cv: ["horizon-secret-key"],
    "dropin": lambda chart: ("horizon-secret-key", "_90_krateo_secret_key.py", "SECRET_KEY = '{{ .password }}'\n"),
}
for _svc in MEMCACHE:
    RULES["memcache-%s" % _svc] = {
        "what": "is %s (its keystonemiddleware memcache secret key)" % _svc,
        "consumes": (lambda s: lambda d, has: d == s)(_svc),
        "password_path": lambda chart: "endpoints.oslo_cache.auth.memcache_secret_key",
        "projections": (lambda s: lambda chart, cv: ["%s-memcache-secret-key" % s])(_svc),
    }


def no_guest_user(tmp):
    """10. Run the rabbitmq chart's own rendered definitions script and require exactly the administrator."""
    chart = stage(os.path.join(BP, "rabbitmq", "chart"), tmp, "rabbitmq-defs")
    rc, out, err = helm_template(chart, "rabbitmq-gate1234")
    script = None
    if rc == 0:
        for d in yaml.safe_load_all(out):
            if d and d.get("kind") == "ConfigMap" and "rabbitmq-password-hash.py" in (d.get("data") or {}):
                script = d["data"]["rabbitmq-password-hash.py"]
    if script is None:
        fail("rabbitmq: no rabbitmq-password-hash.py in a rendered ConfigMap (rc=%s %s)" % (rc, err.strip()[:200]))
        return
    defs = os.path.join(tmp, "gate-definitions.json")
    env = {"PATH": os.environ.get("PATH", ""), "RABBITMQ_ADMIN_USERNAME": "gate-admin",
           "RABBITMQ_ADMIN_PASSWORD": "gate-admin-pw", "RABBITMQ_DEFINITION_FILE": defs}
    r = subprocess.run([sys.executable, "-I", "-c", script], env=env, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(defs):
        fail("rabbitmq: the definitions script does not run with only the administrator's variables: %s"
             % (r.stderr.strip()[-300:]))
        return
    users = [u.get("name") for u in json.load(open(defs)).get("users", [])]
    if users != ["gate-admin"]:
        fail("rabbitmq: the definitions file declares users %s, want only the administrator: a guest user written "
             "there is re-created, as an administrator, on every RabbitMQ boot" % users)


def cookie_rotation_cases(tmp, refs):
    """9. Render the rabbitmq chart with its one StatefulSet lookup (rabbitmq.erlang_cookie_live) replaced by
    stand-in live objects, and check what each case renders: refused, hold, rotate or normal."""
    lookup = 'lookup "apps/v1" "StatefulSet" .Release.Namespace (printf "%s-rabbitmq" .deployment_name)'
    src = os.path.join(BP, "rabbitmq", "chart", "templates", "_erlang_cookie.tpl")
    text = open(src).read()
    if text.count(lookup) != 1:
        fail("rabbitmq: _erlang_cookie.tpl no longer looks up the live StatefulSet exactly once (%s), so the cookie "
             "rotation cannot know how many nodes run" % lookup)
        return
    old, new = "rabbitmq-erlang-cookie", "rabbitmq-erlang-cookie-generated"
    # (label, live: None or (spec replicas, status pods, mounted Secret), wanted, expect)
    # expect: "refused", or (Secret the StatefulSet mounts and reads, rendered replicas, values Secret rendered?)
    cases = [
        ("3 running on the old cookie, 3 wanted", (3, 3, old), 3, "refused"),
        ("1 running on the old cookie, 3 wanted", (1, 1, old), 3, "refused"),
        ("2 running on the old cookie, 1 wanted (0.3.57 deadlocked here)", (2, 2, old), 1, (old, 1, True)),
        ("spec 1 but a 2nd pod still terminating, old cookie", (1, 2, old), 1, (old, 1, True)),
        ("1 running on the old cookie, 1 wanted", (1, 1, old), 1, (new, 1, False)),
        ("3 running already on the generated cookie", (3, 3, new), 3, (new, 3, False)),
        ("no StatefulSet yet", None, 3, (new, 3, False)),
    ]
    for n, (label, live, wanted, expect) in enumerate(cases):
        chart = stage(os.path.join(BP, "rabbitmq", "chart"), tmp, "rabbitmq-cookie-%d" % n)
        if live is None:
            obj = "(dict)"
        else:
            obj = ('(dict "spec" (dict "replicas" %d "template" (dict "spec" (dict "volumes" (list (dict "name" '
                   '"rabbitmq-erlang-cookie" "secret" (dict "secretName" "%s")))))) "status" (dict "replicas" %d))'
                   % (live[0], live[2], live[1]))
        open(os.path.join(chart, "templates", "_erlang_cookie.tpl"), "w").write(text.replace(lookup, obj))
        values = json.loads(json.dumps(refs))
        deep_merge(values, {"pod": {"replicas": {"server": wanted}}})
        rc, out, err = helm_template(chart, "rabbitmq-gate1234", values)
        if expect == "refused":
            if rc == 0 or "every node of a cluster must hold the same cookie" not in err:
                fail("rabbitmq: a cookie rotation with %s is not refused (rc=%d)" % (label, rc))
            continue
        if rc != 0:
            fail("rabbitmq: the cookie rotation refuses %s: %s" % (label, err.strip()[:200]))
            continue
        docs = [d for d in yaml.safe_load_all(out) if d]
        sts = [d for d in docs if d.get("kind") == "StatefulSet"]
        if [d["metadata"]["name"] for d in sts] != ["rabbitmq-rabbitmq"]:
            fail("rabbitmq: the StatefulSet is %s, but the lookup reads <deployment>-rabbitmq (rabbitmq-rabbitmq)"
                 % [d["metadata"]["name"] for d in sts])
            continue
        spec = sts[0]["spec"]
        pod = spec["template"]["spec"]
        vol = [v["secret"]["secretName"] for v in pod.get("volumes", []) if v["name"] == "rabbitmq-erlang-cookie"]
        env = [e["valueFrom"]["secretKeyRef"]["name"] for c in pod["containers"] for e in c.get("env", [])
               if e["name"] == "RABBITMQ_ERLANG_COOKIE" and "valueFrom" in e]
        want_secret, want_replicas, values_secret = expect
        if vol != [want_secret] or env != [want_secret]:
            fail("rabbitmq: with %s the StatefulSet mounts %s and reads %s, want %s" % (label, vol, env, want_secret))
        if spec.get("replicas") != want_replicas:
            fail("rabbitmq: with %s the StatefulSet renders %s replicas, want %d" % (label, spec.get("replicas"),
                                                                                    want_replicas))
        rendered_values = any(d.get("kind") == "Secret" and d["metadata"]["name"] == old for d in docs)
        if rendered_values != values_secret:
            fail("rabbitmq: with %s Secret %s is %s, want %s (a mounted Secret must not be deleted while it is "
                 "mounted)" % (label, old, "rendered" if rendered_values else "absent",
                               "rendered" if values_secret else "absent"))
        if not any(d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == new for d in docs):
            fail("rabbitmq: with %s the generated cookie's ExternalSecret %s is not rendered" % (label, new))


# A projection rendered under a new name (the rotation mechanism: a new name changes the pod spec),
# and the Secret it replaces at defaults.
RENAMED = {
    "mariadb-dbadmin-generated": "mariadb-dbadmin-password",
    "mariadb-secrets-generated": "mariadb-secrets",
    "rabbitmq-admin-user-generated": "rabbitmq-admin-user",
    "rabbitmq-erlang-cookie-generated": "rabbitmq-erlang-cookie",
    "mariadb-dbsst-generated": "mariadb-dbsst-password",
    "mariadb-dbaudit-generated": "mariadb-dbaudit-password",
    "mysql-exporter-generated": "mysql-exporter-secrets",
}


def paths(p):
    return p if isinstance(p, (list, tuple)) else [p]


def pod_specs(docs):
    """(object name, pod spec) for every pod template in docs."""
    out = []
    for d in docs:
        spec = d.get("spec") or {}
        kind = d.get("kind")
        if kind == "Pod":
            pod = spec
        elif kind == "CronJob":
            pod = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
        else:
            pod = (spec.get("template") or {}).get("spec")
        if isinstance(pod, dict) and (pod.get("containers") or pod.get("initContainers")):
            out.append(("%s/%s" % (kind, d["metadata"]["name"]), pod))
    return out


def object_text(d, key):
    v = (d.get("data") or {}).get(key)
    if v is None:
        return None
    if d.get("kind") == "Secret":
        try:
            return base64.b64decode(v).decode("utf-8", "replace")
        except Exception:
            return None
    return str(v)


def memcache_delivery(svc, cv, docs0, docs):
    """11/12. The generated memcache key reaches every container that reads a config file which carried
    the published key, through a .conf.d directory; and that file still sets a strategy."""
    published = str((((cv.get("endpoints") or {}).get("oslo_cache") or {}).get("auth") or {})
                    .get("memcache_secret_key") or "")
    carriers = set()
    for d in docs0:
        if d.get("kind") in ("Secret", "ConfigMap"):
            for k in (d.get("data") or {}):
                t = object_text(d, k)
                if published and t and published in t:
                    carriers.add((d["kind"], d["metadata"]["name"], k))
    if not carriers:
        fail("%s: no rendered config file carries its memcache_secret_key at defaults, so where the generated "
             "key must arrive cannot be determined" % svc)
        return
    byname = {(d.get("kind"), d["metadata"]["name"]): d for d in docs}
    sections = set()
    for kind, name, key in sorted(carriers):
        d = byname.get((kind, name))
        t = object_text(d, key) if d else None
        if t is None:
            fail("%s: %s %s/%s carried the key at defaults and is not rendered with the reference" % (svc, kind, name, key))
            continue
        if "memcache_security_strategy" not in t:
            fail("%s: %s %s/%s sets no memcache_security_strategy, so no key protects the token cache" % (svc, kind, name, key))
        if "memcache_secret_key" in t:
            fail("%s: %s %s/%s still sets memcache_secret_key itself" % (svc, kind, name, key))
        section = None
        for line in t.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif line.replace(" ", "").startswith("memcache_security_strategy="):
                sections.add(section)
    drop = "%s-memcache-secret-key" % svc
    es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == drop]
    tdata = (((es[0].get("spec") or {}).get("target") or {}).get("template") or {}).get("data") or {} if es else {}
    # keystonemiddleware reads its options from [keystone_authtoken] (swift: the paste [filter:authtoken]);
    # client sections that copy the strategy (octavia [service_auth], manila [nova] ...) are keystoneauth
    # sessions, which never use a memcache key.
    mw = "filter:authtoken" if svc == "swift" else "keystone_authtoken"
    want = {"80-memcache-secret-key.conf": "[%s]\nmemcache_secret_key = {{ .password }}\n" % mw}
    if mw not in sections:
        fail("%s: [%s] sets no memcache_security_strategy, so keystonemiddleware ignores any key" % (svc, mw))
    if tdata != want:
        fail("%s: the drop-in %s holds %s, want %s" % (svc, drop, sorted(tdata.items()), sorted(want.items())))
    readers = 0
    for podname, pod in pod_specs(docs):
        # keystonemiddleware runs in long-running servers only; one-shot Jobs, CronJobs and test Pods read
        # the file for other settings (database, messaging) and never validate a token.
        if podname.split("/")[0] not in ("Deployment", "StatefulSet", "DaemonSet"):
            continue
        vols = {v["name"]: v for v in pod.get("volumes") or []}
        for cont in pod.get("containers") or []:  # init containers are one-shot too
            mounts = cont.get("volumeMounts") or []
            reads = []
            for m in mounts:
                v = vols.get(m["name"]) or {}
                obj = (("Secret", (v.get("secret") or {}).get("secretName")) if "secret" in v else
                       ("ConfigMap", (v.get("configMap") or {}).get("name")) if "configMap" in v else (None, None))
                for kind, name, key in carriers:
                    if (kind, name) == obj and m.get("subPath") in (None, key):
                        reads.append(key)
            if not reads:
                continue
            readers += 1
            delivered = False
            for m in mounts:
                v = vols.get(m["name"]) or {}
                srcs = [(v.get("secret") or {}).get("secretName")] if "secret" in v else \
                       [(x.get("secret") or {}).get("name") for x in (v.get("projected") or {}).get("sources") or []]
                if drop in srcs:
                    path = m["mountPath"].rstrip("/")
                    if (path if not m.get("subPath") else os.path.dirname(path)).endswith(".conf.d"):
                        delivered = True
            if not delivered:
                fail("%s: %s container %s reads %s but does not receive %s in a .conf.d directory: the "
                     "generated key would sit in a Secret no daemon reads" % (svc, podname, cont["name"],
                                                                            sorted(set(reads)), drop))
    if readers == 0:
        fail("%s: no container reads a config file that carried the memcache key" % svc)
    if svc == "swift":
        script = None
        for d in docs:
            if d.get("kind") == "ConfigMap" and "proxy-start.sh" in (d.get("data") or {}):
                script = d["data"]["proxy-start.sh"]
        if not script or "exec swift-proxy-server /etc/swift/proxy-server.conf.d" not in script:
            fail("swift: with the reference the proxy must load /etc/swift/proxy-server.conf.d (swift reads a "
                 "config directory; the drop-in is in it)")


def dropin_delivery(chart, cid, sentinel, docs_s, docs):
    """13. A secret delivered as a drop-in reaches every long-running main container that reads a file
    which carried it in values mode (found by rendering with a sentinel), in a directory the program
    reads (an oslo.config .conf.d, or Horizon's local_settings.d); and the drop-in holds exactly
    `option = <generated>` in its section."""
    drop, fname, content = RULES[cid]["dropin"](chart)
    carriers = set()
    for d in docs_s:
        if d.get("kind") in ("Secret", "ConfigMap"):
            for k in (d.get("data") or {}):
                t = object_text(d, k)
                if t and sentinel in t:
                    carriers.add((d["kind"], d["metadata"]["name"], k))
    if not carriers:
        fail("%s: no rendered file carries %s in values mode, so where the generated value must arrive cannot "
             "be determined" % (chart, cid))
        return
    es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == drop]
    tdata = (((es[0].get("spec") or {}).get("target") or {}).get("template") or {}).get("data") or {} if es else {}
    if tdata != {fname: content}:
        fail("%s: the drop-in %s holds %s, want %s" % (chart, drop, sorted(tdata.items()), [(fname, content)]))
    readers = 0
    for podname, pod in pod_specs(docs):
        if podname.split("/")[0] not in ("Deployment", "StatefulSet", "DaemonSet"):
            continue
        vols = {v["name"]: v for v in pod.get("volumes") or []}
        for cont in pod.get("containers") or []:
            mounts = cont.get("volumeMounts") or []
            reads = set()
            for m in mounts:
                v = vols.get(m["name"]) or {}
                obj = (("Secret", (v.get("secret") or {}).get("secretName")) if "secret" in v else
                       ("ConfigMap", (v.get("configMap") or {}).get("name")) if "configMap" in v else (None, None))
                for kind, name, key in carriers:
                    if (kind, name) == obj and m.get("subPath") in (None, key):
                        reads.add(key)
            if not reads:
                continue
            readers += 1
            ok = False
            for m in mounts:
                v = vols.get(m["name"]) or {}
                srcs = [(v.get("secret") or {}).get("secretName")] if "secret" in v else \
                       [(x.get("secret") or {}).get("name") for x in (v.get("projected") or {}).get("sources") or []]
                if drop in srcs:
                    path = m["mountPath"].rstrip("/")
                    d = path if not m.get("subPath") else os.path.dirname(path)
                    if d.endswith(".conf.d") or d.endswith("local_settings.d"):
                        ok = True
            if not ok:
                fail("%s: %s container %s reads %s but does not receive %s where it reads drop-ins"
                     % (chart, podname, cont["name"], sorted(reads), drop))
    if readers == 0:
        fail("%s: no long-running container reads a file that carried %s" % (chart, cid))


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
                continue  # reported below: a rule without its item
            if rule.get("retired"):
                if items[cid].get("retired") is not True:
                    fail("credential %s is retired in this gate but its umbrella item lacks retired: true" % cid)
                for c in components:
                    if cid in (c.get("credentials") or []):
                        fail("closure: %s lists retired credential %s" % (c["name"], cid))
                continue
            if items[cid].get("retired"):
                fail("credential %s is marked retired in the umbrella but this gate still has a live rule" % cid)
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
        for cid in RULES:
            if cid not in items:
                fail("this gate has a rule for %s but the umbrella defines no such item: removing an item puts "
                     "every consumer back on the published default (before any release ships its root, check 4 "
                     "cannot see that)" % cid)

        # 3. no value from values
        checked = 0
        root_secrets = {i["secret"]: i for i in items.values()}
        for c in components:
            cids = [cid for cid in (c.get("credentials") or []) if cid in RULES and not RULES[cid].get("retired")]
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
                for pp in paths(RULES[cid]["password_path"](c["name"])):
                    deep_merge(values, nested(pp, sentinels[cid]))
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
                for pp in paths(RULES[cid]["password_path"](c["name"])):
                    deep_merge(values_g, nested(pp, known[cid]))
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
                        if len(tdata) == 1 and list(tdata)[0].endswith((".conf", ".py")):
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
            for cid in cids:
                if "dropin" not in RULES[cid]:
                    continue
                values_s = json.loads(json.dumps(enabled))
                for pp in paths(RULES[cid]["password_path"](c["name"])):
                    deep_merge(values_s, nested(pp, sentinels[cid]))
                rcs, outs, errs = helm_template(w, release, values_s)
                if rcs != 0:
                    fail("%s: does not render in values mode with the %s sentinel: %s" % (c["name"], cid, errs.strip()[:200]))
                else:
                    dropin_delivery(c["name"], cid, sentinels[cid], [d for d in yaml.safe_load_all(outs) if d], docs)
            if any(cid.startswith("memcache-") for cid in cids):
                if rc0 == 0:
                    memcache_delivery(c["name"], cv, docs0, docs)
                else:
                    fail("%s: does not render at defaults, so memcache delivery cannot be checked" % c["name"])
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

        no_guest_user(tmp)
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
