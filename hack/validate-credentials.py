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
     auth.user); oslo-messaging-guest and oslo-messaging-erlang-cookie to rabbitmq alone; oslo-db-sst and
     oslo-db-audit to mariadb alone; oslo-db-exporter to mariadb and openstack-monitoring (#312).
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
     agents [DEFAULT]) arrives as an oslo.config drop-in. The files that carry it are found by
     rendering in values mode with a sentinel; with the reference, every long-running main container
     that mounts one must receive the drop-in in a .conf.d directory, and the drop-in holds exactly the
     option. (A directory name is not proof a program reads it: 0.3.61 mounted Horizon's key into
     /etc/.../local_settings.d, which Horizon never executes -- measured, a random key per pod.)
  14. A KEY READ FROM A FILE. Horizon's SECRET_KEY: local_settings assigns it exactly once, from the
     mounted file; every pod template that mounts local_settings (the db-sync Job imports it too)
     mounts the Secret at that directory; the ExternalSecret holds exactly that file.
  17. SERVICE USERS ROTATE BEFORE THEIR READERS. For every rotated service user, the ks-user Job that creates
     it is post-install,pre-upgrade, reads the password from the umbrella's root (a pre-upgrade hook runs
     before the projection is applied), and its script exempts the user from lockout BEFORE setting it and
     sets the password LAST, after every other openstack command (0.3.65: setting it first made the readers'
     failure window the Job's whole 1-3 minute run).
  19. EVERY PARSE READS THE DROP-INS. In every container that mounts a .conf.d, every OpenStack program its scripts
     start passes --config-dir <that dir> (0.3.65: watcher's plugin loader re-parsed without a project and its
     decision engine lost every drop-in). Which programs accept the flag is measured, in
     hack/oslo-config-dir-programs.yaml: an unlisted program fails, a listed rejecting one must not get the flag
     (0.3.66 gave it to barbican-db-manage, an argparse program, and its db-sync crash-looped), and a stale entry
     fails. 19b: an unguarded --config-dir names a directory mounted at defaults.
  16b. RELOCATED FILES. A file that carried a service user's password and that the references render drops from
     its Secret (glance's swift-store.conf) is rendered by an ExternalSecret reading that user's root, identical
     to the values-mode file with the sentinel, and mounted at the same path by every container that read it.
  21. A KEY, NOT A PASSWORD. manila's generic driver logs in to its service VMs with a generated SSH key pair (an
     SSHKey item, 0.3.67): a generated password would lock it out of the stock image, whose password login is built
     in. With the reference, the [generic] section of manila.conf sets no service_instance_password and points
     path_to_private_key and path_to_public_key at files; the ExternalSecret holds exactly the item's two keys; and every
     container that starts manila-share (Deployment and StatefulSet mode) mounts the private key and the public key at
     exactly those paths, the private key not world-readable and readable by the container's user (root, or the pod's
     fsGroup, which the kubelet gives group read). Each umbrella root is generated by its item's generator kind.
  19c. INIT CONTAINERS READ THE DROP-INS (#291). Every init container, in every chart and every rendered mode, that starts
     a <project>-* program and mounts that project's main config also mounts its .conf.d; and checks 13, 22 and 22m treat
     such an init container as a reader like a main container (an init container may hold a drop-in it does not use only
     when a reader main container of its own pod receives the same one).
  20. NO PUBLISHED LITERAL. With every reference set, no password-like ini option outside the RabbitMQ and cache sections
     holds a value the chart publishes (the database sections were exempt until their users had roots, 0.3.68). 20b: no
     URL in rendered config, Secrets or ExternalSecret templates logs in with a published password as an install-wide
     administrator (MariaDB root, RabbitMQ admin) or, in a mysql URL, as a per-service MariaDB user (hack/db-users.yaml).
  15b. hack/db-users.yaml is exactly what the charts render: every per-service endpoints.oslo_db*.auth key, rendered with a
     sentinel, with the map's username; and the umbrella's oslo-db-<user> item reaches exactly the map's keys (path and
     extraPaths) in the chart that lists it.
  25. ONE ACCOUNT, ONE PASSWORD. A MariaDB username a chart names under several keys (nova's three databases, octavia's
     persistence database) is one account. Rendered with two different passwords in values, two different references,
     or a reference on one key only, the chart must refuse, naming the keys and no password; with the same password or
     reference under every key it renders (db-init's ALTER USER would otherwise set each in turn).
  22. THE DATABASE URL DROP-IN. For a per-service MariaDB user with its root, every ini option of the chart's config that
     carried the user's password in values mode (found by rendering with a sentinel) is gone from that config and is in
     the drop-in <chart>-db-connection (87-db-connection.conf), as exactly the values-mode URL with the password read from
     the user's root (URL-quoted); every container of a Deployment, StatefulSet, DaemonSet, Job or CronJob that mounts a
     file which carried it receives the drop-in in a .conf.d directory, and no container that reads none of those files
     receives it. In a chart listed in hack/db-readers.yaml (neutron: its agents never use the database), only the
     listed containers may receive it, and every other one must not. db-drop and the optional workloads are rendered on
     too, so their readers are checked.
  23. DB-INIT SETS THE PASSWORD, AND READS THE DROP-IN. Every chart's rendered db-init.py is RUN against a stand-in
     SQLAlchemy with its config split like a referenced render (the connection only in <config>.d): it must CREATE USER IF
     NOT EXISTS and then ALTER USER ... IDENTIFIED BY with the drop-in's user and password (CREATE alone never changes an
     existing user's password, so a generated one never reached MariaDB), and db-drop.py must drop that user.
  24. NOVA'S CELL MAPPINGS HOLD NO PASSWORD. db-sync writes every cell mapping URL as a template nova formats from each
     process's own config (cell1 and cell0 database, cell1 transport), and the templates format to exactly the URLs
     nova.conf names: a literal would store the password in nova_api, and a process that cached it before an update
     kept it until it exited (CELL_CACHE). 24b: every container that loads cell mappings has the [database] connection
     and [DEFAULT] transport_url the templates format from.
  15c. hack/mq-users.yaml is exactly what the charts render: every per-service endpoints.oslo_messaging.auth key, rendered
     with a sentinel, under the map's username (0.3.69). ONE USERNAME, ONE ROOT: each oslo-messaging-<user> item reaches
     every chart rendering that username at its key (closure, check 2), and no other item reaches any of those keys; a
     username two charts render (a cross-chart reader) is listed under both, with one root. 20b covers a RabbitMQ URL
     logging in, at any of its hosts, as a per-service RabbitMQ user, and check 20 no longer exempts the messaging
     sections.
  22m. THE TRANSPORT URL DROP-IN. For a per-service RabbitMQ user with its root, every ini option that carried the user's
     password in values mode is gone from that config and is in the drop-in <chart>-transport (88-transport.conf), as
     exactly the values-mode URL with every host's password read from the root (URL-quoted); a container mounting a carrier
     receives it in a .conf.d exactly when hack/mq-readers.yaml lists it (its program sends or receives over RabbitMQ,
     measured per program), and no other container does; every exec probe of a receiving container that parses a carrier
     with --config-file passes --config-dir <that .conf.d> (neutron's health-probe.py reads no .conf.d by itself, and
     without transport_url reports success unchecked). Every optional workload is rendered on too.
  23m. RABBIT-INIT SETS THE PASSWORD. Every chart's rendered rabbit-init.sh is RUN with a stand-in rabbitmqadmin that
     reports the user as existing, on the connections its Job reads (built from the roots): it must `declare user` with
     the root's password (an HTTP PUT, create or replace: measured on the chart's rabbitmq:3.13-management image to set a
     new password on an existing user), then declare the vhost and the user's permissions.
  26. NOTIFICATION VHOSTS THAT EXIST (#285). components[].rabbitmqVhost is exactly the vhost each chart's rabbit-init
     declares, and rabbitmqInit: false exactly where the chart ships that Job off (measured from renders);
     notificationVhosts.vhosts is ceilometer's own conf.notification_vhosts default; and for the identity, full and
     full-strict profiles, sample explicit selections, and componentValues that switch a component's rabbit-init off or on
     or rename its vhost, the vhosts ceilometer renders in [notification] messaging_urls, given the spec compositions.yaml
     builds, are exactly those of that list the selected closure creates, under the names it creates them (each chart
     rendered with its componentValues).
  18. READERS RESTART. Every Deployment, StatefulSet and DaemonSet that reads an ExternalSecret projecting an
     item's root has a pod template that differs between the render without that one item's reference and the
     render with it, every other reference set in both (0.3.63: ks-etc kept its name, nothing hashed it, and
     stale readers locked nova, neutron and placement for 30 minutes -- measured).

Fails when helm or PyYAML is missing, when the umbrella defines no credential, when a listed chart
does not render, and when no chart was checked.
"""
import base64
import glob
import json
import os
import re
import urllib.parse
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
        r = subprocess.run(args + gate_values.helm_args(chart) + ["-f", vf], capture_output=True, text=True)
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
    # The mysqld-exporter user: MariaDB creates it (the sidecar's 'exporter'@'127.0.0.1', and with krateo.exporterUser
    # the standalone exporter's 'exporter'@'%', #312), and openstack-monitoring's standalone exporter logs in as it.
    "oslo-db-exporter": {
        "what": "is MariaDB itself, or openstack-monitoring's standalone mysqld-exporter (the mysqld-exporter user)",
        "consumes": lambda d, has: d in ("mariadb", "openstack-monitoring"),
        "password_path": lambda chart: "endpoints.oslo_db.auth.exporter.password",
        "projections": lambda chart, cv: (["mysql-exporter-generated", "mariadb-exporter-user"] if chart == "mariadb"
                                          else [cv["secrets"]["mysqld_exporter_cnf"]]),
        # Created only with monitoring on (and the standalone user with krateo.exporterUser): render both on, or the
        # sentinel would pass vacuously.
        "enable": lambda chart: ({"monitoring": {"prometheus": {"enabled": True}}, "krateo": {"exporterUser": True}}
                                 if chart == "mariadb" else {}),
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
    # neutron renders the OVN agents' files only with the OVN backend (instead of metadata_agent.ini), so
    # that mode is rendered too: otherwise their copies of the secret would pass the sentinel vacuously.
    "variants": lambda chart: ([{"network": {"backend": ["ovn"]},
                                 "manifests": {"daemonset_ovn_agent": True, "daemonset_ovn_metadata_agent": True}}]
                               if chart == "neutron" else []),
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
    # Set in local_settings itself, read from a mounted file (0.3.61's local_settings.d snippet was never
    # executed: Horizon runs only the snippets its start script links, and fell back to a random key per pod).
    "fileread": lambda chart: {"secret": "horizon-secret-key", "dir": "/etc/openstack-dashboard/krateo-secret-key",
                               "file": "secret_key", "carrier": ("horizon-etc", "local_settings"),
                               "line": "SECRET_KEY = open('/etc/openstack-dashboard/krateo-secret-key/secret_key', "
                                       "encoding='utf-8').read().strip()  # generated per install (#228)"},
}
# zaqar's signed-URL key (0.3.67): the HMAC key of pre-signed queue URLs, server-side only, delivered as a drop-in.
RULES["zaqar-signed-url-secret-key"] = {
    "what": "is zaqar (the HMAC key of its signed URLs)",
    "consumes": lambda d, has: d == "zaqar",
    "password_path": lambda chart: "conf.zaqar.signed_url.secret_key",
    "projections": lambda chart, cv: ["zaqar-signed-url-secret-key"],
    "dropin": lambda chart: ("zaqar-signed-url-secret-key", "82-signed-url.conf",
                             "[signed_url]\nsecret_key = {{ .password }}\n"),
}
# manila's service-instance login (0.3.67): an SSH key pair replaces the published password (check 21). The sentinel
# sits where the password did, so check 3 fails if the password survives the reference.
RULES["manila-service-instance-ssh-key"] = {
    "what": "is manila (its generic driver's service-instance login)",
    "consumes": lambda d, has: d == "manila",
    "password_path": lambda chart: "conf.manila.generic.service_instance_password",
    "projections": lambda chart, cv: ["manila-service-instance-ssh-key"],
    # manila-share runs as a StatefulSet in that mode: the key must reach it there too
    "variants": lambda chart: [{"manifests": {"statefulset_share": True}}],
    "keyfile": lambda chart: {"secret": "manila-service-instance-ssh-key", "carrier": ("manila-etc", "manila.conf"),
                              "section": "generic", "private": "path_to_private_key", "public": "path_to_public_key",
                              "replaces": "service_instance_password", "programs": ("manila-share",)},
}
# Service Keystone users: one item per USERNAME (id identity-user-<username with - for _>). Which charts render
# which username under which key is the committed map hack/identity-users.yaml, re-verified by check 15.
IDUSERS = (yaml.safe_load(open(os.path.join(ROOT, "hack", "identity-users.yaml"))) or {}).get("users") or {}
IDUSER_BY_ITEM = {"identity-user-" + u.replace("_", "-"): u for u in IDUSERS}
# Charts that read a service user from their main config and receive it as a drop-in (the others project it
# through <chart>-ks-etc or only their openrc Secret).
IDUSER_DROPINS = {
    ("cyborg", "nova"): ("cyborg-identity-nova", "83-identity-nova.conf", "[nova]\npassword = {{ .password }}\n"),
    ("cyborg", "placement"): ("cyborg-identity-placement", "84-identity-placement.conf",
                              "[placement]\npassword = {{ .password }}\n"),
    # blazar's physical-host plugin calls Nova as the blazar user (0.3.67: upstream's literal password stayed in
    # blazar.conf with the user generated, found by check 20 on a live scan, GKE 2026-11-16).
    ("blazar", "blazar"): ("blazar-physical-host-password-etc", "91-physical-host-password.conf",
                           "[physical_host_plugin]\nblazar_password = {{ .password }}\n"),
}
# Users whose ks-user entry (and config) a chart renders only in a mode the defaults do not select: rendered in
# that mode too, so checks 1 and 17 do not pass vacuously for them.
IDUSER_VARIANTS = {
    ("neutron", "neutron_designate"): {"conf": {"neutron": {"DEFAULT": {"external_dns_driver": "designate"}}}},
    ("neutron", "neutron_ironic"): {"network": {"backend": ["openvswitch", "baremetal"]}},
    ("nova", "nova_ironic"): {"manifests": {"statefulset_compute_ironic": True}},
    # masakari's monitor DaemonSets (off by default) read the masakari user from masakarimonitors.conf
    ("masakari", "masakari"): {"manifests": {"host_monitor": True, "instance_monitor": True, "process_monitor": True,
                                             "introspective_instance_monitor": True}},
}


def iduser_dropin(chart, u):
    """The drop-in that delivers user u's password to chart's main config: a dedicated one (IDUSER_DROPINS),
    or the chart's single identity drop-in (slice 2b: the chart lists its entries in
    templates/_identity_dropin_entries.tpl), whose expected lines check 13 derives from the sentinel render."""
    if (chart, u) in IDUSER_DROPINS:
        return IDUSER_DROPINS[(chart, u)]
    f = os.path.join(BP, chart, "chart", "templates", "_identity_dropin_entries.tpl")
    if os.path.exists(f) and ('"user" "%s"' % IDUSERS[u].get(chart, "gate_unmapped")) in open(f).read():
        return ("%s-identity" % chart, "85-identity.conf", None)
    return None


def ini_sentinel_options(text, sentinel):
    """(section, option) pairs whose value is exactly the sentinel in an ini-style text."""
    out, section = set(), None
    for line in text.splitlines():
        t = line.strip()
        if t.startswith("[") and t.endswith("]"):
            section = t[1:-1]
        elif "=" in t and not t.startswith(("#", ";")):
            k, _, val = t.partition("=")
            if val.strip() == sentinel:
                out.add((section, k.strip()))
    return out


for _cid, _u in IDUSER_BY_ITEM.items():
    RULES[_cid] = {
        "what": "renders Keystone user %s" % _u,
        "consumes": (lambda u: lambda d, has: d in IDUSERS[u])(_u),
        # An umbrella listing the map does not know is reported by checks 2 and 15; render it under a key no
        # chart has rather than crash here.
        "password_path": (lambda u: lambda chart: "endpoints.identity.auth.%s.password"
                          % IDUSERS[u].get(chart, "gate_unmapped"))(_u),
        "projections": lambda chart, cv: [],
        "carriers": True,
        "dropin_for": (lambda u: lambda chart: iduser_dropin(chart, u))(_u),
        "variants": (lambda u: lambda chart: [IDUSER_VARIANTS[(chart, u)]] if (chart, u) in IDUSER_VARIANTS else [])(_u),
    }
for _svc in MEMCACHE:
    RULES["memcache-%s" % _svc] = {
        "what": "is %s (its keystonemiddleware memcache secret key)" % _svc,
        "consumes": (lambda s: lambda d, has: d == s)(_svc),
        "password_path": lambda chart: "endpoints.oslo_cache.auth.memcache_secret_key",
        "projections": (lambda s: lambda chart, cv: ["%s-memcache-secret-key" % s])(_svc),
    }

# Per-service MariaDB users (0.3.68): one item per USERNAME (id oslo-db-<username>). Which charts render which username
# under which endpoints.<family>.auth keys is the committed map hack/db-users.yaml, re-verified by check 15b.
DBUSERS = (yaml.safe_load(open(os.path.join(ROOT, "hack", "db-users.yaml"))) or {}).get("users") or {}
DBUSER_BY_ITEM = {"oslo-db-" + u.replace("_", "-"): u for u in DBUSERS}
# The URL drop-in's name and file (helm-toolkit.manifests.db_dropin); charts listing their entries in
# templates/_db_dropin_entries.tpl use it. horizon reads its password from a file (check 14), skyline's skyline.yaml is
# relocated whole into skyline-config (16b), powerdns reads it from powerdns-db-user only.
DB_DROPIN_FILE = "87-db-connection.conf"
DB_FILEREAD = {
    "horizon": {"secret": "horizon-db-password", "dir": "/etc/openstack-dashboard/krateo-db-password", "file": "password",
                "carrier": ("horizon-etc", "local_settings"), "match": "'PASSWORD':",
                "line": "'PASSWORD': open('/etc/openstack-dashboard/krateo-db-password/password', encoding='utf-8')"
                        ".read().strip(),  # generated per install (#228)"},
}
# skyline.yaml, which holds skyline's database_url, is rendered whole by skyline-config (check 16b compares it); powerdns
# reads its password only from powerdns-db-user (DB_PASSWORD), a projection (checks 3, 7, 8).
DB_RELOCATED = {"skyline"}
DB_PROJECTION_ONLY = {"powerdns"}
DB_DROPIN_CHECKED = []
# Charts whose config is read by programs that never use the database: only the listed containers receive the URLs.
DB_READERS = (yaml.safe_load(open(os.path.join(ROOT, "hack", "db-readers.yaml"))) or {}).get("charts") or {}
DB_READERS_SEEN = set()


# Modes whose workloads read a chart's config only there: neutron's OVN agents and its ovn-db-sync CronJob, a database
# reader (hack/db-readers.yaml).
DB_VARIANTS = {
    "neutron": [{"network": {"backend": ["ovn"]},
                 "manifests": {"daemonset_ovn_agent": True, "daemonset_ovn_metadata_agent": True, "cron_job_ovn_db_sync": True}}],
}


def db_keys(chart, u):
    """[(family, auth key)] user u renders under in chart (hack/db-users.yaml)."""
    return [tuple(k.split(".", 1)) for k in (DBUSERS.get(u) or {}).get(chart, [])]


def db_dropin_for(chart):
    f = os.path.join(BP, chart, "chart", "templates", "_db_dropin_entries.tpl")
    return ("%s-db-connection" % chart, DB_DROPIN_FILE) if os.path.exists(f) else None


for _cid, _u in DBUSER_BY_ITEM.items():
    RULES[_cid] = {
        "what": "renders MariaDB user %s" % _u,
        "consumes": (lambda u: lambda d, has: d in DBUSERS[u])(_u),
        "password_path": (lambda u: lambda chart: ["endpoints.%s.auth.%s.password" % k for k in db_keys(chart, u)]
                          or ["endpoints.oslo_db.auth.gate_unmapped.password"])(_u),
        # <chart>-db-user and its siblings (DB_CONNECTION): the secrets.<family>.<key> of each key the user renders under
        "projections": (lambda u: lambda chart, cv: [((cv.get("secrets") or {}).get(fam) or {}).get(key)
                                                     for fam, key in db_keys(chart, u)])(_u),
        "db": True,
        # db-drop is off by default and reads the main config: render it on, or its readers pass unchecked. So are the
        # workloads IDUSER_VARIANTS turns on (nova's ironic compute, masakari's monitors, neutron's ironic agent): their
        # readers must receive the URLs, and those that read none of the configs (masakari's monitors read
        # masakarimonitors.conf, nova's computes nova-compute.conf, which omits the databases) must not.
        "variants": lambda chart: ([{"manifests": {"job_db_drop": True}}]
                                   if os.path.exists(os.path.join(BP, chart, "chart", "templates", "job-db-drop.yaml"))
                                   else []) + [v for (ch, _), v in sorted(IDUSER_VARIANTS.items()) if ch == chart]
                                  + DB_VARIANTS.get(chart, []),
    }
    if any(c in DB_FILEREAD for c in DBUSERS[_u]):
        RULES[_cid]["fileread"] = lambda chart: DB_FILEREAD[chart]

# Per-service RabbitMQ users (0.3.69): one item per USERNAME (id oslo-messaging-<username>). Which charts render which
# username under which endpoints.oslo_messaging.auth key is the committed map hack/mq-users.yaml, re-verified by check 15c.
_MQF = os.path.join(ROOT, "hack", "mq-users.yaml")
MQUSERS = ((yaml.safe_load(open(_MQF)) or {}).get("users") or {}) if os.path.isfile(_MQF) else {}
MQUSER_BY_ITEM = {"oslo-messaging-" + u.replace("_", "-"): u for u in MQUSERS}
# The URL drop-in's name and file (helm-toolkit.manifests.transport_dropin, templates/_transport_dropin_entries.tpl).
MQ_DROPIN_FILE = "88-transport.conf"
# Which containers send or receive over RabbitMQ (or would under a setting), measured per program: only they receive the
# drop-in, and every other container that mounts that config must not (check 22m).
_MQR = os.path.join(ROOT, "hack", "mq-readers.yaml")
MQ_READERS = ((yaml.safe_load(open(_MQR)) or {}).get("charts") or {}) if os.path.isfile(_MQR) else {}
MQ_READERS_SEEN = set()
MQ_DROPIN_CHECKED = []
MQ_PROBES_CHECKED = []
# The keys of endpoints.oslo_messaging.auth that are not a per-service user: the administrator (auth.admin, and the
# rabbitmq chart's own auth.user), the retired guest and the Erlang cookie.
MQ_NOT_SERVICE = ("admin", "user", "guest", "erlang_cookie")


def mq_key(chart, u):
    """The endpoints.oslo_messaging.auth key RabbitMQ user u renders under in chart (hack/mq-users.yaml), or None."""
    return (MQUSERS.get(u) or {}).get(chart)


def all_workloads(chart):
    """manifests.<x>: true for every workload a chart ships switched off (DaemonSets, Deployments, StatefulSets, Jobs,
    CronJobs: cron_job_* and cinder's and manila's cron_*; masakari's *_monitor DaemonSets): a reader that only an optional
    workload runs must be checked too, and so must a non-reader."""
    man = (yaml.safe_load(open(os.path.join(BP, chart, "chart", "values.yaml"))) or {}).get("manifests") or {}
    on = {k: True for k, v in man.items() if v is False and (k.startswith(("daemonset_", "deployment_", "statefulset_",
                                                                             "cron_", "job_")) or k.endswith("_monitor"))
          and k != "job_image_repo_sync"}
    return {"manifests": on} if on else None


# Workloads a manifest alone does not switch on: nova's console proxies follow console.console_kind, neutron's SR-IOV
# agent network.backend.
MQ_VARIANTS = {
    "nova": [{"console": {"console_kind": "spice"}, "manifests": {"deployment_spiceproxy": True}},
             {"console": {"console_kind": "serial"}, "manifests": {"deployment_serialproxy": True}}],
    "neutron": [{"network": {"backend": ["openvswitch", "sriov"]}, "manifests": {"daemonset_sriov_agent": True}}],
}

for _cid, _u in MQUSER_BY_ITEM.items():
    RULES[_cid] = {
        "what": "renders RabbitMQ user %s" % _u,
        "consumes": (lambda u: lambda d, has: d in MQUSERS[u])(_u),
        "password_path": (lambda u: lambda chart: "endpoints.oslo_messaging.auth.%s.password"
                          % (mq_key(chart, u) or "gate_unmapped"))(_u),
        # <chart>-rabbitmq-user (RABBITMQ_CONNECTION, read by rabbit-init): secrets.oslo_messaging.<key>
        "projections": (lambda u: lambda chart, cv: [((cv.get("secrets") or {}).get("oslo_messaging") or {})
                                                     .get(mq_key(chart, u) or "gate_unmapped")])(_u),
        # keystone ships secret_rabbitmq off; render it on, or its projection would pass vacuously
        "enable": lambda chart: {"manifests": {"secret_rabbitmq": True}},
        "mq": True,
        "variants": lambda chart: [v for v in [all_workloads(chart)] if v] + MQ_VARIANTS.get(chart, []),
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
    if content is not None and tdata != {fname: content}:
        fail("%s: the drop-in %s holds %s, want %s" % (chart, drop, sorted(tdata.items()), [(fname, content)]))
    if content is None:
        # One drop-in per chart holding every rotated user's options: each (section, option) where THIS user's
        # sentinel sat in values mode must be in the drop-in as option = {{ .pw_<key> }}, and pw_<key> must read
        # this user's root.
        u = IDUSER_BY_ITEM[cid]
        key = IDUSERS[u].get(chart, "gate_unmapped")
        want = set()
        converted = {d["metadata"]["name"] for d in docs if d.get("kind") == "ExternalSecret"}
        kept = {(d["kind"], d["metadata"]["name"], k) for d in docs if d.get("kind") in ("Secret", "ConfigMap")
                for k in (d.get("data") or {})}
        for kind, name, k in carriers:
            if name in converted or (kind, name, k) not in kept:
                continue  # projected whole by its own ExternalSecret (<chart>-ks-etc, glance-swift-store), not the drop-in
            for d in docs_s:
                if d.get("kind") == kind and d["metadata"]["name"] == name:
                    want |= ini_sentinel_options(object_text(d, k) or "", sentinel)
        if not want:
            fail("%s: no ini option carried %s in values mode, so its drop-in lines cannot be determined" % (chart, cid))
        got = {}
        section = None
        for line in (tdata.get(fname) or "").splitlines():
            t = line.strip()
            if t.startswith("[") and t.endswith("]"):
                section = t[1:-1]
            elif "=" in t:
                o, _, val = t.partition("=")
                got[(section, o.strip())] = val.strip()
        for so in sorted(want, key=str):
            if got.get(so) != "{{ .pw_%s }}" % key:
                fail("%s: drop-in %s has %s = %r for %s, want {{ .pw_%s }}" % (chart, drop, so, got.get(so), cid, key))
        root = [i["secret"] for i in ITEMS_REF.values() if i["id"] == cid]
        data = {e.get("secretKey"): (e.get("remoteRef") or {}).get("key") for e in (es[0]["spec"].get("data") or [])} if es else {}
        if not root or data.get("pw_%s" % key) != root[0]:
            fail("%s: drop-in %s reads pw_%s from %s, not %s's root" % (chart, drop, key, data.get("pw_%s" % key), cid))
    readers = 0
    for podname, pod in pod_specs(docs):
        # Every workload kind, Jobs and CronJobs included: unlike the memcache key (keystonemiddleware, servers only),
        # a service password is used by one-shot programs too. 0.3.65's ceilometer-db-sync (ceilometer-upgrade talks
        # to gnocchi) read ceilometer.conf without the drop-in and failed its post-upgrade hook on every retry.
        # Helm test Pods are left out: they run on demand, not in an install or an upgrade.
        if podname.split("/")[0] not in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"):
            continue
        vols = {v["name"]: v for v in pod.get("volumes") or []}
        # init containers that start an OpenStack program read the config too (#291: designate-worker-init)
        inits = [c for c in pod.get("initContainers") or [] if openstack_programs(pod, c, docs)]
        for cont in (pod.get("containers") or []) + inits:
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
                    if d.endswith(".conf.d"):
                        ok = True
            if not ok:
                fail("%s: %s container %s reads %s but does not receive %s where it reads drop-ins"
                     % (chart, podname, cont["name"], sorted(reads), drop))
    if readers == 0:
        fail("%s: no container reads a file that carried %s" % (chart, cid))


def fileread_check(chart, cid, docs):
    """14. A secret that a config line reads from a mounted file: the carrier renders exactly that line,
    once, and every pod template that mounts the carrier (Jobs included: a db-sync imports it too) mounts
    the Secret at the directory the line reads; the ExternalSecret holds exactly the file."""
    f = RULES[cid]["fileread"](chart)
    cname, ckey = f["carrier"]
    carrier = [d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"] == cname]
    text = object_text(carrier[0], ckey) if carrier else None
    if text is None:
        fail("%s: %s/%s is not rendered with the reference" % (chart, cname, ckey))
        return
    # what assigns the value: `VAR =` (SECRET_KEY), or a dict key such as horizon's DATABASES 'PASSWORD':
    var = f.get("match") or f["line"].split("=", 1)[0].strip() + "="
    assigns = [l.strip() for l in text.splitlines() if l.replace(" ", "").startswith(var.replace(" ", ""))]
    if assigns != [f["line"]]:
        fail("%s: %s/%s assigns %s as %s, want exactly [%r]" % (chart, cname, ckey, var, assigns, f["line"]))
    es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == f["secret"]]
    tdata = (((es[0].get("spec") or {}).get("target") or {}).get("template") or {}).get("data") or {} if es else {}
    if tdata != {f["file"]: "{{ .password }}"}:
        fail("%s: %s holds %s, want {%r: '{{ .password }}'}" % (chart, f["secret"], sorted(tdata), f["file"]))
    readers = 0
    for podname, pod in pod_specs(docs):
        vols = {v["name"]: v for v in pod.get("volumes") or []}
        for cont in (pod.get("containers") or []) + (pod.get("initContainers") or []):
            mounts = cont.get("volumeMounts") or []
            reads = any((vols.get(m["name"]) or {}).get("secret", {}).get("secretName") == cname
                        and m.get("subPath") in (None, ckey) for m in mounts)
            if not reads:
                continue
            readers += 1
            ok = any((vols.get(m["name"]) or {}).get("secret", {}).get("secretName") == f["secret"]
                     and not m.get("subPath") and m["mountPath"].rstrip("/") == f["dir"] for m in mounts)
            if not ok:
                fail("%s: %s container %s reads %s/%s but does not mount %s at %s, so the line would fail"
                     % (chart, podname, cont["name"], cname, ckey, f["secret"], f["dir"]))
    if readers == 0:
        fail("%s: no container reads %s/%s" % (chart, cname, ckey))


def identity_carriers(chart, cid, sentinel, docs_s, docs):
    """16. Every Secret that carried a service user's password in values mode is, with the reference, an
    ExternalSecret reading that user's root -- or a config whose copy was stripped, which then needs that
    chart's drop-in for the user (a stripped password with nothing delivering it is a broken service)."""
    root = None
    for i in ITEMS_REF.values():
        if i["id"] == cid:
            root = i["secret"]
    carriers = sorted({d["metadata"]["name"] for d in docs_s if d.get("kind") == "Secret"
                       and any(sentinel in (object_text(d, k) or "") for k in (d.get("data") or {}))})
    if not carriers:
        fail("%s: no Secret carries %s in values mode" % (chart, cid))
    kept = {(d["metadata"]["name"], k) for d in docs if d.get("kind") == "Secret" for k in (d.get("data") or {})}
    for name in carriers:
        keys_s = [k for d in docs_s if d.get("kind") == "Secret" and d["metadata"]["name"] == name
                  for k in (d.get("data") or {}) if sentinel in (object_text(d, k) or "")]
        if not any((name, k) in kept for k in keys_s) and \
                not any(d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == name for d in docs):
            continue  # every key that carried it moved out of this Secret (a relocated file): check 16b
        es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == name]
        if es:
            keys = [(e.get("remoteRef") or {}).get("key") for e in es[0]["spec"].get("data") or []]
            if root not in keys:
                fail("%s: ExternalSecret %s carried %s but reads %s, not %s" % (chart, name, cid, keys, root))
        elif not RULES[cid]["dropin_for"](chart):
            fail("%s: %s carried %s in values mode and is still a plain Secret with no drop-in for it" % (chart, name, cid))


