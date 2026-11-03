#!/usr/bin/env python3
"""A component's rendered config may only reference its declared dependencies.

hack/validate-component-dag.py already proves the DAG is closed, acyclic and renderable.
None of that looks at what a component's rendered config actually *points at*, and that gap
is how issue #115 shipped: gnocchi declared `deps: [keystone, mariadb]` while its rendered
gnocchi.conf said `[indexer] driver = postgresql` and `[storage] driver = ceph`, and its
kubernetes-entrypoint init containers waited on a Service named `postgresql`. This blueprint
has no postgres component and no ceph component -- no blueprint directory, no
CompositionDefinition, nothing in the catalogue -- so on a real 4-node GKE install of the
full profile gnocchi-db-init-indexer, gnocchi-db-sync, 4x metricd and 4x statsd sat in Init
forever reporting `Service postgresql ... has no ready endpoints`, while gnocchi-db-init had
already created the mariadb database and Completed.

Every gate in this repo was green throughout. The DAG was closed and acyclic, the chart
rendered, the images pulled. What nothing checked was whether the config agreed with the
graph.

Two checks, both read from the RENDERED output rather than from values.yaml, because what
matters is the object the cluster acts on:

 1. kubernetes-entrypoint service waits. DEPENDENCY_SERVICE is a comma-separated list of
    `namespace:service-name`. Every name must be a Service that some component in this
    catalogue actually renders -- a name nothing serves can never become ready, and
    kubernetes-entrypoint cannot tell "not ready yet" from "will never exist", so it blocks
    with no error -- and the component that renders it must be in the waiting component's
    declared TRANSITIVE closure, or itself. Transitive matters: placement declares only
    keystone yet legitimately opens a mysql connection, because keystone depends on mariadb.
    This check needs no hand-maintained table: the Service -> component map is built from
    the renders themselves. An EMPTY ELEMENT of a non-empty list counts too, and is
    reported rather than skipped: a dependencies.static entry naming an endpoint the
    chart's values do not define renders as nothing between two commas, and
    kubernetes-entrypoint's util/env.SplitEnvToDeps does not drop it -- it registers a
    dependency on the Service named "", whose Endpoints Get fails with `resource name may
    not be empty` on every pass, forever. ceilometer waited that way on `oslo_db`, in a
    chart that defines no endpoints.oslo_db and whose rendered ceilometer.conf has no
    [database] section at all, and skipping the empty element is what kept it invisible
    here while three of its pods sat in Init (#130). An entirely empty DEPENDENCY_SERVICE
    is a different thing and not a finding: SplitEnvToDeps returns before splitting when
    the value is "", so nothing is registered and nothing blocks. Both halves were
    measured against a real API server with the image's own build before this check was
    written -- "" gives 0 dependencies, "a,,b" gives 3, the middle one named "".

 2. Backend identities in the rendered config. A `driver = <token>` selection or a
    `<key>url/uri/connection = <scheme>://...` in an oslo config names a backend. If the
    token names a backend no component in this catalogue provides, that is a dependency on
    something that cannot exist here. If it names one a component does provide, that
    component must be in the declared closure.

Deliberately NOT checked, to stay narrow rather than noisy:

 *  Keys like `volume_driver`, `firewall_driver`, `share_driver`, `compute_driver`,
    `backup_driver`. Their values are dotted Python class paths
    (cinder.volume.drivers.rbd.RBDDriver, cinder.backup.drivers.swift.SwiftBackupDriver,
    manila.share.drivers.generic.GenericShareDriver), and a class path is a different kind
    of reference from a backend identity -- resolving one would mean knowing which driver
    classes dial out. cinder's RBDDriver default is a real instance of this bug class that
    this gate does not see; it wants its own change, not a looser gate here.
 *  `http://` service URLs. Those are covered, more precisely, by check 1.
 *  Tokens outside BACKENDS. An OpenStack config is full of `driver =` keys that select a
    code path rather than a server (`messagingv2`, `noop`, `sqlalchemy`, `ping,http_ping`).
    Failing on those would bury the real findings.

Declared gaps live in hack/config-closure-gaps.yaml, with a reason and a tracking issue, and
every active one is printed on success so that a suppression cannot go quiet.
"""
import base64
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
UMBRELLA = ROOT / "blueprints/openstack/chart/values.yaml"
TOOLKIT = ROOT / "charts/helm-toolkit"
GAPS = ROOT / "hack/config-closure-gaps.yaml"

