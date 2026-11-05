#!/usr/bin/env python3
"""Run the pod specs the powerdns chart renders, against a real MariaDB, and use the server.

Every offline gate passed on a chart that could not start. The chart first merged in #170 had
three layered faults, each hidden behind the one before it: settings removed from PowerDNS 4.9 (fatal at
startup), a privileged port an unprivileged user cannot bind, and credentials set in environment
variables that pdns_server does not read. All of them are facts about the server, and no render
can see them.

So this reads the Deployment and the db-sync Job out of `helm template` and executes THOSE: their
image, command, args, env, mounts and probes. Nothing here restates what the chart configures, so
it cannot drift from the chart, which is the failure a hand-written docker command would have.

It uses the same restrictions as the pod: the rendered uid, no_new_privs, every capability dropped
and privileged ports enforced. Docker relaxes that sysctl by default, which would hide a bind
failure, so it is set back explicitly.

WHAT IT ASSERTS, in the order a cluster would reach them:

  1. the db-sync pod applies the schema, and a second run is a no-op (it runs on every release);
  2. the rendered readiness probe fails until the server is up, then passes;
  3. the API key from the Secret is accepted and a wrong one is refused;
  4. a zone created over the API answers a real DNS query, through the rendered port;
  5. with the database stopped, readiness FAILS and liveness still PASSES.

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
DB_IMAGE = "docker.io/library/mariadb:11.4.8"
ROOT_PASSWORD = "smoke-root"
API_KEY = "smoke-api-key-" + uuid.uuid4().hex
ZONE = "smoke.example."

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


def render(workdir):
    dst = pathlib.Path(workdir) / "chart"
    shutil.copytree(CHART, dst)
    (dst / "charts").mkdir(exist_ok=True)
    shutil.copytree(TOOLKIT, dst / "charts" / "helm-toolkit")
    chart_yaml = dst / "Chart.yaml"
    chart_yaml.write_text(chart_yaml.read_text().replace("CHART_VERSION", "0.0.0-smoke"))
    values = dst / "values.yaml"
    values.write_text(values.read_text().replace("chartVersion: CHART_VERSION",
                                                 'chartVersion: "0.0.0-smoke"'))
    out = sh("helm", "template", "powerdns", str(dst), "--namespace", NAMESPACE)
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


def docker_args(docs, spec, container, workdir, shared):
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
            docker_vol = shared.setdefault(mount["name"], f"pdns-smoke-{mount['name']}-{RUN}")
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


def run_once(docs, spec, container, workdir, shared, extra_env):
    name = f"pdns-smoke-{container['name']}-{uuid.uuid4().hex[:6]}"
    created_containers.append(name)
    cmd = ["docker", "run", "--name", name, "--network", NET]
    cmd += docker_args(docs, spec, container, workdir, shared)
    for k, v in container_env(docs, container, extra_env).items():
        cmd += ["-e", f"{k}={v}"]
    if container.get("command"):
        cmd += ["--entrypoint", container["command"][0]]
    cmd.append(container["image"])
    cmd += (container.get("command") or [])[1:]
    cmd += container.get("args") or []
    return sh(*cmd, timeout=180)


def probe(container_name, probe_spec):
    """Execute a rendered probe from inside the container, as the kubelet's exec would."""
    if "exec" in probe_spec:
        return sh("docker", "exec", container_name, *probe_spec["exec"]["command"]).returncode == 0
    if "httpGet" in probe_spec:
        port = probe_spec["httpGet"]["port"]
        if port == "api":
            port = API_PORT
        code = ("import urllib.request;"
                f"urllib.request.urlopen('http://127.0.0.1:{port}"
                f"{probe_spec['httpGet'].get('path', '/')}', timeout=4)")
        return sh("docker", "exec", container_name, "python3", "-c", code).returncode == 0
    measurement_failed(f"a probe has a form this harness cannot run: {list(probe_spec)}")