def relocated_files(chart, cid, sentinel, docs_s, docs, others=None):
    """16b. A file that carried a service user's password in values mode and that the references render drops
    from its Secret (glance's swift-store.conf) must be rendered by an ExternalSecret reading that user's root,
    byte for byte the values-mode file once the placeholder holds the sentinel; and every workload that mounted
    the old key mounts the ExternalSecret's file at the same path. `others` maps the root of every other credential
    the chart receives to the value values mode renders for it, for a file that holds two (skyline.yaml)."""
    others = others or {}
    root = [i["secret"] for i in ITEMS_REF.values() if i["id"] == cid]
    kept = {(d["kind"], d["metadata"]["name"], k) for d in docs if d.get("kind") in ("Secret", "ConfigMap")
            for k in (d.get("data") or {})}
    converted = {d["metadata"]["name"] for d in docs if d.get("kind") == "ExternalSecret"}
    for d in docs_s:
        if d.get("kind") not in ("Secret", "ConfigMap") or d["metadata"]["name"] in converted:
            continue
        for k in (d.get("data") or {}):
            text = object_text(d, k) or ""
            if sentinel not in text or (d["kind"], d["metadata"]["name"], k) in kept:
                continue
            found = None
            for e in docs:
                if e.get("kind") != "ExternalSecret":
                    continue
                tdata = (((e.get("spec") or {}).get("target") or {}).get("template") or {}).get("data") or {}
                if k not in tdata:
                    continue
                keys = {x.get("secretKey"): (x.get("remoteRef") or {}).get("key") for x in e["spec"].get("data") or []}
                got = tdata[k]
                for sk, rk in keys.items():
                    if root and rk == root[0]:
                        got = got.replace("{{ .%s }}" % sk, sentinel).replace("{{ .%s | toJson }}" % sk, json.dumps(sentinel))
                        got = got.replace("{{ .%s | urlquery }}" % sk, urllib.parse.quote_plus(sentinel))
                    elif rk in others:
                        # another credential of the same file (skyline.yaml: the Keystone user and the MariaDB user):
                        # its placeholder holds what values mode renders for it
                        got = got.replace("{{ .%s }}" % sk, others[rk]).replace("{{ .%s | toJson }}" % sk, json.dumps(others[rk]))
                        got = got.replace("{{ .%s | urlquery }}" % sk, urllib.parse.quote_plus(others[rk]))
                same = got == text
                if not same and k.endswith((".yaml", ".yml")):
                    try:
                        same = yaml.safe_load(got) == yaml.safe_load(text)  # toJson quotes the scalar; same document
                    except yaml.YAMLError:
                        same = False
                if same:
                    found = e
            if not found:
                fail("%s: %s/%s key %s carried %s in values mode and is gone from it, but no ExternalSecret reading "
                     "its root renders the same file" % (chart, d["kind"], d["metadata"]["name"], k, cid))
                continue
            target = ((found["spec"].get("target") or {}).get("name")) or found["metadata"]["name"]
            olds = {}
            for podname, pod in pod_specs(docs_s):
                vols = {v["name"]: (v.get("secret") or {}).get("secretName") or (v.get("configMap") or {}).get("name")
                        for v in pod.get("volumes") or []}
                for c in pod.get("containers") or []:
                    for m in c.get("volumeMounts") or []:
                        if vols.get(m["name"]) == d["metadata"]["name"] and m.get("subPath") == k:
                            olds[(podname, c["name"])] = m["mountPath"]
            if not olds:
                fail("%s: nothing mounted %s key %s in values mode" % (chart, d["metadata"]["name"], k))
            news = {}
            for podname, pod in pod_specs(docs):
                vols = {v["name"]: (v.get("secret") or {}).get("secretName") for v in pod.get("volumes") or []}
                for c in pod.get("containers") or []:
                    for m in c.get("volumeMounts") or []:
                        if vols.get(m["name"]) == target and m.get("subPath") == k:
                            news[(podname, c["name"])] = m["mountPath"]
            for pc, path in sorted(olds.items()):
                if news.get(pc) != path:
                    fail("%s: %s container %s mounted %s at %s in values mode; with the reference it must mount %s's "
                         "file there (has %s)" % (chart, pc[0], pc[1], k, path, target, news.get(pc)))


def openstack_programs(pod, cont, docs):
    """The OpenStack programs a container's scripts start: from a shell line (OSLO_PROGRAM), or as the first element of a
    Python subprocess/exec argv (keystone's fernet-manage.py runs subprocess.call(['keystone-manage', ...]))."""
    progs = set()
    for text in container_scripts(pod, cont, docs):
        logical = re.sub(r"\\\n\s*", " ", text)
        for line in logical.splitlines():
            if line.lstrip().startswith("#"):
                continue
            code = line.split("#", 1)[0]
            progs |= set(OSLO_PROGRAM.findall(code))
            progs |= set(PY_PROGRAM.findall(code))
    return progs


