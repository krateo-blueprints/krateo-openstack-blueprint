#!/usr/bin/env python3
"""Run the pod specs the powerdns chart renders, against a real MariaDB, and use the server.

Every offline gate passed on a chart that could not start. The chart first merged in #170 had
three layered faults, each hidden behind the one before it: settings removed from PowerDNS 4.9 (fatal at
startup), a privileged port an unprivileged user cannot bind, and credentials set in environment
variables that pdns_server does not read. All of them are facts about the server, and no render
can see them.

So this reads the Deployment and the db-init and db-sync Jobs out of `helm template` and executes
THOSE: their
image, command, args, env, mounts and probes. Nothing here restates what the chart configures, so
it cannot drift from the chart, which is the failure a hand-written docker command would have.

It uses the same restrictions as the pod: the rendered uid, no_new_privs, every capability dropped
and privileged ports enforced. Docker relaxes that sysctl by default, which would hide a bind
failure, so it is set back explicitly.

It renders under a release name unlike the chart's, as Krateo names a release (the Composition plus
a suffix). Rendered as `powerdns`, names built from the release and fixed names coincide, and that
hid a db-init Job mounting a ConfigMap and a Secret the chart never created (#179).

WHAT IT ASSERTS, in the order a cluster would reach them:

  0. db-init, against a MariaDB holding only its root account, creates the database and the user
     db-sync and the server log in as, and a second run (it runs on every release) is a no-op;
  1. the db-sync pod applies the schema, a second run is a no-op (it runs on every release), and
     a schema missing a table is refused rather than reported as already applied;
  2. the rendered readiness probe fails until the server is up, then passes;
  3. the API key from the Secret is accepted and a wrong one is refused;
  4. a zone created over the API answers a real DNS query, through the rendered port;
  5. with the database stopped, readiness FAILS and liveness PASSES, and the server answers again
     after the database returns;
  6. a secondary zone, created the way designate creates it, accepts a NOTIFY sent from a
     different address than its primary, as designate-worker's is;
  7. an outage with that NOTIFY queued ends with the server answering again;
  8. with webserver-allow-from narrowed as values.yaml advises, both probes still pass;
  9. a changed database password changes the pod template, so the pods roll.

Whether the process survives an outage is REPORTED, not asserted, because it depends on what the
server is doing. Each outage prints what happened. What must hold is recovery.

WHAT A GREEN RESULT DOES NOT COVER: Kubernetes itself. The Service mapping 53 to the named port is
only checked by name, the api-key-init Job and its RBAC need an apiserver, and designate is not
involved, so a zone transfer from designate-mdns is not exercised. Those need a cluster.
"""
import base64
import json
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid

try:
    import yaml
except ImportError:
    print("MEASUREMENT FAILED: PyYAML is not importable, so no pod spec could be read.")
    sys.exit(1)

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints" / "powerdns" / "chart"
TOOLKIT = ROOT / "charts" / "helm-toolkit"
NAMESPACE = "openstack"
# The MariaDB root password. Taken from the rendered admin Secret once the chart is rendered, so
# db-init logs in with exactly what the chart gives it.
ROOT_PASSWORD = None
# Krateo names a release after the Composition plus a suffix, never after the chart.
RELEASE = "powerdns-smoke1234"
API_KEY = "smoke-api-key-" + uuid.uuid4().hex
ZONE = "smoke.example."
SECONDARY_ZONE = "secondary.example."
OUTAGE_SECONDS = 135    # past the 121s exit measured with the default xfr-cycle-interval of 60s

RUN = uuid.uuid4().hex[:8]
NET = f"pdns-smoke-{RUN}"
DB = f"pdns-smoke-db-{RUN}"
SERVER = f"pdns-smoke-server-{RUN}"

created_containers = []
created_volumes = []
network_created = False
problems = []


def measurement_failed(msg):
    print(f"MEASUREMENT FAILED: {msg}")
    print("Nothing was verified. A skipped smoke test is not a passed one.")
    cleanup()
    sys.exit(1)


def sh(*cmd, check=False, input_text=None, timeout=120):
    out = subprocess.run(cmd, capture_output=True, text=True, input=input_text, timeout=timeout)
    if check and out.returncode != 0:
        measurement_failed(f"{' '.join(cmd[:4])} failed: {out.stderr.strip()[-400:]}")
    return out