# Backends this blueprint's components provide, keyed by the token that names them in a
# driver selection or a URL scheme.
PROVIDED_BY = {
    "mysql": "mariadb",
    "mysql+pymysql": "mariadb",
    "mariadb": "mariadb",
    "swift": "swift",
    "memcache": "memcached",
    "memcached": "memcached",
    "rabbit": "rabbitmq",
    "rabbitmq": "rabbitmq",
    "amqp": "rabbitmq",
    # #131 added a redis component (vendored openstack-helm/redis) because zaqar's message
    # store is redis and nothing here provided one. `rediss` is the TLS scheme for the same
    # server; the chart serves plaintext today, so a component configured against rediss is
    # still pointed at this component and still has to declare it -- the dependency is the
    # same whether or not the transport is wrapped.
    "redis": "redis",
    "rediss": "redis",
}

# Backends that name a server no component here runs. A component configured against one of
# these waits on, or dials, something that does not exist in this install.
NO_COMPONENT_PROVIDES = {
    "postgres", "postgresql", "postgresql+psycopg2",
    "ceph", "rbd",
    "s3", "cassandra", "influxdb", "elasticsearch", "opensearch", "etcd", "mongodb",
}

# Backends that are a local file or an in-process store, so they need no component at all.
NEEDS_NO_SERVER = {"file", "dummy", "fake", "noop", "memory", "sqlite", "local"}

BACKENDS = set(PROVIDED_BY) | NO_COMPONENT_PROVIDES | NEEDS_NO_SERVER

# `driver = <bare token>` only: a value with a dot, comma or slash is a class path or a list,
# which this gate deliberately does not interpret (see the module docstring).
DRIVER_LINE = re.compile(r"^\s*driver\s*=\s*([A-Za-z0-9_+-]+)\s*$")
# A connection URL under any key ending in url / uri / connection.
URL_LINE = re.compile(
    r"^\s*(?:[a-z0-9_]*_)?(?:url|uri|connection)\s*=\s*([A-Za-z0-9+_.-]+)://"
)
# oslo.messaging's transport_url is the one URL in these configs that is declared without
# necessarily being dialled: oslo.messaging builds a transport only when something asks for
# one, so with `[oslo_messaging_notifications] driver = noop` and no RPC server the
# rabbit:// URL is inert. keystone and glance both ship exactly that and both install in the
# identity profile, which has no rabbitmq at all -- glance's values.yaml records the same
# finding from the other side (#110 removed glance-api's rabbit *wait* for this reason).
# Treating their transport_url as a dependency would therefore be a false positive, so it is
# checked only where the notification driver says a transport will actually be built.
TRANSPORT_LINE = re.compile(r"^\s*transport_url\s*=\s*([A-Za-z0-9+_.-]+)://")
NOTIFICATION_DRIVER_LINE = re.compile(r"^\s*driver\s*=\s*([A-Za-z0-9_+,-]+)\s*$")
CONF_SUFFIXES = (".conf", ".ini")

POD_SPEC_AT = {
    "Deployment": lambda d: d["spec"]["template"]["spec"],
    "DaemonSet": lambda d: d["spec"]["template"]["spec"],
    "StatefulSet": lambda d: d["spec"]["template"]["spec"],
    "Job": lambda d: d["spec"]["template"]["spec"],
    "CronJob": lambda d: d["spec"]["jobTemplate"]["spec"]["template"]["spec"],
    "Pod": lambda d: d["spec"],
}


def measurement_failed(msg):
    # Exit 2 and never 0. Four gates in this repo printed "SKIP: PyYAML unavailable" and
    # exited 0 for weeks (#120): a gate that reports success without measuring anything is
    # the same bug as a probe that cannot fail. "Not measured" must not read as "fine".
    print(f"FAILED MEASUREMENT: {msg}")
    sys.exit(2)


try:
    import yaml
except ImportError:
    measurement_failed("PyYAML unavailable, so no component's config was ever inspected")

if not UMBRELLA.is_file():
    measurement_failed(f"no umbrella values at {UMBRELLA} -- wrong path?")
if not TOOLKIT.is_dir():
    measurement_failed(f"no helm-toolkit at {TOOLKIT}; every component chart needs it to render")

try:
    umbrella = yaml.safe_load(UMBRELLA.read_text()) or {}
    comps = {c["name"]: c for c in (umbrella.get("components") or [])}
except (yaml.YAMLError, AttributeError, KeyError, TypeError) as e:
    # Cannot read the graph -> cannot judge anything against it. Not a pass.
    measurement_failed(f"{UMBRELLA} does not parse into a component list -- {str(e)[:200]}")
if not comps:
    measurement_failed("the umbrella declares no components, so there is nothing to check")


def closure(name):
    """Declared transitive dependency closure of a component, including itself."""
    seen, stack = set(), [name]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(comps.get(n, {}).get("deps") or [])
    return seen