def conf_readers(carriers, docs, kinds=("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")):
    """[(podname, container, mounts, vols, keys read, init)] for every main container of a pod of `kinds`, and every init
    container that starts an OpenStack program, with the carrier files it mounts (possibly none). An init container that
    starts none (kubernetes-entrypoint, a shell that writes a file) reads no option of the config. Until 0.3.70 init
    containers were left out on the premise that they share their pod's drop-in volume: designate-worker-init mounts
    designate.conf alone and runs designate-manage pool update, so it had no transport_url and never finished (#291)."""
    out = []
    for podname, pod in pod_specs(docs):
        if podname.split("/")[0] not in kinds:
            continue
        vols = {v["name"]: v for v in pod.get("volumes") or []}
        inits = [c for c in pod.get("initContainers") or [] if openstack_programs(pod, c, docs)]
        for cont in (pod.get("containers") or []) + inits:
            mounts = cont.get("volumeMounts") or []
            reads = set()
            for m in mounts:
                v = vols.get(m["name"]) or {}
                obj = (("Secret", (v.get("secret") or {}).get("secretName")) if "secret" in v else
                       ("ConfigMap", (v.get("configMap") or {}).get("name")) if "configMap" in v else (None, None))
                for kind, name, key in carriers:
                    if (kind, name) == obj and m.get("subPath") in (None, key):
                        reads.add(key)
            out.append((podname, cont["name"], mounts, vols, reads, cont in inits))
    return out


def pod_shares_dropin(rows, podname, drop, listed):
    """True when a listed main container of the same pod receives Secret `drop` too: an init container then reads the
    pod's own drop-in volume, on the same node, and the credential reaches no new place."""
    return any(p == podname and not init and c in listed and receives_in_confd(m, v, drop)
               for p, c, m, v, r, init in rows)


def receives_in_confd(mounts, vols, secret):
    """True when one of the container's mounts puts Secret `secret` (whole, or a projected source) in a .conf.d."""
    for m in mounts:
        v = vols.get(m["name"]) or {}
        srcs = [(v.get("secret") or {}).get("secretName")] if "secret" in v else \
               [(x.get("secret") or {}).get("name") for x in (v.get("projected") or {}).get("sources") or []]
        if secret in srcs:
            path = m["mountPath"].rstrip("/")
            if (path if not m.get("subPath") else os.path.dirname(path)).endswith(".conf.d"):
                return True
    return False


def ini_options(text):
    """{(section, option): value} of an ini text (a repeated option keeps its last value, as oslo.config does)."""
    out, section = {}, None
    for line in (text or "").splitlines():
        t = line.strip()
        if t.startswith("[") and t.endswith("]"):
            section = t[1:-1]
        elif "=" in t and not t.startswith(("#", ";")):
            k, _, val = t.partition("=")
            out[(section, k.strip())] = val.strip()
    return out


def db_dropin_check(chart, cid, sentinel, docs_s, docs, label=""):
    """22. A per-service MariaDB user's URLs, with its root: every ini option of a config that carried the user's password
    in values mode (sentinel render docs_s) is gone from that config and is in the drop-in, as exactly the values-mode URL
    with the password read from this user's root; every container mounting a carrier receives the drop-in in a .conf.d,
    and no container reading none of the carriers receives it."""
    drop, fname = db_dropin_for(chart)
    root = [i["secret"] for i in ITEMS_REF.values() if i["id"] == cid]
    projections = set(RULES[cid]["projections"](chart, yaml.safe_load(open(os.path.join(BP, chart, "chart", "values.yaml")))))
    carriers, want = set(), {}
    for d in docs_s:
        if d.get("kind") not in ("Secret", "ConfigMap") or d["metadata"]["name"] in projections:
            continue  # <chart>-db-user and its siblings (DB_CONNECTION, not ini): checks 3 and 8
        for k in (d.get("data") or {}):
            for so, val in ini_options(object_text(d, k)).items():
                if sentinel in val:
                    carriers.add((d["kind"], d["metadata"]["name"], k))
                    if want.get(so, val) != val:
                        fail("22%s: %s: %s carries %s's URL with two values in values mode (%s/%s)" % (
                            label, chart, so, cid, d["metadata"]["name"], k))
                    want[so] = val
    if not carriers:
        fail("22%s: %s: no rendered config carries %s's password in an ini option in values mode, so where the drop-in "
             "must arrive cannot be determined" % (label, chart, cid))
        return
    byname = {(d.get("kind"), d["metadata"]["name"]): d for d in docs}
    for kind, name, k in sorted(carriers):
        d = byname.get((kind, name))
        left = {so for so, v in ini_options(object_text(d, k) if d else "").items() if so in want}
        if left:
            fail("22%s: %s: %s/%s %s still sets %s with %s's reference: the base config must not render the URL"
                 % (label, chart, kind, name, k, sorted(left, key=str), cid))
    es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == drop]
    if len(es) != 1:
        fail("22%s: %s: renders %d ExternalSecret(s) %s, want 1 (%s)" % (label, chart, len(es), drop, cid))
        return
    tdata = ((es[0]["spec"].get("target") or {}).get("template") or {}).get("data") or {}
    if sorted(tdata) != [fname]:
        fail("22%s: %s: %s holds %s, want exactly %s" % (label, chart, drop, sorted(tdata), fname))
        return
    keys = {e.get("secretKey"): (e.get("remoteRef") or {}).get("key") for e in es[0]["spec"].get("data") or []}
    got = ini_options(tdata[fname])
    for so, val in sorted(want.items(), key=str):
        g = got.get(so)
        if g is None:
            fail("22%s: %s: drop-in %s has no %s for %s" % (label, chart, drop, so, cid))
            continue
        used = re.findall(r"\{\{ \.(\w+) \| urlquery \}\}", g)
        filled = g
        for sk in used:
            if not root or keys.get(sk) != root[0]:
                fail("22%s: %s: drop-in %s fills %s from %s (%s), not %s's root" % (label, chart, drop, so, sk,
                                                                                  keys.get(sk), cid))
            filled = filled.replace("{{ .%s | urlquery }}" % sk, urllib.parse.quote_plus(sentinel))
        if len(used) != 1 or filled != val:
            fail("22%s: %s: drop-in %s %s is not the values-mode URL with the password from the root (%d placeholder(s); "
                 "same once filled: %s)" % (label, chart, drop, so, len(used), filled == val))
    readers = 0
    users = (DB_READERS.get(chart) or {}).get("readers")
    rows = conf_readers(carriers, docs)
    for podname, cname, mounts, vols, reads, init in rows:
        gets = receives_in_confd(mounts, vols, drop)
        if reads and users is not None and cname not in users:
            # hack/db-readers.yaml: this chart's config is read by programs that never use the database
            if gets and not (init and pod_shares_dropin(rows, podname, drop, users)):
                fail("22%s: %s: %s container %s receives %s, but hack/db-readers.yaml does not list it as using the "
                     "database: the password has no business there" % (label, chart, podname, cname, drop))
            continue
        if reads:
            if users is not None:
                DB_READERS_SEEN.add((chart, cname))
            readers += 1
            if not gets:
                fail("22%s: %s: %s container %s reads %s but does not receive %s in a .conf.d directory: it would have no "
                     "database URL" % (label, chart, podname, cname, sorted(reads), drop))
        elif gets:
            fail("22%s: %s: %s container %s receives %s but reads no config that carried %s's URL: the database password "
                 "has no business there" % (label, chart, podname, cname, drop, cid))
    if readers == 0:
        fail("22%s: %s: no container reads a config that carried %s's URL" % (label, chart, cid))
    else:
        DB_DROPIN_CHECKED.append((chart, cid, label, readers))


DBINIT_CHECKED = []
# A stand-in for SQLAlchemy, enough for helm-toolkit's db-init.py and db-drop.py: URLs parse as SQLAlchemy does (the
# password URL-unquoted), and every statement executed is recorded.
SQLA_STUB = '''
import json, os, urllib.parse
LOG = os.environ["GATE_SQL_LOG"]
class _URL:
    def __init__(self, s):
        u = urllib.parse.urlsplit(s)
        self.drivername, self.username = u.scheme, urllib.parse.unquote(u.username or "")
        self.password = urllib.parse.unquote(u.password or "")
        self.host, self.port, self.database = u.hostname, u.port, u.path.lstrip("/") or None
class _Conn:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, stmt):
        with open(LOG, "a") as f: f.write(json.dumps(str(stmt)) + "\\n")
    def commit(self): pass
    def close(self): pass
class _Engine:
    def __init__(self, url, **kw): self.url = _URL(url)
    def connect(self): return _Conn()
def create_engine(url, **kw): return _Engine(url)
def text(s): return s
'''


def dbinit_check(chart, docs, tmp):
    """23. Run the chart's rendered db-init.py and db-drop.py with the user's URL only in <config>.d (as a referenced
    render has it): db-init must CREATE USER IF NOT EXISTS and then ALTER USER ... IDENTIFIED BY that password, and
    db-drop must drop that user."""
    scripts = {}
    for d in docs:
        if d.get("kind") in ("ConfigMap", "Secret"):
            for k in ("db-init.py", "db-drop.py"):
                if k in (d.get("data") or {}):
                    scripts[k] = object_text(d, k)
    if "db-init.py" not in scripts:
        return
    work = tempfile.mkdtemp(dir=tmp, prefix="dbinit-%s-" % chart)
    os.makedirs(os.path.join(work, "sqlalchemy"))
    open(os.path.join(work, "sqlalchemy", "__init__.py"), "w").write(SQLA_STUB)
    conf = os.path.join(work, "etc", "svc.conf")
    os.makedirs(conf + ".d")
    # the base file without the URL (a placeholder value that must be overridden), the drop-in with it, as rendered
    open(conf, "w").write("[database]\nconnection = mysql+pymysql://stale:Stale1@db:3306/stale\n")
    pw = "Gate%sAlter7f3c" % chart.replace("-", "")
    open(os.path.join(conf + ".d", DB_DROPIN_FILE), "w").write(
        "[database]\nconnection = mysql+pymysql://gateuser:%s@mariadb:3306/gatedb\n" % pw)
    for name in ("db-init.py", "db-drop.py"):
        if name not in scripts:
            continue
        sp = os.path.join(work, name)
        open(sp, "w").write(scripts[name])
        log = os.path.join(work, name + ".log")
        env = dict(os.environ, PYTHONPATH=work, GATE_SQL_LOG=log, OPENSTACK_CONFIG_FILE=conf,
                   OPENSTACK_CONFIG_DB_SECTION="database", OPENSTACK_CONFIG_DB_KEY="connection",
                   ROOT_DB_CONNECTION="mysql+pymysql://root:GateRoot7f3c@mariadb:3306")
        env.pop("MARIADB_X509", None)
        r = subprocess.run([sys.executable, sp], env=env, capture_output=True, text=True, cwd=work)
        stmts = [json.loads(l) for l in open(log)] if os.path.exists(log) else []
        if r.returncode != 0:
            fail("23: %s: %s did not run with the URL in %s.d (rc %d): %s" % (chart, name, os.path.basename(conf),
                                                                             r.returncode, r.stderr.strip()[-300:]))
            continue
        norm = [re.sub(r"\s+", " ", s).strip() for s in stmts]
        if name == "db-init.py":
            create = [i for i, s in enumerate(norm) if s.startswith("CREATE USER IF NOT EXISTS 'gateuser'@'%%' IDENTIFIED BY '%s'" % pw)]
            alter = [i for i, s in enumerate(norm) if s == "ALTER USER 'gateuser'@'%%' IDENTIFIED BY '%s'" % pw]
            if not create:
                fail("23: %s: db-init.py did not create user gateuser from the drop-in's URL (ran %s)" % (chart, norm))
            elif not alter or alter[0] < create[0]:
                fail("23: %s: db-init.py creates the user but never sets its password afterwards (ALTER USER ... "
                     "IDENTIFIED BY): CREATE USER IF NOT EXISTS leaves an existing user's password unchanged, so a "
                     "generated password never reaches MariaDB" % chart)
            else:
                DBINIT_CHECKED.append((chart, name))
        elif not any(s == "DROP USER IF EXISTS gateuser" for s in norm):
            fail("23: %s: db-drop.py did not drop the user named in the drop-in's URL (ran %s)" % (chart, norm))
        else:
            DBINIT_CHECKED.append((chart, name))


CELLMAP_CHECKED = []
# nova formats a templated cell mapping URL from the process's own config (CellMapping._format_url, nova stable/2026.1).
CELL_CMD = re.compile(r"cell_v2\s+(map_cell0|create_cell|update_cell)\b(.*)")


def nova_format_url(template, default):
    """CellMapping._format_url: the template formatted from a local URL."""
    u = urllib.parse.urlparse(default)
    return template.format(username=u.username, password=u.password, hostname=u.hostname, port=u.port, scheme=u.scheme,
                           query=u.query, fragment=u.fragment, path=u.path.lstrip("/"))


def cell_mapping_check(docs_s, docs, sentinel, label=""):
    """24. nova's db-sync writes only TEMPLATES into nova_api.cell_mappings: every cell_v2 map_cell0, create_cell and
    update_cell passes --database_connection (and, but for cell0, --transport-url) as a quoted literal that holds
    {password} and no shell variable, so no password is stored and no process caches a stale one (nova keeps each cell's
    connections per process, keyed by cell UUID, until it exits). Formatted from the values-mode nova.conf, the
    templates are exactly its [database] connection, [cell0_database] connection and [DEFAULT] transport_url. 24b:
    every container that loads cell mappings (it mounts the API's nova.conf: computes mount nova-compute.conf and never
    read the API database) has a [database] connection and a [DEFAULT] transport_url to format them from, from its base
    config or a drop-in it receives."""
    script = None
    for d in docs:
        if d.get("kind") == "ConfigMap" and "db-sync.sh" in (d.get("data") or {}) and d["metadata"]["name"] == "nova-bin":
            script = d["data"]["db-sync.sh"]
    if not script:
        fail("24%s: nova: no db-sync.sh in nova-bin, so what it writes into the cell mappings cannot be checked" % label)
        return
    conf = None
    for d in docs_s:
        if d.get("kind") == "Secret" and d["metadata"]["name"] == "nova-etc":
            conf = ini_options(object_text(d, "nova.conf"))
    want = {"cell1": conf.get(("database", "connection")), "cell0": conf.get(("cell0_database", "connection")),
            "transport": conf.get(("DEFAULT", "transport_url"))} if conf else {}
    if not all(want.values()):
        fail("24%s: nova: the values-mode nova.conf lacks %s" % (label, sorted(k for k, v in want.items() if not v)))
        return
    logical = re.sub(r"\\\n\s*", " ", script)
    cmds = 0
    for line in logical.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = CELL_CMD.search(line)
        if not m:
            continue
        cmds += 1
        verb, rest = m.group(1), m.group(2)
        args = dict(re.findall(r"--(database_connection|transport-url)[ =]('[^']*'|\"[^\"]*\"|\S+)", rest))
        cell0 = verb == "map_cell0" or "cell0" in rest
        for opt in ("database_connection",) + (() if cell0 else ("transport-url",)):
            v = args.get(opt)
            if v is None:
                fail("24%s: nova: db-sync's cell_v2 %s passes no --%s, so nova-manage writes the literal URL from its "
                     "config, password included" % (label, verb, opt))
                continue
            if not v.startswith("'") or "$" in v or "{password}" not in v:
                fail("24%s: nova: db-sync's cell_v2 %s --%s %s is not a quoted template holding {password}: the cell "
                     "mapping would store a password" % (label, verb, opt, v[:60]))
                continue
            got = nova_format_url(v.strip("'"), want["transport"] if opt == "transport-url" else want["cell1"])
            exp = want["transport"] if opt == "transport-url" else want["cell0" if cell0 else "cell1"]
            if got != exp:
                fail("24%s: nova: db-sync's cell_v2 %s --%s template does not format to the URL nova.conf names for %s"
                     % (label, verb, opt, "cell0" if cell0 else "cell1"))
        if cell0 and verb != "map_cell0" and args.get("transport-url", "").strip("'\"") != "none:///":
            fail("24%s: nova: db-sync's cell0 update sets a transport URL other than none:///" % label)
    if cmds == 0:
        fail("24%s: nova: db-sync.sh runs no cell_v2 map_cell0/create_cell/update_cell; the check had no input" % label)
    # 24b. every mapping loader can format the templates
    loaders = 0
    texts = {}
    for d in docs:
        if d.get("kind") in ("Secret", "ConfigMap"):
            for k in (d.get("data") or {}):
                texts[(d["metadata"]["name"], k)] = object_text(d, k)
        elif d.get("kind") == "ExternalSecret":
            tgt = ((d["spec"].get("target") or {}).get("name")) or d["metadata"]["name"]
            for k, v in (((d["spec"].get("target") or {}).get("template") or {}).get("data") or {}).items():
                texts[(tgt, k)] = v
    for podname, pod in pod_specs(docs):
        if podname.startswith("Pod/"):
            continue
        vols = {v["name"]: v for v in pod.get("volumes") or []}
        for cont in pod.get("containers") or []:
            base, dropins = None, []
            for mnt in cont.get("volumeMounts") or []:
                v = vols.get(mnt["name"]) or {}
                if mnt["mountPath"] == "/etc/nova/nova.conf" and "secret" in v:
                    base = (v["secret"]["secretName"], mnt.get("subPath") or "nova.conf")
                elif mnt["mountPath"].rstrip("/") == "/etc/nova/nova.conf.d" or \
                        os.path.dirname(mnt["mountPath"]) == "/etc/nova/nova.conf.d":
                    srcs = [v["secret"]["secretName"]] if "secret" in v else \
                           [(x.get("secret") or {}).get("name") for x in (v.get("projected") or {}).get("sources") or []]
                    for s in srcs:
                        dropins += [t for (n, k), t in sorted(texts.items()) if n == s and k.endswith(".conf")]
            if base is None or base[1] == "nova-compute.conf":
                continue
            # helm-toolkit's db-init.py and db-drop.py (MariaDB only) and the bootstrap Job (the openstack client) mount
            # nova.conf too and run no nova code, so they load no cell mapping (0.3.69: they get no transport URL)
            cmd = " ".join((cont.get("command") or []) + (cont.get("args") or []))
            if re.search(r"/tmp/db-(init|drop)\.py$", cmd) or podname == "Job/nova-bootstrap":
                continue
            loaders += 1
            eff = ini_options(texts.get(base) or "")
            for t in dropins:
                eff.update(ini_options(t))
            for so in (("database", "connection"), ("DEFAULT", "transport_url")):
                if not eff.get(so):
                    fail("24b%s: nova: %s container %s loads cell mappings but has no [%s] %s to format their templates "
                         "from" % (label, podname, cont["name"], so[0], so[1]))
    if loaders == 0:
        fail("24b%s: nova: no container loads cell mappings; the check had no input" % label)
    else:
        CELLMAP_CHECKED.append((label, cmds, loaders))


ONE_ACCOUNT_CHECKED = []