def cleanup():
    for name in created_containers:
        sh("docker", "rm", "-f", name)
    for name in created_volumes:
        sh("docker", "volume", "rm", "-f", name)
    if network_created:
        sh("docker", "network", "rm", NET)


def render(workdir, *extra):
    dst = pathlib.Path(workdir) / f"chart-{uuid.uuid4().hex[:6]}"
    shutil.copytree(CHART, dst)
    (dst / "charts").mkdir(exist_ok=True)
    shutil.copytree(TOOLKIT, dst / "charts" / "helm-toolkit", dirs_exist_ok=True)
    chart_yaml = dst / "Chart.yaml"
    chart_yaml.write_text(chart_yaml.read_text().replace("CHART_VERSION", "0.0.0-smoke"))
    out = sh("helm", "template", RELEASE, str(dst), "--namespace", NAMESPACE, *extra)
    if out.returncode != 0:
        measurement_failed(f"helm template failed: {out.stderr.strip()[-400:]}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def find(docs, kind, name=None):
    hits = [d for d in docs if d.get("kind") == kind
            and (name is None or d["metadata"]["name"] == name)]
    if not hits:
        measurement_failed(f"the chart rendered no {kind} {name or ''}")
    return hits[0]


def secret_value(docs, name, key):
    if name == "powerdns-api-key":
        # Minted at run time by the api-key-init Job, so it is in no rendered manifest.
        return API_KEY
    return base64.b64decode(find(docs, "Secret", name)["data"][key]).decode()


def container_env(docs, container, extra):
    env = dict(extra)
    for item in container.get("env") or []:
        if "value" in item:
            env[item["name"]] = str(item["value"])
        elif "secretKeyRef" in item.get("valueFrom", {}):
            ref = item["valueFrom"]["secretKeyRef"]
            env[item["name"]] = secret_value(docs, ref["name"], ref["key"])
    return env


def new_pod():
    """The emptyDirs of one pod. Two pods never share one, so neither do their docker volumes."""
    return {"id": uuid.uuid4().hex[:6], "volumes": {}}


def docker_args(docs, spec, container, workdir, pod_state):
    """Translate one rendered container into docker flags, restrictions included."""
    args = []
    volumes = {v["name"]: v for v in spec.get("volumes") or []}
    for mount in container.get("volumeMounts") or []:
        vol = volumes.get(mount["name"])
        if vol is None:
            measurement_failed(f"{container['name']} mounts undeclared volume {mount['name']}")
        if "configMap" in vol:
            data = find(docs, "ConfigMap", vol["configMap"]["name"])["data"]
            key = mount.get("subPath")
            if key is None or key not in data:
                measurement_failed(f"{container['name']} mounts key {key!r} that the ConfigMap "
                                   "does not have")
            path = pathlib.Path(workdir) / f"{vol['configMap']['name']}-{key}"
            path.write_text(data[key])
            path.chmod(0o755)
            args += ["-v", f"{path}:{mount['mountPath']}:ro"]
        elif "emptyDir" in vol:
            docker_vol = pod_state["volumes"].setdefault(
                mount["name"], f"pdns-smoke-{mount['name']}-{pod_state['id']}")
            if docker_vol not in created_volumes:
                sh("docker", "volume", "create", docker_vol, check=True)
                created_volumes.append(docker_vol)
                # An emptyDir is world-writable. A docker volume is not, unless told.
                sh("docker", "run", "--rm", "-v", f"{docker_vol}:/v", DB_IMAGE,
                   "chmod", "0777", "/v", check=True)
            args += ["-v", f"{docker_vol}:{mount['mountPath']}"]
        else:
            measurement_failed(f"volume {mount['name']} has a type this harness cannot honour")
    sc = dict(spec.get("securityContext") or {})
    sc.update(container.get("securityContext") or {})
    if sc.get("runAsUser") is not None:
        args += ["--user", str(sc["runAsUser"])]
    if sc.get("allowPrivilegeEscalation") is False:
        args += ["--security-opt", "no-new-privileges"]
    args += ["--cap-drop", "ALL",
             "--sysctl", "net.ipv4.ip_unprivileged_port_start=1024"]
    return args


def docker_run(name, docs, spec, container, workdir, pod_state, detach=False):
    """`docker run` of one rendered container: its flags, env, entrypoint, image and args."""
    created_containers.append(name)
    cmd = ["docker", "run"] + (["-d"] if detach else [])
    cmd += ["--name", name, "--network", NET]
    cmd += docker_args(docs, spec, container, workdir, pod_state)
    for k, v in container_env(docs, container, {}).items():
        cmd += ["-e", f"{k}={v}"]
    if container.get("command"):
        cmd += ["--entrypoint", container["command"][0]]
    cmd.append(container["image"])
    cmd += (container.get("command") or [])[1:]
    cmd += container.get("args") or []
    return sh(*cmd, timeout=180)


def run_once(docs, spec, container, workdir, pod_state):
    return docker_run(f"pdns-smoke-{container['name']}-{uuid.uuid4().hex[:6]}", docs, spec,
                      container, workdir, pod_state)


def probe(container_name, probe_spec):
    """Execute a rendered probe from inside the container, as the kubelet's exec would."""
    if "exec" in probe_spec:
        return sh("docker", "exec", container_name, *probe_spec["exec"]["command"]).returncode == 0
    measurement_failed(f"a probe has a form this harness cannot run: {list(probe_spec)}")


def wait_until(fn, seconds, name=None):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if fn():
            return True
        if not running(name or SERVER):
            return False
        time.sleep(1)
    return False


def wait_for_db():
    for _ in range(90):
        if sh("docker", "exec", DB, "mariadb", "-uroot", f"-p{ROOT_PASSWORD}",
              "-e", "select 1").returncode == 0:
            return True
        if not running(DB):
            measurement_failed("MariaDB exited:\n" + db_logs())
        time.sleep(1)
    return False


def db_logs():
    logs = sh("docker", "logs", DB)
    return (logs.stdout + logs.stderr)[-900:]


def running(name):
    out = sh("docker", "inspect", "-f", "{{.State.Running}}", name)
    return out.stdout.strip() == "true"


def api(method, path, key, body=None):
    code = (
        "import json,sys,urllib.request,urllib.error\n"
        f"req=urllib.request.Request('http://127.0.0.1:{API_PORT}{path}',method={method!r},"
        f"data={json.dumps(body).encode() if body else None!r},"
        f"headers={{'X-API-Key':{key!r},'Content-Type':'application/json'}})\n"
        "try:\n  r=urllib.request.urlopen(req,timeout=5);print(r.status)\n"
        "except urllib.error.HTTPError as e:\n  print(e.code)\n")
    return sh("docker", "exec", SERVER, "python3", "-c", code).stdout.strip()


def dns_query(name, port):
    """A real SOA query over UDP from inside the server's network namespace."""
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.strip(".").split(".")) + b"\0"
    packet = struct.pack(">HHHHHH", 0x1234, 0x0000, 1, 0, 0, 0) + labels + struct.pack(">HH", 6, 1)
    code = (
        "import socket,sys\n"
        "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(4)\n"
        f"s.sendto(bytes.fromhex('{packet.hex()}'),('127.0.0.1',{port}))\n"
        "d=s.recv(512);print(d[3]&15,int.from_bytes(d[6:8],'big'))\n")
    return sh("docker", "exec", SERVER, "python3", "-c", code).stdout.strip()