def wait_until(fn, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if fn():
            return True
        if not running(SERVER):
            return False
        time.sleep(1)
    return False


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


if shutil.which("docker") is None or shutil.which("helm") is None:
    measurement_failed("docker and helm are both required, and at least one is missing")
if sh("docker", "version").returncode != 0:
    measurement_failed("the docker daemon is not reachable")

workdir = tempfile.mkdtemp()
try:
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

    sh("docker", "network", "create", NET, check=True)
    network_created = True

    db_user = next(e["value"] for e in job_pod["containers"][0]["env"] if e["name"] == "DB_USER")
    db_name = next(e["value"] for e in job_pod["containers"][0]["env"] if e["name"] == "DB_NAME")
    db_pass = secret_value(docs, "powerdns-db-user", "DB_PASSWORD")
    db_host = next(e["value"] for e in job_pod["containers"][0]["env"] if e["name"] == "DB_HOST")
    created_containers.append(DB)
    sh("docker", "run", "-d", "--name", DB, "--network", NET, "--network-alias", db_host,
       "-e", f"MARIADB_ROOT_PASSWORD={ROOT_PASSWORD}", "-e", f"MARIADB_DATABASE={db_name}",
       "-e", f"MARIADB_USER={db_user}", "-e", f"MARIADB_PASSWORD={db_pass}", DB_IMAGE, check=True)
    for _ in range(90):
        ok = sh("docker", "exec", DB, "mariadb", "-uroot", f"-p{ROOT_PASSWORD}", "-e", "select 1")
        if ok.returncode == 0:
            break
        time.sleep(1)
    else:
        measurement_failed("MariaDB did not come up")

    # 1. db-sync, exactly as the Job renders it: the schema-copy init container, then the client.
    shared = {}
    copier = next(c for c in job_pod["initContainers"] if c["name"] == "powerdns-schema-copy")
    syncer = job_pod["containers"][0]
    out = run_once(docs, job_pod, copier, workdir, shared, {})
    if out.returncode != 0:
        problems.append(f"powerdns-schema-copy failed: {(out.stdout + out.stderr)[-300:]}")
    first = run_once(docs, job_pod, syncer, workdir, shared, {})
    if first.returncode != 0:
        problems.append(f"db-sync failed on an empty database: {(first.stdout + first.stderr)[-400:]}")
    tables = sh("docker", "exec", DB, "mariadb", f"-u{db_user}", f"-p{db_pass}", db_name, "-Nse",
                "show tables").stdout.split()
    if "domains" not in tables:
        problems.append(f"db-sync exited but created no `domains` table: {tables}")
    second = run_once(docs, job_pod, syncer, workdir, shared, {})
    if second.returncode != 0:
        problems.append("db-sync failed on its SECOND run. It runs on every release, so this "
                        f"breaks every upgrade: {(second.stdout + second.stderr)[-300:]}")

    # 2. The server, from the rendered container.
    cmd = ["docker", "run", "-d", "--name", SERVER, "--network", NET]
    cmd += docker_args(docs, pod, server, workdir, {})
    for k, v in container_env(docs, server, {}).items():
        cmd += ["-e", f"{k}={v}"]
    if server.get("command"):
        cmd += ["--entrypoint", server["command"][0]]
    cmd.append(server["image"])
    cmd += (server.get("command") or [])[1:]
    cmd += server.get("args") or []
    created_containers.append(SERVER)
    sh(*cmd, check=True)

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

        # 5. The probes must disagree during a database outage, which is the point of having two.
        sh("docker", "stop", DB, check=True)
        time.sleep(2)
        if probe(SERVER, server["readinessProbe"]):
            problems.append("readiness still PASSES with the database stopped, so the Service "
                            "keeps routing to a server that cannot read a zone")
        if not probe(SERVER, server["livenessProbe"]):
            problems.append("liveness FAILS with the database stopped, so an outage restarts the "
                            "server, which repairs nothing")
finally:
    cleanup()
    shutil.rmtree(workdir, ignore_errors=True)

if problems:
    print(f"FAIL: {len(problems)} problem(s) running the rendered powerdns pods")
    for p in problems:
        print("  - " + p)
    sys.exit(1)

print("OK: the rendered db-sync applied the schema and re-ran cleanly; the rendered server started "
      "unprivileged, authenticated its API, served a zone over DNS, and its two probes disagreed "
      "correctly during a database outage. NOT covered: the Service, the api-key-init Job and "
      "designate, which need a cluster.")