def one_account_check(tmp):
    """25. ONE ACCOUNT, ONE PASSWORD. A MariaDB username a chart names under several keys (nova's oslo_db, oslo_db_api and
    oslo_db_cell0; octavia's oslo_db and oslo_db_persistence) is one account: db-init's ALTER USER would set two different
    passwords in turn, the last winning and the other readers refused. Rendered with two different passwords in values,
    with two different references, and with a reference on one key only, the chart must refuse, naming both keys; with
    the same password, or the same reference, under every key it renders."""
    for u, charts in sorted(DBUSERS.items()):
        for chart, ks in sorted((charts or {}).items()):
            if len(ks) < 2:
                continue
            w = stage(os.path.join(BP, chart, "chart"), tmp, "oneacct-%s-%s" % (chart, u))
            pairs = [tuple(k.split(".", 1)) for k in ks]
            names = ["endpoints.%s.auth.%s" % p for p in pairs]

            def vals(pw=None, refs=None):
                v = {}
                for i, (fam, key) in enumerate(pairs):
                    a = {}
                    if pw:
                        a["password"] = pw[i]
                    if refs and refs[i]:
                        a["passwordFrom"] = {"store": "openstack-credentials", "secret": refs[i], "key": "password"}
                    deep_merge(v, {"endpoints": {fam: {"auth": {key: a}}}})
                return v
            last = len(pairs) - 1
            cases = [
                ("different passwords in values", vals(pw=["GateSame7f3c"] * last + ["GateOther7f3c"]), False),
                ("different references", vals(refs=["gate-root-a"] * last + ["gate-root-b"]), False),
                ("a reference on one key only", vals(refs=["gate-root-a"] * last + [None]), False),
                ("the same password in values", vals(pw=["GateSame7f3c"] * len(pairs)), True),
                ("the same reference", vals(refs=["gate-root-a"] * len(pairs)), True),
            ]
            for label, values, ok in cases:
                rc, out, err = helm_template(w, chart + "-gate1234", values)
                if ok and rc != 0:
                    fail("25: %s: MariaDB user %s with %s under %s does not render: %s" % (chart, u, label, names,
                                                                                         err.strip()[:200]))
                elif not ok and rc == 0:
                    fail("25: %s: MariaDB user %s renders with %s under %s; one account has one password, so it must be "
                         "refused" % (chart, u, label, names))
                elif not ok and not (names[0] in err and names[-1] in err):
                    fail("25: %s: MariaDB user %s with %s is refused, but not by naming %s and %s: %s" % (
                        chart, u, label, names[0], names[-1], err.strip()[:200]))
                elif not ok and ("GateSame7f3c" in err or "GateOther7f3c" in err):
                    fail("25: %s: the refusal for MariaDB user %s prints a password" % (chart, u))
                else:
                    ONE_ACCOUNT_CHECKED.append((chart, u, label))
    if not ONE_ACCOUNT_CHECKED:
        fail("25: hack/db-users.yaml names no MariaDB user under two keys of one chart; the check had no input")


MARIADB_OWN_USERS = ("admin", "sst", "audit", "exporter")


def db_map_check(tmp):
    """15b. hack/db-users.yaml is exactly what the charts render: every chart is rendered with a sentinel per per-service
    endpoints.oslo_db*.auth key (MariaDB's own users and the PostgreSQL family are not per-service MariaDB users), and the
    (chart, family.key) pairs that render must equal the map's, under the map's username."""
    rendered = {}
    for d in sorted(os.listdir(BP)):
        vf = os.path.join(BP, d, "chart", "values.yaml")
        if d in ("openstack", "mariadb") or not os.path.isfile(vf):
            continue
        ep = (yaml.safe_load(open(vf)) or {}).get("endpoints") or {}
        keys = {}
        for fam, e in ep.items():
            if not fam.startswith("oslo_db") or fam == "oslo_db_postgresql" or not isinstance(e, dict):
                continue
            for k, a in (e.get("auth") or {}).items():
                # MariaDB's own users are tier 1, one root each (oslo-db-<user>), not per-service: the map's header says
                # so. openstack-monitoring renders `exporter` (#312), the first chart besides mariadb to hold one.
                if k not in MARIADB_OWN_USERS and isinstance(a, dict) and "password" in a:
                    keys["%s.%s" % (fam, k)] = a.get("username")
        if not keys:
            continue
        # one sentinel per USERNAME: one account has one password, and the charts refuse two (check 25). Where a username
        # sits under several keys, which of them renders is told by the database its URL names.
        sent = {k: "gate-dbmap-%s-%s-7f3c" % (d, str(u).replace("_", "-")) for k, u in keys.items()}
        vals = {}
        for k, s in sent.items():
            fam, key = k.split(".", 1)
            deep_merge(vals, {"endpoints": {fam: {"auth": {key: {"password": s}}}}})
        shared = {u for u in keys.values() if list(keys.values()).count(u) > 1}
        dbname = {k: str((ep.get(k.split(".", 1)[0]) or {}).get("path") or "").lstrip("/") for k in keys}
        w = stage(os.path.join(BP, d, "chart"), tmp, "dbmap-" + d)
        rc, out, err = helm_template(w, d + "-gate1234", vals)
        if rc != 0:
            fail("15b: %s: does not render for the MariaDB user map: %s" % (d, err.strip()[:200]))
            continue
        decoded = out
        for doc in yaml.safe_load_all(out):
            if doc and doc.get("kind") == "Secret":
                decoded += "\n".join(object_text(doc, k) or "" for k in (doc.get("data") or {}))
        for k, s in sent.items():
            if keys[k] in shared:
                hit = re.search(r"://[^:/@\s]+:%s@[^/\s'\"]+/%s(?=[?\s'\"]|$)" % (re.escape(s), re.escape(dbname[k])), decoded,
                                re.M)
            else:
                hit = s in decoded
            if hit:
                rendered[(d, k)] = keys[k]
    mapped = {(c, k): u for u, cs in DBUSERS.items() for c, ks in (cs or {}).items() for k in ks}
    if not rendered:
        fail("15b: no chart renders a per-service MariaDB user; the map check had no input")
    for pair in sorted(set(rendered) - set(mapped)):
        fail("15b: %s renders %s (MariaDB user %s) but hack/db-users.yaml does not list it" % (pair[0], pair[1], rendered[pair]))
    for pair in sorted(set(mapped) - set(rendered)):
        fail("15b: hack/db-users.yaml lists %s %s, which the chart does not render" % pair)
    for pair in sorted(set(mapped) & set(rendered)):
        if mapped[pair] != rendered[pair]:
            fail("15b: %s %s is MariaDB user %s, the map says %s" % (pair[0], pair[1], rendered[pair], mapped[pair]))


def identity_map_check(tmp):
    """15. hack/identity-users.yaml is exactly what the charts render: every chart with service users is
    rendered with a sentinel per endpoints.identity.auth key, and the (chart, key) pairs that render must equal
    the map's, with the map's username for each."""
    rendered = {}
    for d in sorted(os.listdir(BP)):
        vf = os.path.join(BP, d, "chart", "values.yaml")
        if d == "openstack" or not os.path.isfile(vf):
            continue
        auth = ((((yaml.safe_load(open(vf)) or {}).get("endpoints") or {}).get("identity") or {}).get("auth") or {})
        users = {k: v for k, v in auth.items() if k != "admin" and isinstance(v, dict) and "password" in v}
        if not users:
            continue
        sent = {k: "gate-idmap-%s-%s-7f3c" % (d, k) for k in users}
        w = stage(os.path.join(BP, d, "chart"), tmp, "idmap-" + d)
        rc, out, err = helm_template(w, d + "-gate1234", {"endpoints": {"identity": {"auth": {k: {"password": v} for k, v in sent.items()}}}})
        if rc != 0:
            fail("%s: does not render for the identity map: %s" % (d, err.strip()[:200]))
            continue
        decoded = out
        for doc in yaml.safe_load_all(out):
            if doc and doc.get("kind") == "Secret":
                decoded += "\n".join(object_text(doc, k) or "" for k in (doc.get("data") or {}))
        for k, sv in sent.items():
            if sv in decoded:
                rendered[(d, k)] = users[k].get("username")
    mapped = {(c, k): u for u, cs in IDUSERS.items() for c, k in cs.items()}
    for pair in sorted(set(rendered) - set(mapped)):
        fail("identity map: %s renders key %s (user %s) but hack/identity-users.yaml does not list it" % (pair[0], pair[1], rendered[pair]))
    for pair in sorted(set(mapped) - set(rendered)):
        fail("identity map: hack/identity-users.yaml lists %s key %s, which the chart does not render" % pair)
    for pair in sorted(set(mapped) & set(rendered)):
        if mapped[pair] != rendered[pair]:
            fail("identity map: %s key %s is user %s, the map says %s" % (pair[0], pair[1], rendered[pair], mapped[pair]))


def mq_map_check(tmp):
    """15c. hack/mq-users.yaml is exactly what the charts render: every chart is rendered with a sentinel per per-service
    endpoints.oslo_messaging.auth key, and the (chart, key) pairs whose URL logs in with it must equal the map's, under the
    map's username. A chart rendering another service's username (a cross-chart reader) shows up here under its own key,
    and the map then lists that username under both charts, with one root."""
    rendered = {}
    for d in sorted(os.listdir(BP)):
        vf = os.path.join(BP, d, "chart", "values.yaml")
        if d == "openstack" or not os.path.isfile(vf):
            continue
        auth = ((((yaml.safe_load(open(vf)) or {}).get("endpoints") or {}).get("oslo_messaging") or {}).get("auth") or {})
        keys = {k: a.get("username") for k, a in auth.items()
                if k not in MQ_NOT_SERVICE and isinstance(a, dict) and "password" in a}
        if not keys:
            continue
        sent = {k: "gatemqmap%s%s7f3c" % (d.replace("-", ""), k.replace("_", "")) for k in keys}
        w = stage(os.path.join(BP, d, "chart"), tmp, "mqmap-" + d)
        rc, out, err = helm_template(w, d + "-gate1234", {"endpoints": {"oslo_messaging": {"auth": {
            k: {"password": s} for k, s in sent.items()}}}})
        if rc != 0:
            fail("15c: %s: does not render for the RabbitMQ user map: %s" % (d, err.strip()[:200]))
            continue
        decoded = out
        for doc in yaml.safe_load_all(out):
            if doc and doc.get("kind") == "Secret":
                decoded += "\n".join(object_text(doc, k) or "" for k in (doc.get("data") or {}))
        for k, s in sent.items():
            if re.search(r"[/,]%s:%s@" % (re.escape(urllib.parse.quote(str(keys[k]), safe="")), re.escape(s)), decoded):
                rendered[(d, k)] = keys[k]
    mapped = {(c, k): u for u, cs in MQUSERS.items() for c, k in (cs or {}).items()}
    if not rendered:
        fail("15c: no chart renders a per-service RabbitMQ user; the map check had no input")
    for pair in sorted(set(rendered) - set(mapped)):
        fail("15c: %s renders %s (RabbitMQ user %s) but hack/mq-users.yaml does not list it" % (pair[0], pair[1],
                                                                                              rendered[pair]))
    for pair in sorted(set(mapped) - set(rendered)):
        fail("15c: hack/mq-users.yaml lists %s %s, which the chart does not render" % pair)
    for pair in sorted(set(mapped) & set(rendered)):
        if mapped[pair] != rendered[pair]:
            fail("15c: %s %s is RabbitMQ user %s, the map says %s" % (pair[0], pair[1], rendered[pair], mapped[pair]))


# 20b/22m: every `<user>:<password>@` of a RabbitMQ URL, the first after the scheme and each further host's after a comma
# (authenticated_transport_endpoint_uri_lookup writes one per StatefulSet replica).
MQ_URL = re.compile(r"\b(?:rabbit|amqp|kombu)[a-z0-9+.-]*://\S")
MQ_USERINFO = re.compile(r"(?:\b(?:rabbit|amqp|kombu)[a-z0-9+.-]*://|,)([^:/@\s,'\"]+):(\{\{.*?\}\}|[^@/\s,'\"]+)@")


def probe_texts(pod, cont, docs):
    """[(probe, the words it runs and the text of any mounted script it names)] for every exec probe of a container."""
    vols = {v["name"]: v for v in pod.get("volumes") or []}
    out = []
    for p in ("startupProbe", "readinessProbe", "livenessProbe"):
        cmd = ((cont.get(p) or {}).get("exec") or {}).get("command")
        if not cmd:
            continue
        texts = [" ".join(cmd)]
        for m in cont.get("volumeMounts") or []:
            if m.get("subPath") and any(m["mountPath"] == w or m["mountPath"] in w.split() for w in cmd):
                v = vols.get(m["name"]) or {}
                kind, name = (("ConfigMap", (v.get("configMap") or {}).get("name")) if "configMap" in v else
                              ("Secret", (v.get("secret") or {}).get("secretName")) if "secret" in v else (None, None))
                for d in docs:
                    if d.get("kind") == kind and d["metadata"]["name"] == name:
                        texts.append(re.sub(r"\\\n\s*", " ", object_text(d, m["subPath"]) or ""))
        out.append((p, "\n".join(texts)))
    return out


def mq_dropin_check(chart, cid, sentinel, docs_s, docs, label=""):
    """22m. A per-service RabbitMQ user's URLs, with its root: every ini option of a config that carried the user's password
    in values mode (sentinel render docs_s) is gone from that config and is in the drop-in <chart>-transport
    (88-transport.conf), as exactly the values-mode URL with every password read from this user's root (URL-quoted); a
    container that mounts a carrier receives the drop-in in a .conf.d exactly when hack/mq-readers.yaml lists it as using
    RabbitMQ, and no container reading none of the carriers receives it; and every exec probe of a
    receiving container that parses a carrier with --config-file also passes --config-dir <that .conf.d> (neutron's
    health-probe.py parses with no project, so it never reads a .conf.d by itself, and without transport_url it reports
    success without checking the agent's queue)."""
    drop = "%s-transport" % chart
    root = [i["secret"] for i in ITEMS_REF.values() if i["id"] == cid]
    projections = set(RULES[cid]["projections"](chart, yaml.safe_load(open(os.path.join(BP, chart, "chart", "values.yaml")))))
    carriers, want = set(), {}
    for d in docs_s:
        if d.get("kind") not in ("Secret", "ConfigMap") or d["metadata"]["name"] in projections:
            continue  # <chart>-rabbitmq-user (RABBITMQ_CONNECTION, read by rabbit-init): checks 3, 8 and 23m
        for k in (d.get("data") or {}):
            for so, val in ini_options(object_text(d, k)).items():
                if sentinel in val:
                    carriers.add((d["kind"], d["metadata"]["name"], k))
                    if want.get(so, val) != val:
                        fail("22m%s: %s: %s carries %s's URL with two values in values mode (%s/%s)" % (
                            label, chart, so, cid, d["metadata"]["name"], k))
                    want[so] = val
    if not carriers:
        fail("22m%s: %s: no rendered config carries %s's password in an ini option in values mode, so where the drop-in "
             "must arrive cannot be determined" % (label, chart, cid))
        return
    byname = {(d.get("kind"), d["metadata"]["name"]): d for d in docs}
    for kind, name, k in sorted(carriers):
        d = byname.get((kind, name))
        left = {so for so, v in ini_options(object_text(d, k) if d else "").items() if so in want}
        if left:
            fail("22m%s: %s: %s/%s %s still sets %s with %s's reference: the base config must not render the URL"
                 % (label, chart, kind, name, k, sorted(left, key=str), cid))
    es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == drop]
    if len(es) != 1:
        fail("22m%s: %s: renders %d ExternalSecret(s) %s, want 1 (%s)" % (label, chart, len(es), drop, cid))
        return
    tdata = ((es[0]["spec"].get("target") or {}).get("template") or {}).get("data") or {}
    if sorted(tdata) != [MQ_DROPIN_FILE]:
        fail("22m%s: %s: %s holds %s, want exactly %s" % (label, chart, drop, sorted(tdata), MQ_DROPIN_FILE))
        return
    keys = {e.get("secretKey"): (e.get("remoteRef") or {}).get("key") for e in es[0]["spec"].get("data") or []}
    got = ini_options(tdata[MQ_DROPIN_FILE])
    for so in sorted(set(got) - set(want), key=str):
        fail("22m%s: %s: drop-in %s sets %s, which carried no %s URL in values mode" % (label, chart, drop, so, cid))
    for so, val in sorted(want.items(), key=str):
        g = got.get(so)
        if g is None:
            fail("22m%s: %s: drop-in %s has no %s for %s" % (label, chart, drop, so, cid))
            continue
        used = re.findall(r"\{\{ \.(\w+) \| urlquery \}\}", g)
        filled = g
        for sk in set(used):
            if not root or keys.get(sk) != root[0]:
                fail("22m%s: %s: drop-in %s fills %s from %s (%s), not %s's root" % (label, chart, drop, so, sk,
                                                                                   keys.get(sk), cid))
            filled = filled.replace("{{ .%s | urlquery }}" % sk, urllib.parse.quote_plus(sentinel))
        if not used or "{{" in filled or filled != val:
            fail("22m%s: %s: drop-in %s %s is not the values-mode URL with every password from the root (%d placeholder(s); "
                 "same once filled: %s)" % (label, chart, drop, so, len(used), filled == val))
    spec = MQ_READERS.get(chart)
    if spec is None or not (spec or {}).get("readers"):
        fail("22m%s: %s: hack/mq-readers.yaml does not list which of %s's containers use RabbitMQ, so "
             "who must receive %s cannot be checked" % (label, chart, chart, drop))
        return
    users = set(spec["readers"])
    readers = 0
    rows = conf_readers(carriers, docs)
    for podname, cname, mounts, vols, reads, init in rows:
        gets = receives_in_confd(mounts, vols, drop)
        if reads and cname not in users:
            if gets and not (init and pod_shares_dropin(rows, podname, drop, users)):
                fail("22m%s: %s: %s container %s receives %s, but hack/mq-readers.yaml does not list it as using RabbitMQ "
                     "(it reads %s): the RabbitMQ password has no business there" % (label, chart, podname, cname, drop,
                                                                                         sorted(reads)))
            continue
        if reads:
            MQ_READERS_SEEN.add((chart, cname))
            readers += 1
            if not gets:
                fail("22m%s: %s: %s container %s reads %s and uses RabbitMQ (hack/mq-readers.yaml) but does not "
                     "receive %s in a .conf.d directory: it would have no transport URL" % (label, chart, podname, cname,
                                                                                           sorted(reads), drop))
                continue
            # where the drop-in lands in this container
            dirs = set()
            for m in mounts:
                v = vols.get(m["name"]) or {}
                srcs = [(v.get("secret") or {}).get("secretName")] if "secret" in v else \
                       [(x.get("secret") or {}).get("name") for x in (v.get("projected") or {}).get("sources") or []]
                if drop in srcs:
                    p = m["mountPath"].rstrip("/")
                    dirs.add(p if not m.get("subPath") else os.path.dirname(p))
            carrier_paths = {m["mountPath"] for m in mounts if m.get("subPath") in {k for _, _, k in carriers}}
            pod = [p for n, p in pod_specs(docs) if n == podname][0]
            cont = [c for c in (pod.get("containers") or []) + (pod.get("initContainers") or []) if c["name"] == cname][0]
            for probe, text in probe_texts(pod, cont, docs):
                files = set(re.findall(r"--config-file(?:=|\s+)(\S+)", text))
                if not files & carrier_paths:
                    continue
                MQ_PROBES_CHECKED.append((chart, podname, cname, probe))
                if not any(re.search(r"--config-dir(?:=|\s+)%s/?(?=\s|$)" % re.escape(dd), text) for dd in dirs):
                    fail("22m%s: %s: %s container %s's %s parses %s with --config-file but passes no --config-dir %s: "
                         "it never reads the drop-in, so it has no transport URL and checks nothing" % (
                             label, chart, podname, cname, probe, sorted(files & carrier_paths), " or ".join(sorted(dirs))))
        elif gets:
            fail("22m%s: %s: %s container %s receives %s but reads no config that carried %s's URL: the RabbitMQ password "
                 "has no business there" % (label, chart, podname, cname, drop, cid))
    if readers == 0:
        fail("22m%s: %s: no listed container reads a config that carried %s's URL" % (label, chart, cid))
    else:
        MQ_DROPIN_CHECKED.append((chart, cid, label, readers))