def report_outage(seconds, what):
    """Hold an outage and say what the process did. If it exited, restart it with the database
    still down, as the kubelet would, and say whether it stays up."""
    deadline = time.time() + seconds
    while time.time() < deadline and running(SERVER):
        time.sleep(1)
    held = seconds - (deadline - time.time())
    if running(SERVER):
        print(f"outage with {what}: the server was still running after {held:.0f}s")
        return
    exited = sh("docker", "logs", SERVER)
    print(f"outage with {what}: the server exited after {held:.0f}s, code "
          + sh("docker", "inspect", "-f", "{{.State.ExitCode}}", SERVER).stdout.strip() + ":\n    "
          + (exited.stdout + exited.stderr).strip().splitlines()[-1][:220])
    sh("docker", "start", SERVER, check=True)
    time.sleep(10)
    print("  restarted with the database still down, it is "
          + ("running" if running(SERVER) else "exited, so it restarts until the database returns"))


def recover(after):
    """Bring the database back and require the server to answer, restarting it if it exited."""
    sh("docker", "start", DB, check=True)
    if not wait_for_db():
        measurement_failed("MariaDB did not come back:\n" + db_logs())
    if not running(SERVER):
        sh("docker", "start", SERVER, check=True)
    if not wait_until(lambda: probe(SERVER, server["readinessProbe"]), 45):
        problems.append(f"the server did not pass readiness after {after}")
        return
    answer = dns_query(ZONE, DNS_PORT)
    if answer.split()[:1] != ["0"] or answer.split()[1:2] in ([], ["0"]):
        problems.append(f"after {after}, {ZONE} did not answer: {answer!r}")