def render(name):
    """helm template a component chart in a freshly created empty directory."""
    chart = ROOT / "blueprints" / name / "chart"
    if not (chart / "Chart.yaml").is_file():
        return None, f"no chart at blueprints/{name}/chart"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "chart"
        shutil.copytree(chart, work)
        cy = work / "Chart.yaml"
        cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
        (work / "charts").mkdir(exist_ok=True)
        shutil.copytree(TOOLKIT, work / "charts/helm-toolkit", dirs_exist_ok=True)
        out = subprocess.run(
            # Without --kube-version a kubeVersion-constrained chart renders nothing, and a
            # gate over an empty document set passes everything.
            ["helm", "template", "gate", str(work), "--kube-version", "1.36.0"],
            capture_output=True, text=True,
        )
    if out.returncode != 0:
        tail = out.stderr.strip().splitlines()[-1][:200] if out.stderr.strip() else "no stderr"
        return None, f"helm template failed -- {tail}"
    try:
        docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    except yaml.YAMLError as e:
        return None, f"rendered output does not parse as YAML -- {str(e)[:200]}"
    if not docs:
        return None, "rendered 0 objects"
    return docs, None


def notifications_live(text):
    """True when this config will actually build an oslo.messaging transport.

    Read from the same file as the transport_url, so the judgement and the reference come
    from one measurement rather than from two sources that can disagree.
    """
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section != "oslo_messaging_notifications":
            continue
        m = NOTIFICATION_DRIVER_LINE.match(line)
        if m:
            return m.group(1).lower() not in NEEDS_NO_SERVER
    return False  # no notification driver configured at all


def oslo_confs(doc):
    """Every oslo-style config file carried by a rendered Secret or ConfigMap."""
    if doc.get("kind") == "Secret":
        for key, val in (doc.get("data") or {}).items():
            if key.endswith(CONF_SUFFIXES):
                try:
                    yield key, base64.b64decode(val).decode()
                except Exception:
                    continue
    elif doc.get("kind") == "ConfigMap":
        for key, val in (doc.get("data") or {}).items():
            if key.endswith(CONF_SUFFIXES) and isinstance(val, str):
                yield key, val


# ---------------------------------------------------------------------------
# Render everything once. The Service -> component map has to be complete before any
# wait can be judged, so this is two passes over one set of renders.
# ---------------------------------------------------------------------------
renders = {}
problems = []
for name in sorted(comps):
    if name == "openstack":  # the umbrella needs cluster lookups; its own gates cover it
        continue
    docs, err = render(name)
    if err:
        # A component that cannot be rendered cannot be checked. That is a gap in the
        # measurement, not a pass.
        measurement_failed(f"{name}: {err}")
    renders[name] = docs

if not renders:
    measurement_failed("no component chart was rendered, so nothing was inspected")

served_by = {}
for name, docs in renders.items():
    for doc in docs:
        if doc.get("kind") == "Service":
            served_by.setdefault(doc["metadata"]["name"], set()).add(name)
if not served_by:
    measurement_failed("no component renders a single Service; the wait check would pass "
                       "everything, because every waited-on name would look unknown")

gaps = []
if GAPS.is_file():
    loaded = yaml.safe_load(GAPS.read_text()) or {}
    gaps = loaded.get("gaps") or []
    for g in gaps:
        missing = [k for k in ("component", "reference", "why", "tracking") if not g.get(k)]
        if missing:
            measurement_failed(f"a gap in {GAPS.name} is missing {missing}; a suppression "
                               f"without a reason and a tracking issue is not a decision")
waived = {(g["component"], g["reference"]) for g in gaps}
used_waivers = set()


def report(component, reference, detail):
    if (component, reference) in waived:
        used_waivers.add((component, reference))
        return
    problems.append(f"{component}: {detail}")


checked_waits = 0
checked_backends = 0
ignored_tokens = set()