RABBIT_INIT_CHECKED = []
# A stand-in for rabbitmqadmin: every call is recorded, and any listing answers that the user already exists, so a script
# that declares the user only when it is missing (and so never sets a new password on an existing one) is caught.
RABBITMQADMIN_STUB = """#!/bin/bash
for a in "$@"; do printf '%s\\037' "$a"; done >> "$GATE_MQ_LOG"
printf '\\n' >> "$GATE_MQ_LOG"
case " $* " in *" list "*|*" show "*) printf '%s\\n' "$GATE_MQ_USER";; esac
exit 0
"""


def rabbit_init_check(chart, cid, docs, tmp, label=""):
    """23m. RABBIT-INIT SETS THE PASSWORD. The chart's rendered rabbit-init.sh is RUN, with a stand-in rabbitmqadmin, on the
    connection URLs its Job reads, the user's built from this user's root as the ExternalSecret renders it: it must
    `declare user name=<user> password=<the root's password>` unconditionally, with the user already existing, then
    declare the vhost and the user's permissions on it. `declare user` is an HTTP PUT of the user (create or replace), so
    it sets a new password on an existing user: measured 2026-11-16 on docker.io/rabbitmq:3.13-management, the chart's
    image (rabbitmqadmin invoke_declare -> PUT /api/users/<name>): run twice with two passwords, the second accepted and
    the first refused afterwards."""
    jobs = [d for d in docs if d.get("kind") == "Job" and d["metadata"]["name"].endswith("rabbit-init")]
    if not jobs:
        return
    script = None
    for d in docs:
        if d.get("kind") in ("ConfigMap", "Secret") and "rabbit-init.sh" in (d.get("data") or {}):
            script = object_text(d, "rabbit-init.sh")
    if not script:
        fail("23m%s: %s: renders a rabbit-init Job but no rabbit-init.sh" % (label, chart))
        return
    root = [i["secret"] for i in ITEMS_REF.values() if i["id"] == cid]
    pw = "Gate%sRotate7f3c" % chart.replace("-", "")
    admin_pw = "GateAdmin7f3c"
    admin_root = (ITEMS_REF.get("oslo-messaging-admin") or {}).get("secret")

    def connection(name, want_root, password):
        for d in docs:
            if d.get("kind") == "ExternalSecret" and (((d["spec"].get("target") or {}).get("name")) or
                                                      d["metadata"]["name"]) == name:
                data = {e.get("secretKey"): (e.get("remoteRef") or {}).get("key") for e in d["spec"].get("data") or []}
                if want_root not in data.values():
                    return None, "ExternalSecret %s reads %s, not the root %s" % (name, sorted(data.values()), want_root)
                t = (((d["spec"].get("target") or {}).get("template") or {}).get("data") or {}).get("RABBITMQ_CONNECTION")
                for sk in data:
                    t = (t or "").replace("{{ .%s }}" % sk, password)
                return t, None
        return None, "%s is not an ExternalSecret reading the root, so rabbit-init would set the published password" % name
    pod = jobs[0]["spec"]["template"]["spec"]
    cont = [c for c in pod.get("containers") or [] if c["name"] == "rabbit-init"]
    env = {e["name"]: e for e in (cont[0].get("env") or [])} if cont else {}
    refname = lambda n: (((env.get(n) or {}).get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
    user_conn, why = connection(refname("RABBITMQ_USER_CONNECTION"), root[0] if root else None, pw)
    admin_conn, why2 = connection(refname("RABBITMQ_ADMIN_CONNECTION"), admin_root, admin_pw)
    if not user_conn or not admin_conn:
        fail("23m%s: %s: rabbit-init's connections are not built from the roots: %s" % (label, chart, why or why2))
        return
    u = urllib.parse.urlsplit(user_conn)
    work = tempfile.mkdtemp(dir=tmp, prefix="rabbitinit-%s-" % chart)
    os.makedirs(os.path.join(work, "bin"))
    stub = os.path.join(work, "bin", "rabbitmqadmin")
    open(stub, "w").write(RABBITMQADMIN_STUB)
    os.chmod(stub, 0o755)
    sp = os.path.join(work, "rabbit-init.sh")
    open(sp, "w").write(script)
    log = os.path.join(work, "calls.log")
    runenv = {"PATH": os.path.join(work, "bin") + os.pathsep + os.environ.get("PATH", ""), "GATE_MQ_LOG": log,
              "GATE_MQ_USER": u.username or "", "RABBITMQ_ADMIN_CONNECTION": admin_conn,
              "RABBITMQ_USER_CONNECTION": user_conn}
    if (env.get("RABBITMQ_AUXILIARY_CONFIGURATION") or {}).get("value"):
        runenv["RABBITMQ_AUXILIARY_CONFIGURATION"] = env["RABBITMQ_AUXILIARY_CONFIGURATION"]["value"]
    r = subprocess.run(["bash", sp], env=runenv, capture_output=True, text=True, cwd=work)
    calls = [l.rstrip("\n").split("\x1f")[:-1] for l in open(log)] if os.path.exists(log) else []
    if r.returncode != 0:
        fail("23m%s: %s: rabbit-init.sh did not run against the stand-in (rc %d): %s" % (label, chart, r.returncode,
                                                                                        r.stderr.strip()[-300:]))
        return
    vhost = urllib.parse.unquote(u.path.lstrip("/")) or "/"
    want_user = ["declare", "user", "name=%s" % u.username, "password=%s" % pw]
    has = lambda seq, c: any(c[i:i + len(seq)] == seq for i in range(len(c) - len(seq) + 1))
    users = [c for c in calls if has(want_user, c)]
    if not users:
        fail("23m%s: %s: rabbit-init.sh never runs `declare user name=%s password=<the root's>` while the user exists: the "
             "generated password would never reach RabbitMQ (calls: %d)" % (label, chart, u.username, len(calls)))
        return
    if any("--host=%s" % u.hostname not in c for c in users):
        fail("23m%s: %s: rabbit-init.sh declares the user against another host than the connection names" % (label, chart))
    if not any("--password=%s" % admin_pw in c for c in users):
        fail("23m%s: %s: rabbit-init.sh does not authenticate as the administrator from its root" % (label, chart))
    if vhost != "/" and not any(has(["declare", "vhost", "name=%s" % vhost], c) for c in calls):
        fail("23m%s: %s: rabbit-init.sh does not declare vhost %s" % (label, chart, vhost))
    if not any(has(["declare", "permission", "vhost=%s" % vhost, "user=%s" % u.username], c) for c in calls):
        fail("23m%s: %s: rabbit-init.sh does not grant %s its permissions on %s" % (label, chart, u.username, vhost))
    RABBIT_INIT_CHECKED.append((chart, label))


# 26. NOTIFICATION VHOSTS THAT EXIST (#285). ceilometer's notification agent consumed upstream's fixed list of seven vhosts,
# and oslo.messaging retries a missing one forever: on a full-strict install (cinder not selected; glance and keystone
# create no vhost) it consumed nothing (GKE 2026-11-16, 442 NOT_ALLOWED in 26 minutes).
VHOST_CASES_CHECKED = []
VHOST_JOBS_CHECKED = []


_VHOST_CACHE = {}


def rabbit_init_vhost(tmp, chart, values=None):
    """What chart's rabbit-init renders with `values`: {"job": a rabbit-init Job renders, "secret": the Secret its
    RABBITMQ_USER_CONNECTION reads renders too, "vhost": that connection's path, the vhost it declares, or None}. A Job
    whose Secret is not rendered never starts (keystone ships secret_rabbitmq off), so it creates no vhost."""
    key = (chart, json.dumps(values or {}, sort_keys=True))
    if key in _VHOST_CACHE:
        return _VHOST_CACHE[key]
    w = stage(os.path.join(BP, chart, "chart"), tmp, "vhost-%s-%d" % (chart, len(_VHOST_CACHE)))
    rc, o, e = helm_template(w, chart + "-gate1234", values or {})
    out = {"job": False, "secret": False, "vhost": None, "name": None}
    if rc != 0:
        fail("26: %s: does not render with %s: %s" % (chart, values or "its defaults", e.strip()[:200]))
    else:
        docs = [x for x in yaml.safe_load_all(o) if x]
        secrets = {x["metadata"]["name"]: x for x in docs if x.get("kind") == "Secret"}
        for j in docs:
            if j.get("kind") != "Job" or not j["metadata"]["name"].endswith("rabbit-init"):
                continue
            out["job"] = True
            out["name"] = j["metadata"]["name"]
            for c in j["spec"]["template"]["spec"].get("containers") or []:
                for ev in c.get("env") or []:
                    if ev["name"] != "RABBITMQ_USER_CONNECTION":
                        continue
                    n = ((ev.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
                    url = object_text(secrets[n], "RABBITMQ_CONNECTION") if n in secrets else None
                    if not url:
                        continue  # the Job's Secret is not rendered: it never starts
                    out["secret"] = True
                    vh = urllib.parse.unquote(urllib.parse.urlsplit(url).path.lstrip("/")) or "/"
                    if out["vhost"] is not None and out["vhost"] != vh:
                        fail("26: %s: two rabbit-init Jobs declare vhosts %s and %s" % (chart, out["vhost"], vh))
                    out["vhost"] = vh
    _VHOST_CACHE[key] = out
    return out


def notification_vhosts_check(tmp, components):
    """26. The umbrella's components[].rabbitmqVhost is exactly the vhost each chart's rabbit-init declares (rendered with
    the Job and its Secret on), rabbitmqInit: false exactly when the chart ships that Job off and rabbitmqSecret: false
    exactly when it ships the Job's Secret off (keystone), all measured, both ways; notificationVhosts.vhosts is the consumer chart's own conf.notification_vhosts default, in order; and for
    each profile, a sample of explicit selections, and componentValues that switch a component's rabbit-init or its
    Secret off or on or rename its vhost (endpoints.oslo_messaging.path), the list the consumer RENDERS (its [notification]
    messaging_urls, in its config or its drop-in, given the spec compositions.yaml builds) holds exactly what the
    selected closure creates, each component's chart rendered with that component's componentValues: for every entry of
    notificationVhosts.vhosts whose component is selected and creates a vhost, the vhost it creates, under the name it
    creates it; none that nothing created (oslo.messaging would retry it forever and never reach the others), and none
    left out."""
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    nv = uv.get("notificationVhosts") or {}
    consumer = nv.get("component") or "ceilometer"
    on, dflt = {}, {}
    for c in components:
        n = c["name"]
        full = rabbit_init_vhost(tmp, n, {"manifests": {"job_rabbit_init": True, "secret_rabbitmq": True}})
        job_on = rabbit_init_vhost(tmp, n, {"manifests": {"job_rabbit_init": True}})
        at_defaults = rabbit_init_vhost(tmp, n)
        on[n] = full["vhost"]
        dflt[n] = at_defaults["vhost"]
        if full["name"] is not None and full["name"] != "%s-rabbit-init" % n:
            fail("26b: %s's rabbit-init Job renders as %s; the umbrella passes ceilometer %s-rabbit-init to wait for"
                 % (n, full["name"], n))
        if c.get("rabbitmqVhost") != on[n]:
            fail("26: umbrella: component %s declares rabbitmqVhost %s, its chart's rabbit-init declares %s" % (
                n, c.get("rabbitmqVhost"), on[n]))
        if on[n] is None:
            for k in ("rabbitmqInit", "rabbitmqSecret"):
                if k in c:
                    fail("26: umbrella: component %s declares %s, but its chart has no rabbit-init" % (n, k))
            continue
        for k, got, what in (("rabbitmqInit", at_defaults["job"], "a rabbit-init Job"),
                             ("rabbitmqSecret", job_on["secret"], "the rabbit-init Job's Secret")):
            declared = str(c.get(k, True)).lower() == "true"
            if declared != got:
                fail("26: umbrella: component %s declares %s %s, its chart renders %s %s at its defaults" % (
                    n, k, str(declared).lower(), what, "" if got else "off"))
    if not any(on.values()):
        fail("26: no chart renders a rabbit-init Job; the vhost map had no input")
    cdef = ((yaml.safe_load(open(os.path.join(BP, consumer, "chart", "values.yaml"))) or {}).get("conf") or {}).get(
        "notification_vhosts")
    if nv.get("vhosts") != cdef:
        fail("26: umbrella: notificationVhosts.vhosts is %s, %s's conf.notification_vhosts default is %s: the two must be "
             "the same list" % (nv.get("vhosts"), consumer, cdef))
    comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
    start = comp.find("{{- $ov := ")
    mark = comp.find("include \"osh.notificationVhosts\"", start)
    if mark < 0:
        # no such block: the spec ends after the tolerations merge, and the consumer renders its chart default
        mark = comp.find('"tolerations"', start)
    end = comp.find("{{- end }}", mark) if mark >= 0 else -1
    if start < 0 or end < 0:
        fail("26: compositions.yaml no longer builds $ov the way this check extracts it (from `{{- $ov := ` through the "
             "notification vhosts merge)")
        return
    span = comp[start:end + len("{{- end }}")]
    fs = "full-strict"
    cases = [("profile identity", {"profile": "identity"}), ("profile full", {"profile": "full"}),
             ("profile full-strict", {"profile": fs}),
             ("enabled [ceilometer]", {"enabled": ["ceilometer"]}),
             ("enabled [ceilometer, cinder, glance]", {"enabled": ["ceilometer", "cinder", "glance"]}),
             ("enabled [ceilometer, nova]", {"enabled": ["ceilometer", "nova"]}),
             ("full-strict, nova's rabbit-init off", {"profile": fs, "componentValues": {
                 "nova": {"manifests": {"job_rabbit_init": False}}}}),
             ("full-strict, glance's rabbit-init on", {"profile": fs, "componentValues": {
                 "glance": {"manifests": {"job_rabbit_init": True}}}}),
             ("full-strict, heat's vhost renamed /orchestration", {"profile": fs, "componentValues": {
                 "heat": {"endpoints": {"oslo_messaging": {"path": "/orchestration"}}}}}),
             ("full-strict, keystone's rabbit-init on, its Secret off", {"profile": fs, "componentValues": {
                 "keystone": {"manifests": {"job_rabbit_init": True}}}}),
             ("full-strict, keystone's rabbit-init and Secret on", {"profile": fs, "componentValues": {
                 "keystone": {"manifests": {"job_rabbit_init": True, "secret_rabbitmq": True}}}})]
    for n, (label, vals) in enumerate(cases):
        u = stage(UMBRELLA, tmp, "umbrella-vh-%d" % n)
        with open(os.path.join(u, "templates", "zz-gate-vhosts-probe.yaml"), "w") as f:
            f.write('{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
                    '# SELECTED {{ join " " $sel }}\n'
                    '{{- range $c := .Values.components }}\n{{- if has $c.name $sel }}\n' + span + '\n'
                    '# VHSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n{{- end }}\n')
        rc, out, err = helm_template(u, "openstack-gate", vals)
        if rc != 0:
            fail("26: %s: the umbrella does not render: %s" % (label, err.strip()[:300]))
            continue
        sel, spec = None, None
        for line in out.splitlines():
            if line.startswith("# SELECTED "):
                sel = set(line[len("# SELECTED "):].split())
            elif line.startswith("# VHSPEC %s " % consumer):
                spec = json.loads(line.split(" ", 3)[3])
        if not sel:
            fail("26: %s: osh.selected returned nothing" % label)
            continue
        if consumer not in sel:
            VHOST_CASES_CHECKED.append((label, None))
            continue
        # what the selected closure creates, each chart rendered with its own componentValues: default vhost -> created
        cvs = vals.get("componentValues") or {}
        created, creator_job = {}, {}
        for c in components:
            if c["name"] not in sel or on.get(c["name"]) is None:
                continue
            r = rabbit_init_vhost(tmp, c["name"], cvs[c["name"]] if c["name"] in cvs else None)
            if r["vhost"] is not None:
                created[on[c["name"]]] = r["vhost"]
                creator_job[r["vhost"]] = (c["name"], r["name"])
        made = set(created.values())
        w = stage(os.path.join(BP, consumer, "chart"), tmp, "vh-%s-%d" % (consumer, n))
        rcc, oc, ec = helm_template(w, consumer + "-gate1234", spec or {})
        if rcc != 0:
            fail("26: %s: %s does not render with the spec the umbrella builds: %s" % (label, consumer, ec.strip()[:300]))
            continue
        listed = []
        for d in yaml.safe_load_all(oc):
            if not d:
                continue
            texts = []
            if d.get("kind") in ("Secret", "ConfigMap"):
                texts = [object_text(d, k) or "" for k in (d.get("data") or {})]
            elif d.get("kind") == "ExternalSecret":
                texts = [str(v) for v in (((d["spec"].get("target") or {}).get("template") or {}).get("data") or {}).values()]
            for t in texts:
                section = None
                for line in t.splitlines():
                    s = line.strip()
                    if s.startswith("[") and s.endswith("]"):
                        section = s[1:-1]
                    elif section == "notification" and re.match(r"messaging_urls\s*=", s):
                        url = s.split("=", 1)[1].strip()
                        listed.append(urllib.parse.unquote(url.rsplit("/", 1)[-1]))
        want = [created[v] for v in (nv.get("vhosts") or []) if v in created]
        if not listed:
            fail("26: %s: %s renders no [notification] messaging_urls" % (label, consumer))
            continue
        for v in listed:
            if v not in made:
                fail("26: %s: %s consumes vhost %s, which no selected component creates (the selected closure creates "
                     "%s); oslo.messaging retries it forever and never reaches the others" % (label, consumer, v,
                                                                                               sorted(made)))
        missing = sorted(set(want) - set(listed))
        if missing:
            fail("26: %s: %s does not consume %s, which selected components create" % (label, consumer, missing))
        # 26b (#298): ceilometer-notification waits for the rabbit-init Job of every vhost it consumes (its own aside,
        # which its chart already lists): its rendered DEPENDENCY_JOBS, less the chart's own static jobs, are exactly
        # the Jobs the creating components render, by the names they render them under.
        own = set(((((yaml.safe_load(open(os.path.join(BP, consumer, "chart", "values.yaml"))) or {}).get("dependencies")
                    or {}).get("static") or {}).get("notification") or {}).get("jobs") or [])
        deps = None
        for d in yaml.safe_load_all(oc):
            if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "%s-notification" % consumer:
                for ic in d["spec"]["template"]["spec"].get("initContainers") or []:
                    for ev in ic.get("env") or []:
                        if ev["name"] == "DEPENDENCY_JOBS":
                            deps = set(x for x in (ev.get("value") or "").split(",") if x)
        want_jobs = {creator_job[v][1] for v in listed if v in creator_job and creator_job[v][0] != consumer}
        if deps is None:
            fail("26b: %s: %s-notification renders no DEPENDENCY_JOBS" % (label, consumer))
        else:
            extra, missing = sorted((deps - own) - want_jobs), sorted(want_jobs - deps)
            if missing:
                fail("26b: %s: %s-notification consumes the vhosts of %s but does not wait for those Jobs: on a fresh "
                     "install it starts before they exist and its workers die with NOT_ALLOWED (#298)"
                     % (label, consumer, missing))
            if extra:
                fail("26b: %s: %s-notification waits for %s, which create no vhost it consumes" % (label, consumer, extra))
            VHOST_JOBS_CHECKED.append((label, tuple(sorted(want_jobs))))
        VHOST_CASES_CHECKED.append((label, tuple(listed)))


# 19. Programs named after these projects read oslo.config. swift's servers read paste-deploy files, not oslo.config,
# so swift- is not here. Horizon and skyline run no such binary.
OSLO_PROJECTS = ("aodh", "barbican", "blazar", "ceilometer", "cinder", "cloudkitty", "cyborg", "designate", "freezer",
                 "glance", "gnocchi", "heat", "ironic", "keystone", "magnum", "manila", "masakari", "masakarimonitors",
                 "mistral", "neutron", "nova", "octavia", "placement", "tacker", "trove", "watcher", "zaqar")
# Which of them accept --config-dir is MEASURED, not inferred from the name: hack/oslo-config-dir-programs.yaml (0.3.66
# passed it to barbican-db-manage, an argparse program, and its db-sync crash-looped).
_CDP = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "oslo-config-dir-programs.yaml")))
CONFIG_DIR_ACCEPTS = set(_CDP.get("accepts") or [])
CONFIG_DIR_REJECTS = {r["name"]: r.get("measured", "") for r in (_CDP.get("rejects") or [])}
OSLO_PROGRAM = re.compile(r"(?:^\s*|[;&|(]\s*|\bexec\s+|\btimeout\s+\d+\s+)((?:%s)-[a-z0-9-]+)(?=\s|$)" % "|".join(OSLO_PROJECTS))
PY_PROGRAM = re.compile(r"""(?:subprocess\.(?:call|run|check_call|check_output|Popen)|os\.exec\w+)\(\s*\[\s*['"]((?:%s)-[a-z0-9-]+)['"]""" % "|".join(OSLO_PROJECTS))
CONFIG_DIR_CHECKED = []
INIT_CONFD_CHECKED = []


def init_confd_check(chart, docs, label=""):
    """19c. EVERY INIT CONTAINER THAT RUNS AN OPENSTACK PROGRAM READS ITS DROP-INS. An init container that starts a
    <project>-* program and mounts that project's main config (/etc/<dir>/<file>.conf, where the drop-ins of
    <file>.conf live in <file>.conf.d) must mount <file>.conf.d as well: 0.3.69's designate-worker-init mounted
    designate.conf alone, its `[ -d ]` guard dropped --config-dir, and designate-manage pool update dialled localhost
    for RabbitMQ forever (#291). Check 19 only looked at containers that mount a .conf.d, so it never saw it."""
    for podname, pod in pod_specs(docs):
        if podname.startswith("Pod/"):
            continue
        for cont in pod.get("initContainers") or []:
            progs = openstack_programs(pod, cont, docs)
            if not progs:
                continue
            projects = {p.split("-", 1)[0] for p in progs}
            mounted = set()
            for m in cont.get("volumeMounts") or []:
                path = m["mountPath"].rstrip("/")
                mounted.add(path)
                if m.get("subPath"):
                    mounted.add(os.path.dirname(path))
            confs = sorted(p for p in mounted if re.match(r"^/etc/(%s)/[^/]+\.conf$" % "|".join(projects), p)
                           and os.path.basename(p)[:-5] in projects)
            for conf in confs:
                INIT_CONFD_CHECKED.append((chart, podname, cont["name"], conf))
                if conf + ".d" not in mounted:
                    fail("19c%s: %s: %s init container %s starts %s and mounts %s but not %s: its program never reads the "
                         "drop-ins (0.3.69's designate-worker-init had no transport_url, #291)" % (
                             label, chart, podname, cont["name"], ", ".join(sorted(progs)), conf, conf + ".d"))


def container_scripts(pod, cont, docs):
    """The shell text a container runs: its inline `bash -c` script and every mounted script its command names."""
    vols = {v["name"]: v for v in pod.get("volumes") or []}
    cmd = (cont.get("command") or []) + (cont.get("args") or [])
    texts = []
    if len(cmd) >= 3 and cmd[0].endswith("sh") and cmd[1] == "-c":
        texts.append(cmd[2])
    words = set(re.findall(r"/[\w./-]+", " ".join(cmd)))
    for m in cont.get("volumeMounts") or []:
        if m["mountPath"] in words and m.get("subPath"):
            v = vols.get(m["name"]) or {}
            kind, name = (("ConfigMap", (v.get("configMap") or {}).get("name")) if "configMap" in v else
                          ("Secret", (v.get("secret") or {}).get("secretName")) if "secret" in v else (None, None))
            for d in docs:
                if d.get("kind") == kind and d["metadata"]["name"] == name:
                    t = object_text(d, m["subPath"])
                    if t:
                        texts.append(t)
    return texts


def config_dir_check(chart, docs):
    """19. EVERY PARSE READS THE DROP-INS. In every container that mounts a .conf.d, every OpenStack program its
    scripts start passes --config-dir <that dir>. oslo.config finds <project>.conf.d by itself only on a parse that
    names the project; watcher's plugin loader re-parses with neither project nor dirs, and on 0.3.65 its decision
    engine lost every drop-in value (\"password is a required attribute\"). An argv flag survives such a re-parse,
    since it reuses sys.argv. WSGI apps (uwsgi, Apache) take no argv; they are out of scope here."""
    for podname, pod in pod_specs(docs):
        if podname.startswith("Pod/"):
            continue  # helm test pods run on demand
        for cont in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            dirs = set()
            for m in cont.get("volumeMounts") or []:
                path = m["mountPath"].rstrip("/")
                d = os.path.dirname(path) if m.get("subPath") else path
                if d.endswith(".conf.d"):
                    dirs.add(d)
            if not dirs:
                continue
            for text in container_scripts(pod, cont, docs):
                logical = re.sub(r"\\\n\s*", " ", text)
                for line in logical.splitlines():
                    code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
                    for prog in OSLO_PROGRAM.findall(code):
                        CONFIG_DIR_CHECKED.append((chart, podname, cont["name"], prog))
                        has_flag = re.search(r"--config-dir[ =]", code) is not None
                        if prog in CONFIG_DIR_REJECTS:
                            if has_flag:
                                fail("19: %s: %s container %s passes --config-dir to %s, which rejects it (%s); "
                                     "hack/oslo-config-dir-programs.yaml" % (chart, podname, cont["name"], prog,
                                                                             CONFIG_DIR_REJECTS[prog]))
                            continue
                        if prog not in CONFIG_DIR_ACCEPTS:
                            fail("19: %s: %s container %s starts %s, which hack/oslo-config-dir-programs.yaml does not "
                                 "list; measure whether it accepts --config-dir (<program> --config-dir /nonexistent "
                                 "--help in its image) and add it to accepts or rejects" % (chart, podname, cont["name"],
                                                                                           prog))
                            continue
                        # the flag may be guarded on the directory existing: $([ -d D ] && echo --config-dir D)
                        if not any(re.search(r"--config-dir[ =]%s/?(?=[\s)\"']|$)" % re.escape(d), code) for d in dirs):
                            fail("19: %s: %s container %s starts %s without --config-dir %s; oslo.config finds that "
                                 "directory only on a parse that names the project, and a re-parse without it (watcher's "
                                 "plugin loader, 0.3.65) loses every drop-in" % (chart, podname, cont["name"], prog,
                                                                                 " or ".join(sorted(dirs))))



def init_confd_survey(tmp):
    """19c over every chart, at defaults, with every workload switched on, and in the modes a manifest alone does not
    select (MQ_VARIANTS): an init container is checked in every mode that renders it."""
    for d in sorted(os.listdir(BP)):
        if d == "openstack" or not os.path.isfile(os.path.join(BP, d, "chart", "values.yaml")):
            continue
        w = stage(os.path.join(BP, d, "chart"), tmp, "initconfd-" + d)
        aw = all_workloads(d) or {}
        modes = [("", {}), (" (every workload on)", aw)]
        for v in MQ_VARIANTS.get(d, []):
            modes.append((" (with %s)" % json.dumps(v, sort_keys=True), deep_merge(json.loads(json.dumps(aw)),
                                                                                  json.loads(json.dumps(v)))))
        for label, vals in modes:
            rc, out, err = helm_template(w, d + "-gate1234", vals)
            if rc != 0:
                fail("19c: %s%s: does not render: %s" % (d, label, err.strip()[:200]))
                continue
            init_confd_check(d, [x for x in yaml.safe_load_all(out) if x], label)


def config_dir_defaults(chart, docs0):
    """19b. A --config-dir that is not guarded by `[ -d <dir> ]` names a directory the container mounts, in the render
    at defaults too: oslo.config aborts on a --config-dir that does not exist, and a chart whose drop-ins are mounted
    file by file has no such directory until a drop-in is active."""
    for podname, pod in pod_specs(docs0):
        if podname.startswith("Pod/"):
            continue
        for cont in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            mounted = set()
            for m in cont.get("volumeMounts") or []:
                path = m["mountPath"].rstrip("/")
                mounted.add(path)
                if m.get("subPath"):
                    mounted.add(os.path.dirname(path))
            for text in container_scripts(pod, cont, docs0):
                logical = re.sub(r"\\\n\s*", " ", text)
                for line in logical.splitlines():
                    if line.lstrip().startswith("#"):
                        continue
                    for cm in re.finditer(r"--config-dir[ =](\S+?)/?(?=[\s)\"']|$)", line):
                        d = cm.group(1)
                        if "$" in d or d in mounted:
                            continue
                        if re.search(r"\[\s+-d\s+%s/?\s+\]" % re.escape(d), line[:cm.start()]):
                            continue
                        fail("19b: %s: at defaults %s container %s passes --config-dir %s, which it does not mount; "
                             "oslo.config aborts on a missing directory (guard it: $([ -d %s ] && echo --config-dir %s))"
                             % (chart, podname, cont["name"], d, d, d))


# 20. No published default survives in a password-like option. Checks 1-19 follow each credential from where its
# value sits in values (endpoints.*.auth) by planting a sentinel there; a literal written under conf instead is invisible
# to that. blazar's conf.blazar.physical_host_plugin.blazar_password: password (the blazar user's published default)
# stayed in blazar.conf with the blazar user generated, found by a live scan on GKE 2026-11-16 (r066).
# The cache sections are #228's remaining tier. The database sections were exempt too until the per-service MariaDB
# users got their roots (0.3.68), and the messaging sections until the per-service RabbitMQ users did (0.3.69): both are
# checked now.
TIER2_SECTIONS = {"cache", "coordination"}
PW_KEY = re.compile(r"(^|_)(password|passwd|secret|secret_key)$")
LITERALS_CHECKED = []
# 20b. A password inside a URL's userinfo has no password-like option name: ceilometer's [notification]
# messaging_urls carried the RabbitMQ administrator's published password in seven rabbit:// URLs after that password
# was generated (0.3.57), invisible to 20 (#281, found by rendering 2026-11-16). Every URL in rendered config and
# Secrets is read, and the subject is the URL's USER, not the section it sits in: a URL logging in as an install-wide
# administrator (MariaDB root, RabbitMQ admin), whose root is generated, must never carry the published value. Since
# 0.3.68 the same holds for a mysql URL logging in as a per-service MariaDB user (hack/db-users.yaml), every one of
# which has a root: [database] connection and its siblings, gnocchi's [indexer] url, DB_CONNECTION. The subject is the
# URL's scheme and user: a rabbit:// URL as a per-service RabbitMQ user of the same name (transport_url) is 0.3.69's
# tier. A section-based exemption would have let #281 through ([notification] is not one).
URL_USERINFO = re.compile(r"\b([a-z][a-z0-9+.-]*)://([^:/@\s'\"]+):(\{\{.*?\}\}|[^@/\s'\"]+)@")
URLS_CHECKED = []
DB_URLS_CHECKED = []
MQ_URLS_CHECKED = []


def admin_usernames(values):
    """The install-wide administrators' usernames a chart's values name (MariaDB root, RabbitMQ admin)."""
    ep = (values or {}).get("endpoints") or {}
    out = set()
    for fam, users in (("oslo_db", ("admin",)), ("oslo_messaging", ("admin", "user"))):
        auth = ((ep.get(fam) or {}).get("auth") or {})
        for u in users:
            name = (auth.get(u) or {}).get("username") if isinstance(auth.get(u), dict) else None
            if name:
                out.add(name)
    return out


def published_secrets(values):
    """Every non-empty string the chart's values hold under a password-like key: what OpenStack-Helm publishes."""
    out = set()
    def walk(v, key=""):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, str(k))
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and v and PW_KEY.search(key.lower()) and len(v) >= 6 and "{{" not in v:
            out.add(v)
    walk(values)
    return out