def send_notify(zone, port):
    """A NOTIFY from a separate container, so its source address is not the server's own or the
    zone's primary. That is designate's shape: the worker sends it, and the primary is mdns."""
    labels = b"".join(bytes([len(p)]) + p.encode() for p in zone.strip(".").split(".")) + b"\0"
    packet = (struct.pack(">HHHHHH", 0x4242, 0x2400, 1, 0, 0, 0) + labels
              + struct.pack(">HH", 6, 1))
    code = (
        "import socket\n"
        "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(4)\n"
        f"s.sendto(bytes.fromhex('{packet.hex()}'),('{SERVER}',{port}))\n"
        "print(s.recv(512)[3]&15)\n")
    out = sh("docker", "run", "--rm", "--network", NET, "--entrypoint", "python3",
             server["image"], "-c", code)
    return out.stdout.strip() or out.stderr.strip()[-200:]


if shutil.which("docker") is None or shutil.which("helm") is None:
    measurement_failed("docker and helm are both required, and at least one is missing")
if sh("docker", "version").returncode != 0:
    measurement_failed("the docker daemon is not reachable")

workdir = tempfile.mkdtemp()
try:
    # The server the chart runs against, read from its own chart, so a bump of that image reaches
    # this test. Only the db-sync client image is in the powerdns chart.
    DB_IMAGE = (yaml.safe_load((ROOT / "blueprints" / "mariadb" / "chart" / "values.yaml")
                               .read_text())["images"]["tags"]["mariadb"])
    docs = render(workdir)
    deployment = find(docs, "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    server = next(c for c in pod["containers"] if c["name"] == "powerdns")
    job = find(docs, "Job", "powerdns-db-sync")
    job_pod = job["spec"]["template"]["spec"]
    API_PORT = next(p["containerPort"] for p in server["ports"] if p["name"] == "api")
    DNS_PORT = next(p["containerPort"] for p in server["ports"] if p["name"] == "dns-udp")

    # Every named targetPort on the Service must exist on the container, or the Service routes
    # nowhere and nothing above would notice.
    named = {p["name"] for p in server["ports"]}
    for port in find(docs, "Service", "powerdns")["spec"]["ports"]:
        if port["targetPort"] not in named:
            problems.append(f"Service port {port['name']} targets {port['targetPort']!r}, which "
                            "no container port is named")

    # 9, checked first because it needs no containers. The password reaches the pod through a
    # secretKeyRef, so nothing in the template changes with it unless a hash annotation does, and
    # pods would keep the old password until something else restarted them.
    def db_secret_hash(rendered):
        annotations = find(rendered, "Deployment")["spec"]["template"]["metadata"]["annotations"]
        return annotations.get("secret-db-hash")

    if db_secret_hash(docs) is None or db_secret_hash(docs) == db_secret_hash(
            render(workdir, "--set", "endpoints.oslo_db.auth.powerdns.password=rotated")):
        problems.append("changing the database password does not change the pod template, so "
                        "running pods keep the old one and replicas can disagree after a restart")

    sh("docker", "network", "create", NET, check=True)
    network_created = True

    db_user = next(e["value"] for e in job_pod["containers"][0]["env"] if e["name"] == "DB_USER")
    db_name = next(e["value"] for e in job_pod["containers"][0]["env"] if e["name"] == "DB_NAME")
    db_pass = secret_value(docs, "powerdns-db-user", "DB_PASSWORD")
    db_host = next(e["value"] for e in job_pod["containers"][0]["env"] if e["name"] == "DB_HOST")
    ROOT_PASSWORD = secret_value(docs, "powerdns-db-admin", "DB_PASSWORD")
    created_containers.append(DB)
    # Only the root account. The database and the powerdns user are db-init's to create, as on a
    # cluster, where nothing else does.
    sh("docker", "run", "-d", "--name", DB, "--network", NET, "--network-alias", db_host,
       "-e", f"MARIADB_ROOT_PASSWORD={ROOT_PASSWORD}", DB_IMAGE, check=True)
    if not wait_for_db():
        measurement_failed("MariaDB did not come up:\n" + db_logs())

    # 0. db-init, exactly as the Job renders it. Its kubernetes-entrypoint init container waits on
    # the apiserver for dependencies and is skipped, as db-sync's is below.
    init_job = find(docs, "Job", "powerdns-db-init")
    init_pod = init_job["spec"]["template"]["spec"]
    initer = next(c for c in init_pod["containers"] if c["name"].startswith("powerdns-db-init"))
    for attempt in ("first", "second"):
        ran = run_once(docs, init_pod, initer, workdir, new_pod())
        if ran.returncode != 0:
            problems.append(f"db-init failed on its {attempt} run: {(ran.stdout + ran.stderr)[-400:]}")
    login = sh("docker", "exec", DB, "mariadb", f"-u{db_user}", f"-p{db_pass}", db_name, "-e",
               "select 1")
    if login.returncode != 0:
        problems.append(f"after db-init, {db_user} cannot log in to {db_name}: "
                        f"{(login.stdout + login.stderr)[-300:]}")

    # 1. db-sync, exactly as the Job renders it: the schema-copy init container, then the client.
    job_state = new_pod()
    copier = next(c for c in job_pod["initContainers"] if c["name"] == "powerdns-schema-copy")
    syncer = job_pod["containers"][0]
    out = run_once(docs, job_pod, copier, workdir, job_state)
    if out.returncode != 0:
        problems.append(f"powerdns-schema-copy failed: {(out.stdout + out.stderr)[-300:]}")
    first = run_once(docs, job_pod, syncer, workdir, job_state)
    if first.returncode != 0:
        problems.append(f"db-sync failed on an empty database: {(first.stdout + first.stderr)[-400:]}")
    tables = sh("docker", "exec", DB, "mariadb", f"-u{db_user}", f"-p{db_pass}", db_name, "-Nse",
                "show tables").stdout.split()
    if "domains" not in tables:
        problems.append(f"db-sync exited but created no `domains` table: {tables}")
    second = run_once(docs, job_pod, syncer, workdir, job_state)
    if second.returncode != 0:
        problems.append("db-sync failed on its SECOND run. It runs on every release, so this "
                        f"breaks every upgrade: {(second.stdout + second.stderr)[-300:]}")

    # An apply cut off part way leaves the first table and not the last. That must stop the job:
    # going green would start the server against a schema with holes in it.
    def db_sql(sql):
        return sh("docker", "exec", DB, "mariadb", "-uroot", f"-p{ROOT_PASSWORD}", db_name, "-e",
                  "SET FOREIGN_KEY_CHECKS=0;" + sql)

    db_sql(f"DROP TABLE {tables[-1]};")
    partial = run_once(docs, job_pod, syncer, workdir, job_state)
    if partial.returncode == 0:
        problems.append(f"db-sync exited 0 with table {tables[-1]} missing, so an interrupted "
                        "apply goes green and the server starts against a broken schema")
    elif tables[-1] not in partial.stdout + partial.stderr:
        problems.append("db-sync refused the partial schema without naming the missing table: "
                        f"{(partial.stdout + partial.stderr)[-300:]}")
    db_sql("".join(f"DROP TABLE {t};" for t in tables[:-1]))
    fresh = run_once(docs, job_pod, syncer, workdir, job_state)
    if fresh.returncode != 0:
        problems.append("db-sync failed on an empty database after the partial-schema check: "
                        f"{(fresh.stdout + fresh.stderr)[-300:]}")

    # 2. The server, from the rendered container.
    started = docker_run(SERVER, docs, pod, server, workdir, new_pod(), detach=True)
    if started.returncode != 0:
        measurement_failed(f"the server container did not start: {started.stderr.strip()[-400:]}")

    ready = wait_until(lambda: probe(SERVER, server["readinessProbe"]), 45)
    if not ready:
        logs = sh("docker", "logs", SERVER)
        problems.append("the server never passed its rendered readiness probe:\n      "
                        + (logs.stdout + logs.stderr)[-700:].replace("\n", "\n      "))
    else:
        # 3. Authentication.
        if api("GET", "/api/v1/servers/localhost/zones", API_KEY) != "200":
            problems.append("the API key from the Secret was not accepted")
        if api("GET", "/api/v1/servers/localhost/zones", "wrong-key") != "401":
            problems.append("a WRONG API key was not refused with 401, so the API is unprotected")

        # 4. A real zone answering a real query, on the port the Service maps to.
        status = api("POST", "/api/v1/servers/localhost/zones", API_KEY, {
            "name": ZONE, "kind": "Native", "nameservers": ["ns1." + ZONE]})
        if status != "201":
            problems.append(f"creating a zone over the API returned {status}, expected 201")
        answer = dns_query(ZONE, DNS_PORT)
        if answer.split()[:1] != ["0"] or answer.split()[1:2] in ([], ["0"]):
            problems.append(f"a SOA query for {ZONE} on port {DNS_PORT} did not get an answer "
                            f"(rcode, answers = {answer!r})")

        # 5. An outage with no NOTIFY queued. Readiness must fail and liveness must pass:
        # that disagreement is the point of having two probes.
        sh("docker", "stop", DB, check=True)
        time.sleep(2)
        if not running(SERVER):
            problems.append("the server exited within 2s of a database outage with no NOTIFY "
                            "queued, so liveness could not be judged")
        else:
            if probe(SERVER, server["readinessProbe"]):
                problems.append("readiness still PASSES with the database stopped, so the Service "
                                "keeps routing to a server that cannot read a zone")
            if not probe(SERVER, server["livenessProbe"]):
                problems.append("liveness FAILS on a running server with the database stopped, so "
                                "an outage restarts a server that has not exited")
        report_outage(OUTAGE_SECONDS, "no NOTIFY queued")
        recover("the outage with no NOTIFY queued")

        # 6. A secondary zone, posted as designate's pdns4 backend posts it. Its primary is an
        # address nothing answers on, so a NOTIFY from elsewhere is accepted only if the server
        # trusts the sender. rcode 5 is REFUSED.
        status = api("POST", "/api/v1/servers/localhost/zones", API_KEY, {
            "name": SECONDARY_ZONE, "kind": "Slave", "masters": ["192.0.2.1"]})
        if status != "201":
            problems.append(f"creating a secondary zone returned {status}, expected 201")
        rcode = send_notify(SECONDARY_ZONE, DNS_PORT)
        if rcode != "0":
            problems.append(f"a NOTIFY for {SECONDARY_ZONE} from a non-primary address got rcode "
                            f"{rcode!r}, expected 0. designate-worker sends these from its pod "
                            "address, so a zone it changes would not refresh until its SOA timer.")

        # 7. An outage with that NOTIFY queued.
        sh("docker", "stop", DB, check=True)
        report_outage(30, "a NOTIFY queued")
        recover("the outage with a NOTIFY queued")

        # 8. values.yaml tells operators to narrow this, so doing so must not break the probes.
        narrow = render(workdir, "--set", "conf.pdns.webserver-allow-from=10.99.0.0/16")
        narrow_name = SERVER + "-narrow"
        narrow_server = next(c for c in find(narrow, "Deployment")["spec"]["template"]["spec"]
                             ["containers"] if c["name"] == "powerdns")
        docker_run(narrow_name, narrow, pod, narrow_server, workdir, new_pod(), detach=True)
        if not wait_until(lambda: probe(narrow_name, narrow_server["readinessProbe"]), 45,
                          narrow_name):
            problems.append("with webserver-allow-from narrowed, readiness never passes, so "
                            "following the chart's advice empties the Service")
        elif not probe(narrow_name, narrow_server["livenessProbe"]):
            problems.append("with webserver-allow-from narrowed, liveness fails, so following the "
                            "chart's advice restarts every pod")
finally:
    cleanup()
    shutil.rmtree(workdir, ignore_errors=True)

if problems:
    print(f"FAIL: {len(problems)} problem(s) running the rendered powerdns pods")
    for p in problems:
        print("  - " + p)
    sys.exit(1)

print(f"OK: rendered as release {RELEASE}, the rendered db-init created the database and its user "
      "against a bare MariaDB and re-ran cleanly; the "
      "rendered db-sync applied the schema, re-ran cleanly and refused a partial one; the "
      "rendered server started unprivileged, authenticated its API, served a zone over DNS, "
      "failed readiness and passed liveness during a database outage, accepted a NOTIFY for a "
      "secondary zone from a non-primary address, answered again after two outages, kept its "
      "probes with webserver-allow-from narrowed, and rolls its pods on a password change. NOT covered: the Service, the api-key-init Job and "
      "designate, which need a cluster.")