for name, docs in sorted(renders.items()):
    own = closure(name)
    jobs = {d["metadata"]["name"] for d in docs if d.get("kind") == "Job"}

    for doc in docs:
        spec_at = POD_SPEC_AT.get(doc.get("kind"))
        if not spec_at:
            continue
        try:
            spec = spec_at(doc)
        except (KeyError, TypeError):
            measurement_failed(f"{name}: {doc.get('kind')}/{doc['metadata']['name']} has no "
                               f"pod spec where one was expected, so its waits were not read")
        where = f"{doc['kind']}/{doc['metadata']['name']}"
        for container in (spec.get("initContainers") or []):
            for env in (container.get("env") or []):
                value = env.get("value") or ""
                if env.get("name") == "DEPENDENCY_SERVICE":
                    # An entirely empty variable is not a wait: util/env.SplitEnvToDeps
                    # returns before splitting when the value is "", so no dependency is
                    # registered and the init container does not block. Measured against a
                    # real API server: "" -> 0 deps. 31 of this repo's workloads render
                    # DEPENDENCY_SERVICE="" and are all fine.
                    if not value.strip():
                        continue
                    for item in (i.strip() for i in value.split(",")):
                        svc = item.split(":")[-1]
                        checked_waits += 1
                        if not svc:
                            report(name, "service:<empty>",
                                   f"{where} waits for a Service with an empty name: its "
                                   f"DEPENDENCY_SERVICE is {value!r}, and an empty element of a "
                                   f"non-empty list is NOT dropped -- "
                                   f"util/env.SplitEnvToDeps turns it into a dependency on the "
                                   f"Service named \"\", whose Endpoints Get fails with "
                                   f"`resource name may not be empty` forever. Measured: "
                                   f"\"a,,b\" -> 3 deps, the middle one named \"\". The cause is "
                                   f"a dependencies.static entry naming an endpoint the chart's "
                                   f"values do not define, which renders as nothing at all")
                            continue
                        owners = served_by.get(svc)
                        if not owners:
                            report(name, f"service:{svc}",
                                   f"{where} waits for Service {svc!r}, which no component "
                                   f"in this catalogue renders. kubernetes-entrypoint will "
                                   f"block on it forever and report nothing: this is the "
                                   f"exact shape of #115, where the message on the live "
                                   f"install was `Service postgresql ... has no ready "
                                   f"endpoints`")
                        elif not (owners & own):
                            report(name, f"service:{svc}",
                                   f"{where} waits for Service {svc!r}, served by "
                                   f"{sorted(owners)}, which is outside {name}'s declared "
                                   f"closure {sorted(own)}. Either add the dependency or "
                                   f"stop waiting on it")
                elif env.get("name") == "DEPENDENCY_JOBS":
                    for job in (j.strip() for j in value.split(",")):
                        if job and job not in jobs:
                            report(name, f"job:{job}",
                                   f"{where} waits for Job {job!r}, which this chart does "
                                   f"not render. A Job that is never created never "
                                   f"completes, so the wait never ends")

    for doc in docs:
        for conf_name, text in oslo_confs(doc):
            where = f"{doc['kind']}/{doc['metadata']['name']}:{conf_name}"
            live_transport = notifications_live(text)
            for line in text.splitlines():
                if TRANSPORT_LINE.match(line) and not live_transport:
                    continue
                m = DRIVER_LINE.match(line)
                token = m.group(1).lower() if m else None
                kind = "driver"
                if token is None:
                    m = URL_LINE.match(line)
                    if m:
                        token = m.group(1).lower()
                        kind = "URL scheme"
                if token is None:
                    continue
                if token not in BACKENDS:
                    ignored_tokens.add(token)
                    continue
                checked_backends += 1
                if token in NEEDS_NO_SERVER:
                    continue
                if token in NO_COMPONENT_PROVIDES:
                    report(name, f"backend:{token}",
                           f"{where} selects the {token!r} backend ({kind}), and no "
                           f"component in this catalogue provides it -- no blueprint "
                           f"directory, no CompositionDefinition, nothing. Point it at a "
                           f"backend this blueprint ships, or add the component")
                    continue
                provider = PROVIDED_BY[token]
                if provider not in own:
                    report(name, f"backend:{token}",
                           f"{where} selects the {token!r} backend ({kind}), provided by "
                           f"{provider!r}, which is outside {name}'s declared closure "
                           f"{sorted(own)}. The orchestrator will emit {name} before "
                           f"{provider} is ready")

if not checked_waits and not checked_backends:
    measurement_failed("not one dependency wait or backend reference was found across "
                       f"{len(renders)} components; the gate measured nothing")

stale = waived - used_waivers
if stale:
    problems.append(
        "stale entries in hack/config-closure-gaps.yaml: "
        + ", ".join(f"{c}/{r}" for c, r in sorted(stale))
        + " -- the finding they suppress no longer occurs, so remove them before they start "
          "hiding a new one"
    )

if problems:
    print(f"FAIL: {len(problems)} config reference(s) outside the declared dependency closure")
    for p in problems:
        print(f"  {p}")
    sys.exit(1)

print(f"OK: {len(renders)} components; {checked_waits} kubernetes-entrypoint waits and "
      f"{checked_backends} backend references all resolve inside their declared closure")
if ignored_tokens:
    print(f"    ({len(ignored_tokens)} token(s) outside the backend table were not treated as "
          f"backend references: {', '.join(sorted(ignored_tokens))})")
for g in gaps:
    print(f"    DECLARED GAP, still suppressing a finding: {g['component']} / "
          f"{g['reference']} -- {g['why'].strip()} (tracking: {g['tracking']})")