def no_published_literal(chart, published, docs, admins=frozenset(), dbusers=frozenset(), mqusers=frozenset()):
    """20. With every reference set, no ini option with a password-like name outside tier-2 sections holds a value the
    chart publishes. 20b. No URL logging in as an install-wide administrator (`admins`), a mysql URL logging in as a
    per-service MariaDB user (`dbusers`), or a RabbitMQ URL logging in, at any of its hosts, as a per-service RabbitMQ
    user (`mqusers`, 0.3.69), carries a value the chart publishes, in any section or Secret."""
    for d in docs:
        if d.get("kind") not in ("Secret", "ConfigMap", "ExternalSecret"):
            continue
        if d.get("kind") == "ExternalSecret":
            # The generated files' templates: an administrator URL here must take its password from the root.
            texts = (((d.get("spec") or {}).get("target") or {}).get("template") or {}).get("data") or {}
        else:
            texts = {k: object_text(d, k) for k in (d.get("data") or {})}
        for k, text in texts.items():
            text = str(text or "")
            section = None
            for line in text.splitlines():
                m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
                if m:
                    section = m.group(1).strip()
                    continue
                for um in URL_USERINFO.finditer(line):
                    scheme = um.group(1)
                    user, pw = urllib.parse.unquote(um.group(2)), urllib.parse.unquote(um.group(3))
                    as_db_user = scheme.startswith("mysql") and user in dbusers and user not in admins
                    if user not in admins and not as_db_user:
                        continue
                    (DB_URLS_CHECKED if as_db_user else URLS_CHECKED).append((chart, d["metadata"]["name"], k, section, user))
                    if "{{" in pw:
                        continue  # read from the generated root by the ExternalSecret template
                    if pw in published:
                        fail("20b: %s: %s/%s %s [%s] a URL logs in as %s%s with a password the chart publishes; with the "
                             "references set it must be built from the generated root (a drop-in), not from a literal"
                             % (chart, d["kind"], d["metadata"]["name"], k, section,
                                "MariaDB user " if as_db_user else "", user))
                if MQ_URL.search(line):
                    for um in MQ_USERINFO.finditer(line):
                        user, pw = urllib.parse.unquote(um.group(1)), urllib.parse.unquote(um.group(2))
                        if user not in mqusers or user in admins:
                            continue
                        MQ_URLS_CHECKED.append((chart, d["metadata"]["name"], k, section, user))
                        if "{{" in pw:
                            continue  # read from the generated root by the ExternalSecret template
                        if pw in published:
                            fail("20b: %s: %s/%s %s [%s] a URL logs in as RabbitMQ user %s with a password the chart "
                                 "publishes; with the references set it must be built from the generated root (a "
                                 "drop-in), not from a literal" % (chart, d["kind"], d["metadata"]["name"], k, section,
                                                                   user))
                m = re.match(r"^\s*([\w.-]+)\s*=\s*(.*?)\s*$", line)
                if not m or not PW_KEY.search(m.group(1).lower()):
                    continue
                LITERALS_CHECKED.append((chart, d["metadata"]["name"], k, section, m.group(1)))
                if (section or "").lower() in TIER2_SECTIONS:
                    continue
                if m.group(2).strip("'\"") in published:
                    fail("20: %s: %s/%s %s [%s] %s holds a password the chart publishes in its values; with the "
                         "references set it must come from a generated root (a drop-in), not from a literal under conf"
                         % (chart, d["kind"], d["metadata"]["name"], k, section, m.group(1)))

KEYFILE_CHECKED = []


def mounted_secret_files(pod, cont):
    """{path in the container: (Secret name, key, mode)} for every Secret file a container mounts (secret and
    projected volumes; a directory mount without items lists every key the volume's items name, or `*`)."""
    vols = {v["name"]: v for v in pod.get("volumes") or []}
    out = {}
    for m in cont.get("volumeMounts") or []:
        v = vols.get(m["name"]) or {}
        if "secret" in v:
            srcs = [(v["secret"].get("secretName"), v["secret"].get("items"), v["secret"].get("defaultMode"))]
        elif "projected" in v:
            dm = v["projected"].get("defaultMode")
            srcs = [((x.get("secret") or {}).get("name"), (x.get("secret") or {}).get("items"), dm)
                    for x in v["projected"].get("sources") or [] if "secret" in x]
        else:
            continue
        for name, items, dmode in srcs:
            files = {i["path"]: (i["key"], i.get("mode", dmode)) for i in items} if items else {"*": ("*", dmode)}
            for path, (key, mode) in files.items():
                mode = 0o644 if mode is None else mode
                if m.get("subPath"):
                    if m["subPath"] == path or (path == "*"):
                        out[m["mountPath"]] = (name, key if path != "*" else m["subPath"], mode)
                elif path == "*":
                    out[m["mountPath"].rstrip("/") + "/*"] = (name, "*", mode)
                else:
                    out[m["mountPath"].rstrip("/") + "/" + path] = (name, key, mode)
    return out


def keyfile_check(chart, cid, docs, label):
    """21. A key, not a password: the carrier sets no password and names the two key files; the ExternalSecret holds
    exactly the item's two keys; every container that starts one of the programs mounts both at those paths, the
    private key not world-readable and readable by the container's user."""
    f = RULES[cid]["keyfile"](chart)
    item = [i for i in ITEMS_REF.values() if i["id"] == cid][0]
    cname, ckey = f["carrier"]
    carrier = [d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"] == cname]
    text = object_text(carrier[0], ckey) if carrier else None
    if text is None:
        fail("21: %s%s: %s/%s is not rendered with the reference" % (chart, label, cname, ckey))
        return
    opts, section = {}, None
    for line in text.splitlines():
        t = line.strip()
        if t.startswith("[") and t.endswith("]"):
            section = t[1:-1]
        elif "=" in t and not t.startswith(("#", ";")) and section == f["section"]:
            k, _, val = t.partition("=")
            opts[k.strip()] = val.strip()
    if f["replaces"] in opts:
        fail("21: %s%s: %s [%s] still sets %s with the key referenced: the published password login survives"
             % (chart, label, ckey, f["section"], f["replaces"]))
    priv, pub = opts.get(f["private"]), opts.get(f["public"])
    if not priv or not pub:
        fail("21: %s%s: %s [%s] sets %s=%r and %s=%r; with the key referenced both must name the mounted files"
             % (chart, label, ckey, f["section"], f["private"], priv, f["public"], pub))
        return
    es = [d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == f["secret"]]
    if len(es) != 1:
        fail("21: %s%s: renders %d ExternalSecret(s) %s, want 1" % (chart, label, len(es), f["secret"]))
        return
    target = ((es[0]["spec"].get("target") or {}).get("name")) or f["secret"]
    files = {e.get("secretKey"): ((e.get("remoteRef") or {}).get("key"), (e.get("remoteRef") or {}).get("property"))
             for e in es[0]["spec"].get("data") or []}
    privf = [k for k, v in files.items() if v == (item["secret"], item["key"])]
    pubf = [k for k, v in files.items() if v == (item["secret"], item.get("publicKey"))]
    if len(files) != 2 or len(privf) != 1 or len(pubf) != 1:
        fail("21: %s%s: %s holds %s, want exactly the root %s's %s and %s" % (chart, label, f["secret"], files,
                                                                          item["secret"], item["key"], item.get("publicKey")))
        return
    readers = 0
    for podname, pod in pod_specs(docs):
        if podname.startswith("Pod/"):
            continue
        sc = pod.get("securityContext") or {}
        for cont in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            starts = set()
            for t in container_scripts(pod, cont, docs):
                for line in re.sub(r"\\\n\s*", " ", t).splitlines():
                    if not line.lstrip().startswith("#"):
                        starts |= set(OSLO_PROGRAM.findall(line.split("#", 1)[0])) & set(f["programs"])
            if not starts:
                continue
            readers += 1
            at = mounted_secret_files(pod, cont)
            where = "%s container %s (starts %s)" % (podname, cont["name"], ", ".join(sorted(starts)))
            for path, want, what in ((priv, privf[0], "private"), (pub, pubf[0], "public")):
                got = at.get(path)
                if got is None or got[:2] != (target, want):
                    fail("21: %s%s: %s does not mount %s's %s key (%s) at %s, where %s points (has %s)"
                         % (chart, label, where, target, what, want, path, f["private" if what == "private" else "public"],
                            got))
            got = at.get(priv)
            if got and got[:2] == (target, privf[0]):
                mode = got[2]
                uid = (cont.get("securityContext") or {}).get("runAsUser", sc.get("runAsUser"))
                if mode & 0o007:
                    fail("21: %s%s: %s mounts the private key with mode %o, readable or writable by others"
                         % (chart, label, where, mode))
                elif uid != 0 and "fsGroup" not in sc and not mode & 0o004:
                    fail("21: %s%s: %s runs as %s and mounts the private key %o without a pod fsGroup: the kubelet "
                         "leaves a Secret file owned by root, so the program cannot read it" % (chart, label, where,
                                                                                               uid, mode))
    if readers == 0:
        fail("21: %s%s: no container starts %s, so where the key must arrive cannot be determined"
             % (chart, label, ", ".join(f["programs"])))
    else:
        KEYFILE_CHECKED.append((chart, cid, label, readers))


def pod_template_refs(pod):
    """Secret names a pod spec references (volumes, projected sources, env, envFrom)."""
    refs = set()
    for v in pod.get("volumes") or []:
        if (v.get("secret") or {}).get("secretName"):
            refs.add(v["secret"]["secretName"])
        for src in (v.get("projected") or {}).get("sources") or []:
            if (src.get("secret") or {}).get("name"):
                refs.add(src["secret"]["name"])
    for c in (pod.get("containers") or []) + (pod.get("initContainers") or []):
        for e in c.get("env") or []:
            n = ((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
            if n:
                refs.add(n)
        for e in c.get("envFrom") or []:
            if (e.get("secretRef") or {}).get("name"):
                refs.add(e["secretRef"]["name"])
    return refs


READERS_CHECKED = []


def readers_restart(chart, cid, root, docs_without, docs_with):
    """18. Every long-running workload that reads an ExternalSecret projecting this item's root has a pod template
    that differs between the render WITHOUT this one item's reference and the render with it (every other
    reference set in both), so applying it restarts the workload onto the new value. Comparing against the
    defaults render instead would pass on any other item's change (measured: it survived removing the hash).
    0.3.63's ks-etc kept its name and nothing hashed it: nova, neutron and placement kept the old password in
    memory and locked their users."""
    es_names = set()
    for d in docs_with:
        if d.get("kind") == "ExternalSecret":
            keys = {(e.get("remoteRef") or {}).get("key") for e in (d.get("spec") or {}).get("data") or []}
            if root in keys:
                es_names.add(((d.get("spec") or {}).get("target") or {}).get("name") or d["metadata"]["name"])
    before = {(d["kind"], d["metadata"]["name"]): d for d in docs_without
              if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet")}
    for d in docs_with:
        if d.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
            continue
        t = (d.get("spec") or {}).get("template") or {}
        reads = pod_template_refs(t.get("spec") or {}) & es_names
        old = before.get((d["kind"], d["metadata"]["name"]))
        if not reads or old is None:
            continue
        READERS_CHECKED.append((chart, cid, d["metadata"]["name"]))
        if old["spec"]["template"] == t:
            fail("%s: %s/%s reads %s, which projects %s, but its pod template is identical with and without that "
                 "reference, so it would keep the previous value in memory (and a service user would be locked)"
                 % (chart, d["kind"], d["metadata"]["name"], sorted(reads), cid))


KS_USER_CHECKED = []


def ks_user_job_keys(chart):
    """The endpoints.identity.auth keys a chart's ks-user Job can create users for, read from its template:
    serviceName alone, or every key of serviceUsers (the tuple and each conditional append)."""
    keys = set()
    for f in glob.glob(os.path.join(BP, chart, "chart", "templates", "job-ks-user.yaml")):
        t = open(f).read()
        m = re.search(r'"serviceName" "([^"]+)"', t)
        lists = re.findall(r'\(tuple((?:\s+"[^"]+")+)\s*\)', t)
        if "serviceUsers" in t and lists:
            for l in lists:
                keys.update(re.findall(r'"([^"]+)"', l))
            keys.update(re.findall(r'append \$serviceUsers "([^"]+)"', t))
        elif m:
            keys.add(m.group(1))
    return keys


def ks_user_rotation(chart, cid, cv, root, docs):
    """17. The chart's ks-user Job that creates this user runs pre-upgrade, reads the password from the root, and
    exempts the user from lockout before setting the password. A pre-upgrade hook runs before the release's
    resources are applied, so the Job may read nothing the release renders except what exists since the first
    install (the admin openrc and the TLS secret): no ConfigMap (the previous revision's <svc>-bin lacks the
    exemption), no user openrc (absent for a user the upgrade enables)."""
    u = IDUSER_BY_ITEM[cid]
    key = IDUSERS[u].get(chart, "gate_unmapped")
    ident = (cv.get("secrets") or {}).get("identity") or {}
    openrc = ident.get(key)
    found = False
    for d in docs:
        if d.get("kind") != "Job" or "ks-user" not in d["metadata"]["name"]:
            continue
        pod = d["spec"]["template"]["spec"]
        for c in pod.get("containers") or []:
            env = {e["name"]: e for e in c.get("env") or []}
            un = env.get("SERVICE_OS_USERNAME") or {}
            ref = ((un.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
            # by the literal username (rotate mode) or by the user's openrc Secret (values mode); a chart without
            # an openrc for this user (cyborg has none for nova) must not match a literal of another user
            if un.get("value") != u and not (openrc and ref == openrc):
                continue
            found = True
            job = d["metadata"]["name"]
            hook = d["metadata"].get("annotations", {}).get("helm.sh/hook")
            if hook != "post-install,pre-upgrade":
                fail("%s: Job %s creates user %s with hook %r; it must be post-install,pre-upgrade so the new password "
                     "is in Keystone before the readers restart" % (chart, job, u, hook))
            pw = ((env.get("SERVICE_OS_PASSWORD") or {}).get("valueFrom") or {}).get("secretKeyRef") or {}
            if pw.get("name") != root:
                fail("%s: Job %s sets user %s's password from %s, not the root %s (on a pre-upgrade hook the projection "
                     "is not applied yet)" % (chart, job, u, pw.get("name"), root))
            allowed = {root, ident.get("admin")} | {(v.get("secret") or {}).get("secretName") for v in pod.get("volumes") or []
                                                    if "tls" in v.get("name", "")}
            used = pod_template_refs({"containers": [c], "volumes": pod.get("volumes")})
            cms = [v["configMap"]["name"] for v in pod.get("volumes") or [] if v.get("configMap")]
            if (used - allowed) or cms:
                fail("%s: pre-upgrade Job %s reads %s, rendered by this release (a pre-upgrade hook sees the previous "
                     "revision's, or none for a user the upgrade enables)" % (chart, job, sorted((used - allowed) | set(cms))))
            for ic in pod.get("initContainers") or []:
                ienv = {e["name"]: e.get("value") for e in ic.get("env") or []}
                if (ienv.get("DEPENDENCY_JOBS") or "").strip():
                    fail("%s: pre-upgrade Job %s waits on Job(s) %s; on an upgrade those run after it (post-upgrade) or "
                         "are the previous revision's" % (chart, job, ienv["DEPENDENCY_JOBS"]))
            cmd = c.get("command") or []
            script = cmd[-1] if len(cmd) == 3 and cmd[:2] == ["/bin/bash", "-c"] else ""
            # the command itself (a line that runs it), not the echo that announces it with a masked value
            ex = script.find("--ignore-lockout-failure-attempts")
            runs = [m.start() for m in re.finditer(r"(?m)^[ \t]*openstack user set --password=", script)]
            setpw = runs[-1] if runs else -1
            if not (0 <= ex < setpw):
                fail("%s: Job %s's inline script must exempt user %s from lockout (--ignore-lockout-failure-attempts) "
                     "before it sets the password" % (chart, job, u))
            # The password changes LAST: every reader still holding the previous value fails from that moment until it
            # restarts, which is after this Job completes. Any openstack command after it (role assignments took 1 to
            # 3 minutes per user) widens that window for nothing (measured on 0.3.65: about 221 failed logins).
            if setpw >= 0:
                after = [l.strip() for l in script[setpw:].splitlines()[1:]
                         if l.strip() and not l.strip().startswith("#")]
                later = [l for l in after if re.search(r"(^|[\s$(])openstack\s", l)]
                if later:
                    fail("%s: Job %s's inline script runs %d openstack command(s) after it sets user %s's password "
                         "(first: %r); the password must change last, so readers fail only until they restart"
                         % (chart, job, len(later), u, later[0][:80]))
    if found:
        KS_USER_CHECKED.append((chart, u))


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


ITEMS_REF = {}


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
    # `source: provided` items (#308) are copies of keys Ceph mints, rendered only with a ceph storage.backend: they have
    # no generator, no published default to replace, and no root at the umbrella's defaults. hack/validate-ceph-consumer.py
    # checks them (27 provided roots, 22c readers, 18c restarts, 28 no client.admin, 29 consistency); every check here is
    # about generated roots, so they are set aside -- from the items and from each component's list -- and named.
    provided_ids = sorted(cid for cid, i in items.items() if i.get("source"))
    # Their roots, though: cinder is rendered as the umbrella delivers it (hack/gate_values.py, #308 step 4), ceph-managed,
    # so its keyring and ceph.conf projections read these roots, and a known root is what they must read.
    provided_roots = {i["secret"]: i for i in items.values() if i.get("source")}
    items = {cid: i for cid, i in items.items() if not i.get("source")}
    components = [dict(c, credentials=[x for x in (c.get("credentials") or []) if x not in provided_ids])
                  for c in (uv.get("components") or [])]
    if provided_ids:
        print("set aside, checked by hack/validate-ceph-consumer.py: %s" % ", ".join(provided_ids))
    ITEMS_REF.update(items)
    for cid in IDUSER_BY_ITEM:
        if cid in items:
            for c in components:
                if cid in (c.get("credentials") or []):
                    want = "endpoints.identity.auth.%s.passwordFrom" % IDUSERS[IDUSER_BY_ITEM[cid]].get(c["name"], "?")
                    got = (items[cid].get("pathFor") or {}).get(c["name"], items[cid]["path"])
                    if got != want:
                        fail("umbrella: %s reaches %s at %s, but that chart holds user %s under %s"
                             % (cid, c["name"], got, IDUSER_BY_ITEM[cid], want))
    # 15b. a MariaDB user's item reaches exactly the keys the chart renders that username under: one account, one root
    for cid in DBUSER_BY_ITEM:
        if cid not in items:
            continue
        for c in components:
            if cid in (c.get("credentials") or []):
                want = sorted("endpoints.%s.auth.%s.passwordFrom" % k for k in db_keys(c["name"], DBUSER_BY_ITEM[cid]))
                got = sorted([(items[cid].get("pathFor") or {}).get(c["name"], items[cid]["path"])]
                             + list(items[cid].get("extraPaths") or []))
                if got != want:
                    fail("umbrella: %s reaches %s at %s, but that chart renders MariaDB user %s under %s (one account "
                         "has one password: every key, one root)" % (cid, c["name"], got, DBUSER_BY_ITEM[cid], want))
    # 15c. ONE RABBITMQ ACCOUNT, ONE ROOT: a RabbitMQ user's item reaches every chart rendering that username at the key the
    # chart renders it under (closure, check 2, makes it every such chart), and no other item reaches any of those keys:
    # two roots for one username would have each chart's rabbit-init set its own in turn, the last winning and every
    # other reader refused.
    reached = {}
    for c in components:
        for cid in c.get("credentials") or []:
            i = items.get(cid)
            if not i:
                continue
            for p in [(i.get("pathFor") or {}).get(c["name"], i["path"])] + list(i.get("extraPaths") or []):
                m = re.match(r"^endpoints\.oslo_messaging\.auth\.([^.]+)\.passwordFrom$", p)
                if m and m.group(1) not in MQ_NOT_SERVICE:
                    reached.setdefault((c["name"], m.group(1)), set()).add(cid)
    for (chart, key), cids in sorted(reached.items()):
        users = [u for u, cs in MQUSERS.items() if (cs or {}).get(chart) == key]
        if not users:
            fail("umbrella: %s reach %s at endpoints.oslo_messaging.auth.%s, which hack/mq-users.yaml does not map to a "
                 "RabbitMQ username" % (sorted(cids), chart, key))
            continue
        want = {"oslo-messaging-" + users[0].replace("_", "-")}
        if cids != want:
            fail("umbrella: %s's RabbitMQ user %s (endpoints.oslo_messaging.auth.%s) is reached by %s, want exactly %s: "
                 "one username, one root" % (chart, users[0], key, sorted(cids), sorted(want)))
    for cid, u in MQUSER_BY_ITEM.items():
        if cid in items:
            for chart, key in sorted((MQUSERS[u] or {}).items()):
                if (chart, key) not in reached:
                    fail("umbrella: %s renders RabbitMQ user %s under endpoints.oslo_messaging.auth.%s, but %s does not reach "
                         "it there" % (chart, u, key, cid))

    def refs_for(c, skip=None):
        out = {}
        for cid in c.get("credentials") or []:
            if cid == skip:
                continue
            i = items[cid]
            path = (i.get("pathFor") or {}).get(c["name"], i["path"])
            ref = {"store": creds["store"], "secret": i["secret"], "key": i["key"]}
            if i.get("publicKey"):
                ref["publicKey"] = i["publicKey"]  # an SSHKey item's root holds the public half under this key
            for p in [path] + list(i.get("extraPaths") or []):  # one credential under several keys: one root
                deep_merge(out, nested(p, dict(ref)))
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
        # monitoring on: a `monitoring: true` item's root (identity-user-openstack-exporter, #312) renders only then, and
        # every root must be checked. hack/validate-openstack-monitoring.py checks that it is absent with monitoring off.
        rc, out, err = helm_template(u, "openstack-gate", {"componentValues": {"keystone": override},
                                                           "monitoring": {"enabled": True}})
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
        # 21. each root is generated by its item's kind, and holds exactly the keys the item (and so the reference) names
        gen_names = {d["kind"]: d["metadata"]["name"] for d in yaml.safe_load_all(out)
                     if d and d.get("apiVersion") == "generators.external-secrets.io/v1alpha1"}
        for d in roots:
            i = [x for x in items.values() if x["secret"] == d["metadata"]["name"]]
            if not i:
                continue
            i = i[0]
            kind = i.get("generator") or "Password"
            refs = [((s.get("sourceRef") or {}).get("generatorRef") or {}) for s in d["spec"].get("dataFrom") or []]
            if [(r.get("kind"), r.get("name")) for r in refs] != [(kind, gen_names.get(kind, "<none rendered>"))]:
                fail("umbrella: root %s is generated by %s, want the %s generator %s" % (
                    d["metadata"]["name"], [(r.get("kind"), r.get("name")) for r in refs], kind, gen_names.get(kind)))
            keys = sorted((((d["spec"].get("target") or {}).get("template") or {}).get("data") or {}))
            want = sorted([i["key"]] + ([i["publicKey"]] if i.get("publicKey") else []))
            if keys != want:
                fail("umbrella: root %s holds %s, want %s (what the reference passes)" % (d["metadata"]["name"], keys, want))

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
            if cid in IDUSER_BY_ITEM and cid not in items:
                continue  # service users ship in releases (0.3.63, 0.3.64); a username without an item is not yet rotated
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
            config_dir_check(c["name"], docs)
            chart_values = yaml.safe_load(open(os.path.join(chart_dir, "values.yaml")))
            no_published_literal(c["name"], published_secrets(chart_values), docs, admin_usernames(chart_values),
                                 {u for u, cs in DBUSERS.items() if c["name"] in cs},
                                 {u for u, cs in MQUSERS.items() if c["name"] in cs})
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
                    item = root_secrets.get(ref.get("key")) or provided_roots.get(ref.get("key"))
                    if item is None or ref.get("property") not in (item["key"], item.get("publicKey") or item["key"]):
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
                    refs = [(e.get("remoteRef") or {}).get("key") for e in spec.get("data") or []]
                    if refs and all(r in provided_roots for r in refs):
                        continue  # a projection of a key Ceph minted: no values-mode counterpart; validate-ceph-consumer P
                    src = plain.get(RENAMED.get(name, name))
                    if src is None:
                        fileread = {RULES[x]["fileread"](c["name"])["secret"] for x in cids if "fileread" in RULES[x]}
                        if name in fileread:
                            continue  # a raw key file read by a config line: check 14 validates it
                        if name in {RULES[x]["keyfile"](c["name"])["secret"] for x in cids if "keyfile" in RULES[x]}:
                            continue  # a key pair: no values-mode counterpart, check 21 validates it
                        if len(tdata) == 1 and list(tdata)[0].endswith((".conf", ".py")):
                            continue  # a keystone_password_conf_snippet drop-in: no Secret counterpart
                        if len(tdata) == 1 and any(list(tdata)[0] in (p.get("data") or {}) for p in plain.values()):
                            continue  # a file relocated out of a values-mode Secret (skyline.yaml): check 16b
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
                            # a password inside a URL is URL-quoted, as values mode quotes it (Go's url.QueryEscape)
                            v = v.replace("{{ .%s }}" % sk, pw).replace("{{ .%s | urlquery }}" % sk,
                                                                          urllib.parse.quote_plus(pw))
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
                config_dir_defaults(c["name"], docs0)
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
            base = refs_for(c)
            deep_merge(base, json.loads(json.dumps(enabled)))
            rcb, outb, errb = helm_template(w, release, base)
            for cid in cids:
                if rcb != 0:
                    break
                minus = refs_for(c, skip=cid)
                deep_merge(minus, json.loads(json.dumps(enabled)))
                rcm, outm, errm = helm_template(w, release, minus)
                if rcm != 0:
                    fail("%s: does not render with every reference but %s: %s" % (c["name"], cid, errm.strip()[:200]))
                    continue
                readers_restart(c["name"], cid, items[cid]["secret"], [d for d in yaml.safe_load_all(outm) if d],
                                [d for d in yaml.safe_load_all(outb) if d])
            def others_for(cid):
                """{root of every other credential the chart receives: the value values mode renders for it}"""
                out = {}
                for x in cids:
                    if x == cid:
                        continue
                    node = cv
                    for part in paths(RULES[x]["password_path"](c["name"]))[0].split("."):
                        node = node.get(part) if isinstance(node, dict) else None
                    if isinstance(node, (str, int)):
                        out[items[x]["secret"]] = str(node)
                return out
            dbinit_check(c["name"], docs, tmp)
            for cid in cids:
                if RULES[cid].get("db"):
                    vsx = json.loads(json.dumps(enabled))
                    for pp in paths(RULES[cid]["password_path"](c["name"])):
                        deep_merge(vsx, nested(pp, sentinels[cid]))
                    rcx, outx, errx = helm_template(w, release, vsx)
                    if rcx != 0:
                        fail("%s: does not render in values mode with the %s sentinel: %s" % (c["name"], cid, errx.strip()[:200]))
                        continue
                    dsx = [d for d in yaml.safe_load_all(outx) if d]
                    relocated_files(c["name"], cid, sentinels[cid], dsx, docs, others_for(cid))
                    if c["name"] == "nova":
                        cell_mapping_check(dsx, docs, sentinels[cid])
                    if db_dropin_for(c["name"]):
                        db_dropin_check(c["name"], cid, sentinels[cid], dsx, docs)
                    elif c["name"] in DB_FILEREAD:
                        DB_DROPIN_CHECKED.append((c["name"], cid, "fileread (check 14)", 1))
                    elif c["name"] in DB_RELOCATED:
                        DB_DROPIN_CHECKED.append((c["name"], cid, "relocated file (check 16b)", 1))
                    elif c["name"] in DB_PROJECTION_ONLY:
                        DB_DROPIN_CHECKED.append((c["name"], cid, "projection only", 1))
                    else:
                        fail("22: %s renders MariaDB user %s but lists no database URL drop-in entries "
                             "(templates/_db_dropin_entries.tpl) and is no known special case" % (c["name"], DBUSER_BY_ITEM[cid]))
                if RULES[cid].get("mq"):
                    vsx = json.loads(json.dumps(enabled))
                    for pp in paths(RULES[cid]["password_path"](c["name"])):
                        deep_merge(vsx, nested(pp, sentinels[cid]))
                    rcx, outx, errx = helm_template(w, release, vsx)
                    if rcx != 0:
                        fail("%s: does not render in values mode with the %s sentinel: %s" % (c["name"], cid, errx.strip()[:200]))
                    else:
                        mq_dropin_check(c["name"], cid, sentinels[cid], [d for d in yaml.safe_load_all(outx) if d], docs)
                    rabbit_init_check(c["name"], cid, docs, tmp)
                    # 22m: a literal URL in values together with the reference is refused (it carries a password, and the
                    # drop-in would replace it silently)
                    vlit = json.loads(json.dumps(values))
                    deep_merge(vlit, {"conf": {c["name"]: {"DEFAULT": {"transport_url": "rabbit://gate:Literal7f3c@h:5672/v"}}}})
                    rcl, _, errl = helm_template(w, release, vlit)
                    if rcl == 0 or "conf.%s.DEFAULT.transport_url is set while" % c["name"] not in errl:
                        fail("22m: %s: a literal conf.%s.DEFAULT.transport_url renders together with %s's reference (rc=%d); "
                             "it must be refused" % (c["name"], c["name"], cid, rcl))
                    elif "Literal7f3c" in errl:
                        fail("22m: %s: the refusal of a literal transport_url prints its password" % c["name"])
                if RULES[cid].get("carriers"):
                    ks_user_rotation(c["name"], cid, cv, items[cid]["secret"], docs)
                    vsx = json.loads(json.dumps(enabled))
                    for pp in paths(RULES[cid]["password_path"](c["name"])):
                        deep_merge(vsx, nested(pp, sentinels[cid]))
                    rcx, outx, errx = helm_template(w, release, vsx)
                    if rcx == 0:
                        dsx = [d for d in yaml.safe_load_all(outx) if d]
                        identity_carriers(c["name"], cid, sentinels[cid], dsx, docs)
                        relocated_files(c["name"], cid, sentinels[cid], dsx, docs, others_for(cid))
                        di = RULES[cid]["dropin_for"](c["name"])
                        if di:
                            RULES[cid]["dropin"] = lambda chart, di=di: di
                            dropin_delivery(c["name"], cid, sentinels[cid], dsx, docs)
                            RULES[cid].pop("dropin", None)
                    else:
                        fail("%s: does not render in values mode with the %s sentinel: %s" % (c["name"], cid, errx.strip()[:200]))
                if "fileread" in RULES[cid]:
                    fileread_check(c["name"], cid, docs)
                if "keyfile" in RULES[cid]:
                    keyfile_check(c["name"], cid, docs, "")
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
            for cid in cids:
                for variant in (RULES[cid].get("variants") or (lambda ch: []))(c["name"]):
                    vv = json.loads(json.dumps(values))
                    deep_merge(vv, json.loads(json.dumps(variant)))
                    rcv, outv, errv = helm_template(w, release, vv)
                    if rcv != 0:
                        fail("%s: does not render with %s and %s: %s" % (c["name"], cid, variant, errv.strip()[:200]))
                        continue
                    docsv = [d for d in yaml.safe_load_all(outv) if d]
                    leaked = sentinels[cid] in outv or any(
                        d.get("kind") == "Secret" and any(sentinels[cid] in (object_text(d, k) or "")
                                                          for k in (d.get("data") or {})) for d in docsv)
                    if leaked:
                        fail("%s: with %s the %s value from values is still rendered" % (c["name"], variant, cid))
                    if "keyfile" in RULES[cid]:
                        keyfile_check(c["name"], cid, docsv, " (with %s)" % json.dumps(variant, sort_keys=True))
                    if RULES[cid].get("db") and db_dropin_for(c["name"]):
                        vs = json.loads(json.dumps(enabled))
                        deep_merge(vs, json.loads(json.dumps(variant)))
                        for pp in paths(RULES[cid]["password_path"](c["name"])):
                            deep_merge(vs, nested(pp, sentinels[cid]))
                        rcs, outs, errs = helm_template(w, release, vs)
                        if rcs != 0:
                            fail("%s: does not render with %s and the %s sentinel: %s" % (c["name"], variant, cid, errs.strip()[:200]))
                        else:
                            dsv = [d for d in yaml.safe_load_all(outs) if d]
                            db_dropin_check(c["name"], cid, sentinels[cid], dsv, docsv,
                                            " (with %s)" % json.dumps(variant, sort_keys=True))
                            if c["name"] == "nova":
                                cell_mapping_check(dsv, docsv, sentinels[cid], " (with %s)" % json.dumps(variant, sort_keys=True))
                    if RULES[cid].get("db"):
                        dbinit_check(c["name"], docsv, tmp)
                    if RULES[cid].get("mq"):
                        vlabel = " (with %s)" % json.dumps(variant, sort_keys=True)
                        vs = json.loads(json.dumps(enabled))
                        deep_merge(vs, json.loads(json.dumps(variant)))
                        for pp in paths(RULES[cid]["password_path"](c["name"])):
                            deep_merge(vs, nested(pp, sentinels[cid]))
                        rcs, outs, errs = helm_template(w, release, vs)
                        if rcs != 0:
                            fail("%s: does not render with %s and the %s sentinel: %s" % (c["name"], variant, cid, errs.strip()[:200]))
                        else:
                            mq_dropin_check(c["name"], cid, sentinels[cid], [d for d in yaml.safe_load_all(outs) if d], docsv,
                                            vlabel)
                        rabbit_init_check(c["name"], cid, docsv, tmp, vlabel)
                    if RULES[cid].get("carriers"):
                        ks_user_rotation(c["name"], cid, cv, items[cid]["secret"], docsv)
                        di = RULES[cid]["dropin_for"](c["name"])
                        if di:
                            vs = json.loads(json.dumps(enabled))
                            deep_merge(vs, json.loads(json.dumps(variant)))
                            for pp in paths(RULES[cid]["password_path"](c["name"])):
                                deep_merge(vs, nested(pp, sentinels[cid]))
                            rcs, outs, errs = helm_template(w, release, vs)
                            if rcs != 0:
                                fail("%s: does not render with %s and the %s sentinel: %s" % (c["name"], variant, cid, errs.strip()[:200]))
                            else:
                                RULES[cid]["dropin"] = lambda chart, di=di: di
                                dropin_delivery(c["name"], cid, sentinels[cid], [d for d in yaml.safe_load_all(outs) if d], docsv)
                                RULES[cid].pop("dropin", None)
                    if "dropin" in RULES[cid]:
                        vs = json.loads(json.dumps(enabled))
                        deep_merge(vs, json.loads(json.dumps(variant)))
                        for pp in paths(RULES[cid]["password_path"](c["name"])):
                            deep_merge(vs, nested(pp, sentinels[cid]))
                        rcs, outs, errs = helm_template(w, release, vs)
                        if rcs == 0:
                            dropin_delivery(c["name"], cid, sentinels[cid], [d for d in yaml.safe_load_all(outs) if d], docsv)
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
        identity_map_check(tmp)
        db_map_check(tmp)
        one_account_check(tmp)
        mq_map_check(tmp)
        notification_vhosts_check(tmp, components)
        init_confd_survey(tmp)
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

    if checked and not READERS_CHECKED:
        fail("18: no workload reading a credential projection was found; the check had no input")
    # every rotated user a ks-user Job can create must have been matched in some render (a mode that only a
    # condition selects needs an IDUSER_VARIANTS entry, or the user's rotation goes unchecked)
    for cid, u in IDUSER_BY_ITEM.items():
        if cid not in items:
            continue
        for chart, key in IDUSERS[u].items():
            if key in ks_user_job_keys(chart) and (chart, u) not in KS_USER_CHECKED:
                fail("17: %s's ks-user Job can create user %s (key %s) but no render matched it; add the mode that "
                     "renders it to IDUSER_VARIANTS" % (chart, u, key))
    if checked and not KS_USER_CHECKED:
        fail("17: no chart's ks-user Job was matched to a rotated service user; the check had no input")
    for cid, rule in RULES.items():
        if "keyfile" in rule and cid in items and not any(k[1] == cid for k in KEYFILE_CHECKED):
            fail("21: no render checked where %s's key files arrive; the check had no input" % cid)
    if checked and not URLS_CHECKED:
        fail("20b: no URL logging in as an install-wide administrator was found in any rendered chart; the check had "
             "no input")
    if checked and not DB_URLS_CHECKED:
        fail("20b: no mysql URL logging in as a per-service MariaDB user was found in any rendered chart; the check had "
             "no input")
    if not DBUSERS:
        fail("15b/22: hack/db-users.yaml lists no per-service MariaDB user, so no database URL was checked")
    for cid, rule in RULES.items():
        if rule.get("db") and cid in items and not any(x[1] == cid for x in DB_DROPIN_CHECKED):
            fail("22: no render checked where %s's database URLs arrive; the check had no input" % cid)
    for chart, spec in sorted(DB_READERS.items()):
        if not (spec or {}).get("readers"):
            fail("22: hack/db-readers.yaml lists %s with no readers; the check had no input" % chart)
        for cname in (spec or {}).get("readers") or []:
            if checked and (chart, cname) not in DB_READERS_SEEN:
                fail("22: hack/db-readers.yaml lists %s container %s, which no render shows reading the chart's config; "
                     "remove it or render the mode that runs it" % (chart, cname))
    if not MQUSERS:
        fail("15c/22m: hack/mq-users.yaml lists no per-service RabbitMQ user (or is missing), so no RabbitMQ URL was checked")
    if not MQ_READERS:
        fail("22m: hack/mq-readers.yaml lists no chart (or is missing), so who receives a RabbitMQ URL cannot be checked")
    if checked and not MQ_URLS_CHECKED:
        fail("20b: no RabbitMQ URL logging in as a per-service RabbitMQ user was found in any rendered chart; the check had "
             "no input")
    for cid, rule in RULES.items():
        if rule.get("mq") and cid in items and not any(x[1] == cid for x in MQ_DROPIN_CHECKED):
            fail("22m: no render checked where %s's RabbitMQ URLs arrive; the check had no input" % cid)
    for cid, u in MQUSER_BY_ITEM.items():
        if cid not in items:
            fail("15c: hack/mq-users.yaml lists RabbitMQ user %s, but the umbrella defines no item %s: every chart rendering "
                 "it keeps the published password" % (u, cid))
    for chart, spec in sorted(MQ_READERS.items()):
        for cname in (spec or {}).get("readers") or []:
            if checked and (chart, cname) not in MQ_READERS_SEEN:
                fail("22m: hack/mq-readers.yaml lists %s container %s, which no render shows reading the chart's config; "
                     "remove it or render the mode that runs it" % (chart, cname))
    if not INIT_CONFD_CHECKED:
        fail("19c: no init container that starts an OpenStack program and mounts its project's main config was found in "
             "any chart; the check had no input")
    if checked and not MQ_PROBES_CHECKED:
        fail("22m: no exec probe parsing a config that carried a transport URL was found; the probe check had no input")
    if checked and not RABBIT_INIT_CHECKED:
        fail("23m: no chart's rabbit-init.sh was run; the check had no input")
    if not [x for x in VHOST_JOBS_CHECKED if x[1]]:
        fail("26b: no case rendered a notification consumer that waits for another component's rabbit-init; the check "
             "had no input")
    if not [x for x in VHOST_CASES_CHECKED if x[1]]:
        fail("26: no case rendered the notification consumer's vhost list; the check had no input")
    if len([x for x in VHOST_CASES_CHECKED if x[1] and x[0].startswith("full-strict, ")]) != 5:
        fail("26: the componentValues override cases did not all render the consumer's vhost list; the check had no input")
    if "oslo-db-nova" in items and checked and not CELLMAP_CHECKED:
        fail("24: no render checked nova's cell mapping templates; the check had no input")
    if checked and not DBINIT_CHECKED:
        fail("23: no chart's db-init.py was run; the check had no input")
    if checked and not LITERALS_CHECKED:
        fail("20: no password-like ini option was found in any rendered chart; the check had no input")
    if checked and not CONFIG_DIR_CHECKED:
        fail("19: no OpenStack program was found in any container that mounts a .conf.d; the check had no input")
    seen = {p for _, _, _, p in CONFIG_DIR_CHECKED}
    for stale in sorted((CONFIG_DIR_ACCEPTS | set(CONFIG_DIR_REJECTS)) - seen):
        fail("19: hack/oslo-config-dir-programs.yaml lists %s, which no chart starts in a container with a .conf.d; "
             "remove it (a list that keeps entries nothing checks stops meaning anything)" % stale)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: %d credential item(s); %d chart(s) rendered with references carry no admin password from values; "
          "umbrella references, closure, roots, the admin lockout exemption and teardown with the release consistent; "
          "%d service-user ks-user Job(s) rotate pre-upgrade from the root with the exemption first; %d (workload, item) "
          "reader pair(s) restart when that item's reference is applied; %d program start(s) in a container with a "
          "conf.d pass --config-dir; %d per-service MariaDB user URL(s) carry no published password; %d database URL "
          "delivery check(s); %d db-init/db-drop run(s) set and drop the drop-in's user; %d per-service RabbitMQ URL(s) "
          "carry no published password; %d transport URL delivery check(s); %d rabbit-init run(s) set the root's "
          "password; %d exec probe(s) that parse a carrier read the drop-in; %d notification-vhost case(s)"
          % (len(items), checked, len(KS_USER_CHECKED), len(READERS_CHECKED), len(CONFIG_DIR_CHECKED),
             len(DB_URLS_CHECKED), len(DB_DROPIN_CHECKED), len(DBINIT_CHECKED), len(MQ_URLS_CHECKED),
             len(MQ_DROPIN_CHECKED), len(RABBIT_INIT_CHECKED), len(MQ_PROBES_CHECKED), len(VHOST_CASES_CHECKED)))
    for chart, podname, cname, conf in sorted(set(INIT_CONFD_CHECKED)):
        print("  19c: %s: %s init container %s mounts %s and %s.d" % (chart, podname, cname, conf, conf))
    for label, jobs in VHOST_JOBS_CHECKED:
        print("  26b: %s: ceilometer-notification also waits for %s" % (label, ", ".join(jobs) or "nothing"))
    for label, listed in VHOST_CASES_CHECKED:
        print("  26: %s: ceilometer consumes %s" % (label, ", ".join(listed) if listed else "(not selected)"))


main()
