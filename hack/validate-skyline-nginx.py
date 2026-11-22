#!/usr/bin/env python3
"""Skyline's nginx starts with a catalog endpoint whose host does not resolve, and proxies as before (#444).

    hack/validate-skyline-nginx.py [repo-root]

Found by the 0.3.71 -> 0.3.74 upgrade test (r074, 2026-11-21). Manila's Keystone endpoints outlived its removal, and
pointed at the deleted manila-api Service. skyline-nginx-generator writes one `proxy_pass <url>;` per catalog endpoint,
with a literal host and no resolver. nginx resolves literal proxy_pass hosts when it loads its config, so it refused to
start, and the umbrella never went Ready. skyline-nginx-tolerant.py (upstream divergence skyline-444) rewrites those
locations to resolve per request through the cluster DNS.

  1. THE CHART. skyline renders skyline-nginx-tolerant.py in skyline-bin. The api-init container mounts it, and its
     init script runs it on /etc/nginx/nginx.conf after skyline-nginx-generator.
  2. THE REWRITE. A config is generated from UPSTREAM's template (hack/fixtures/skyline-apiserver-2026.1-nginx.conf.j2,
     vendored, rendered with Jinja2 as the generator renders it). It has an endpoint whose host does not resolve and an
     endpoint on 127.0.0.1. The rendered script, run on it:
     - leaves no literal host in any endpoint proxy_pass;
     - leaves skyline's own upstream locations unchanged;
     - adds `resolver` with the given resolv.conf's nameservers, an IPv6 one bracketed;
     - refuses a resolv.conf with no nameserver.
  3. NGINX. `nginx -t` on the config BEFORE the rewrite must fail on the unresolvable host (the defect, a positive
     control). AFTER it, `nginx -t` must pass. A running nginx then:
     - proxies a request through the 127.0.0.1 endpoint with the same upstream path and query as the literal config
       sends (rewrite semantics equal);
     - answers the unresolvable location with 502, while it keeps running. Its `resolver` points at a stand-in DNS on
       127.0.0.1 that answers NXDOMAIN at once, as the cluster DNS does for a missing Service (nothing answers at
       10.96.0.10 on a runner, so nginx would only time out), and nginx must have asked it for the dead host.
  4. MUTANTS. The script with its rewrite disabled, with its resolver dropped, and with its path suffix ($1) dropped.
     Each must be caught.

nginx: the `nginx` on PATH (GitHub's Ubuntu runners ship Ubuntu's, the same nginx 1.24 as the skyline image's Ubuntu
noble). If none is on PATH, docker with the chart's skyline_nginx image. Neither: FAILED MEASUREMENT. Also fails when
helm, PyYAML or Jinja2 is missing, and when any part finds nothing to check.
"""
import base64
import http.server
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:
    import yaml
    import jinja2
except ImportError as e:
    print("FAILED MEASUREMENT: %s unavailable, so nothing was checked" % e.name)
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
CHART = os.path.join(ROOT, "blueprints", "skyline", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
TEMPLATE = os.path.join(ROOT, "hack", "fixtures", "skyline-apiserver-2026.1-nginx.conf.j2")
DEAD = "manila-api.openstack.svc.cluster.local:8786"      # the host #444 found: it does not resolve here either
CALL = "python3 /tmp/skyline-nginx-tolerant.py /etc/nginx/nginx.conf"
sys.path.insert(0, os.path.join(ROOT, "hack"))
import gate_values  # noqa: E402

counts = {}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---- 1. the chart ----------------------------------------------------------------------------------------------------
def render(tmp, fails):
    if shutil.which("helm") is None:
        print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
        sys.exit(2)
    w = os.path.join(tmp, "skyline")
    shutil.copytree(CHART, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    os.makedirs(os.path.join(w, "charts"), exist_ok=True)
    shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    r = subprocess.run(["helm", "template", "skyline-gate", w, "--namespace", "openstack", "--kube-version", "1.36.0",
                        *gate_values.helm_args(w)], capture_output=True, text=True)
    if r.returncode != 0:
        fails.append("1: skyline does not render: %s" % r.stderr.strip()[:300])
        return None, None, None
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    bin_cm = next((d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "skyline-bin"), None)
    if not bin_cm:
        fails.append("1: skyline renders no skyline-bin ConfigMap")
        return None, None, None
    script = (bin_cm.get("data") or {}).get("skyline-nginx-tolerant.py")
    init = (bin_cm.get("data") or {}).get("skyline-apiserver-init.sh") or ""
    if not script:
        fails.append("1: skyline-bin carries no skyline-nginx-tolerant.py")
    gi, ci = init.find("skyline-nginx-generator"), init.find(CALL)
    if ci < 0:
        fails.append("1: the init script never runs `%s`: nginx would load the literal config again" % CALL)
    elif not 0 <= gi < ci:
        fails.append("1: the init script runs the rewrite before skyline-nginx-generator writes the config")
    dep = next((d for d in docs if d.get("kind") == "Deployment"), None)
    mounted = False
    for c in ((dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("initContainers") or []):
        if c.get("name") == "api-init":
            mounted = any(m.get("mountPath") == "/tmp/skyline-nginx-tolerant.py" and m.get("subPath") == "skyline-nginx-tolerant.py"
                          for m in c.get("volumeMounts") or [])
    if not mounted:
        fails.append("1: api-init does not mount skyline-nginx-tolerant.py at /tmp/skyline-nginx-tolerant.py")
    image = None
    for c in ((dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []):
        if c.get("name") == "nginx":
            image = c.get("image")
    counts["1"] = 1
    return script, init, image


# ---- 2. the rewrite --------------------------------------------------------------------------------------------------
def generated(listen, static, log, echo_port):
    """the config skyline-nginx-generator writes: upstream's template, rendered with its context"""
    eps = [{"part": "# RegionOne manila", "location": "/api/openstack/regionone/manila/",
            "url": "http://%s/v2/" % DEAD, "host": DEAD},
           {"part": "# RegionOne echo", "location": "/api/openstack/regionone/echo/",
            "url": "http://127.0.0.1:%d/v2.1/" % echo_port, "host": "127.0.0.1:%d" % echo_port}]
    t = jinja2.Template(open(TEMPLATE).read())
    return t.render(endpoints=eps, listen_address=listen, skyline_console_static_path=static, access_log_file=log,
                    error_log_file=log, api_prefix="/api/v1", ssl_certfile="", ssl_keyfile="")


def run_script(script, tmp, conf_text, resolv_text):
    sp = os.path.join(tmp, "skyline-nginx-tolerant.py")
    open(sp, "w").write(script)
    c, rv = os.path.join(tmp, "nginx.conf"), os.path.join(tmp, "resolv.conf")
    open(c, "w").write(conf_text)
    open(rv, "w").write(resolv_text)
    r = subprocess.run([sys.executable, sp, c, rv], capture_output=True, text=True)
    return r.returncode, open(c).read(), (r.stdout + r.stderr).strip()


def check_rewrite(script, tmp, base, fails, label=""):
    rc, out, msg = run_script(script, tmp, base, "search openstack.svc.cluster.local\nnameserver 10.96.0.10\noptions ndots:5\n")
    if rc != 0:
        fails.append("2%s: the rewrite failed on a generated config: %s" % (label, msg[:200]))
        return None
    pp = re.findall(r"^\s*proxy_pass\s+(\S+);", out, re.M)
    literal = [p for p in pp if not p.startswith("http://skyline") and "$" not in p]
    if literal:
        fails.append("2%s: endpoint proxy_pass still literal after the rewrite: %s" % (label, literal))
    if [p for p in pp if p.startswith("http://skyline")] != [p for p in re.findall(r"^\s*proxy_pass\s+(\S+);", base, re.M)
                                                             if p.startswith("http://skyline")]:
        fails.append("2%s: the rewrite changed skyline's own upstream locations" % label)
    if not re.search(r"^\s*resolver\s+10\.96\.0\.10\s+valid=30s;", out, re.M):
        fails.append("2%s: no `resolver 10.96.0.10` from resolv.conf in the rewritten config" % label)
    if "rewrite ^/api/openstack/regionone/manila/(.*)$ /v2/$1 break;" not in out:
        fails.append("2%s: the manila location is not rewritten to its path with the suffix ($1)" % label)
    rc6, out6, _ = run_script(script, tmp, base, "nameserver fd00:10:96::a\n")
    if rc6 != 0 or "resolver [fd00:10:96::a] valid=30s;" not in out6:
        fails.append("2%s: an IPv6 nameserver is not bracketed in `resolver`" % label)
    rc0, _, _ = run_script(script, tmp, base, "search x\n")
    if rc0 == 0:
        fails.append("2%s: a resolv.conf with no nameserver is accepted; every endpoint would fail at request time" % label)
    counts["2"] = counts.get("2", 0) + 3
    return out


# ---- 3. nginx --------------------------------------------------------------------------------------------------------
class Nginx:
    def __init__(self, image):
        self.local = shutil.which("nginx")
        self.image = image if not self.local and shutil.which("docker") else None
        if not self.local and not self.image:
            print("FAILED MEASUREMENT: no nginx on PATH and no docker, so nginx never read a config")
            sys.exit(2)

    def cmd(self, d, *args):
        if self.local:
            return [self.local, "-p", d, "-e", os.path.join(d, "error.log"), *args]
        # The airshipit images are published for linux/amd64 only; name the platform so an arm64 host runs it emulated
        # instead of failing to find a manifest.
        return ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "host", "-v", "%s:%s" % (d, d),
                "--entrypoint", "nginx", self.image,
                "-p", d, "-e", os.path.join(d, "error.log"), *args]

    def test(self, d, conf):
        r = subprocess.run(self.cmd(d, "-t", "-c", conf), capture_output=True, text=True, timeout=120)
        return r.returncode, " ".join((r.stdout + r.stderr).split())


def for_test(text, d):
    """the harness's own paths for pid and logs (the generated /run/nginx.pid needs root); nothing the rewrite touched"""
    return text.replace("pid /run/nginx.pid;", "pid %s/nginx.pid;" % d)


class NxDns:
    """The cluster DNS, as the running nginx meets it: a nameserver that answers NXDOMAIN at once for a Service that does
    not exist. The rewritten config's `resolver` names the pod's nameserver (10.96.0.10 here, checked in part 2); on a
    runner nothing answers there, so nginx would wait out its resolver timeout and the request would time out, which is
    not what a pod sees. The live run points `resolver` at this server instead (live_resolver), and every name it was
    asked is recorded, so the 502 is shown to come from a lookup nginx made, not from a refusal before one."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.asked = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                q, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            if len(q) < 12:
                continue
            labels, i = [], 12
            while i < len(q) and q[i]:
                labels.append(q[i + 1:i + 1 + q[i]].decode("ascii", "replace"))
                i += 1 + q[i]
            end = i + 5                                   # the root label, QTYPE, QCLASS
            self.asked.append(".".join(labels))
            # the query's ID, then QR|RD|RA with RCODE 3 (NXDOMAIN), the one question echoed, no answers
            self.sock.sendto(q[:2] + b"\x81\x83" + b"\x00\x01\x00\x00\x00\x00\x00\x00" + q[12:end], addr)

    def close(self):
        self.sock.close()


def live_resolver(text, port):
    """the rewritten config's resolver line pointed at NxDns; nothing else the rewrite wrote is touched"""
    return re.sub(r"^(\s*resolver\s+)\S.*?(\s+valid=30s;)$", r"\g<1>127.0.0.1:%d\g<2>" % port, text, flags=re.M)


class Echo(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        Echo.seen.append(self.path)
        body = self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve_and_get(ng, d, conf, port, paths):
    p = subprocess.Popen(ng.cmd(d, "-c", conf, "-g", "daemon off;"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    got = {}
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        for path in paths:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=10) as r:
                    got[path] = (r.status, r.read().decode())
            except urllib.error.HTTPError as e:
                got[path] = (e.code, "")
            except OSError as e:
                got[path] = (None, str(e))
        got["alive"] = p.poll() is None
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
    return got


def check_nginx(ng, script, tmp, fails, label=""):
    d = tempfile.mkdtemp(dir=tmp)
    static = os.path.join(d, "static")
    os.makedirs(static)
    echo = http.server.HTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=echo.serve_forever, daemon=True).start()
    eport, lport = echo.server_address[1], free_port()
    try:
        base = generated("127.0.0.1:%d" % lport, static, os.path.join(d, "access.log"), eport)
        if not label:
            rc, msg = ng.test(d, write(d, "before.conf", for_test(base, d)))
            if rc == 0 or "host not found" not in msg:
                fails.append("3: nginx -t on the generated config BEFORE the rewrite did not fail on the unresolvable "
                             "host (rc=%s: %s): the defect was not reproduced, so the check measures nothing" % (rc, msg[:200]))
        after = check_rewrite(script, d, base, fails, label)
        if after is None:
            return
        rc, msg = ng.test(d, write(d, "after.conf", for_test(after, d)))
        if rc != 0:
            fails.append("3%s: nginx -t on the rewritten config fails: %s" % (label, msg[:300]))
            return
        req = "/api/openstack/regionone/echo/a/b%20c/?x=1&y=two"
        Echo.seen = []
        dns = NxDns()
        try:
            live = live_resolver(after, dns.port)
            if not label and live == after:
                fails.append("3: the rewritten config has no `resolver ... valid=30s;` line to point at the stand-in DNS")
            t0 = time.monotonic()
            a = serve_and_get(ng, d, write(d, "run-after.conf", for_test(live, d)), lport,
                              [req, "/api/openstack/regionone/manila/shares"])
            took = time.monotonic() - t0
        finally:
            dns.close()
        after_seen = list(Echo.seen)
        dead_host = DEAD.split(":")[0]
        if not label and dead_host not in dns.asked:
            fails.append("3: nginx never asked the stand-in DNS for %s (asked: %s), so a 502 would not show a lookup "
                         "failing at request time" % (dead_host, sorted(set(dns.asked))))
        # the same request through the LITERAL config, with only the resolvable endpoint (it cannot load otherwise)
        lit = re.sub(r"\n\s*# RegionOne manila\n\s*location /api/openstack/regionone/manila/ \{.*?\n\s*\}\n", "\n", base, flags=re.S)
        Echo.seen = []
        b = serve_and_get(ng, d, write(d, "run-literal.conf", for_test(lit, d)), lport, [req])
        lit_seen = list(Echo.seen)
        if not label and (not lit_seen or b.get(req, (None,))[0] != 200):
            fails.append("3: the literal config did not proxy the echo request (%s), so there is no baseline" % (b.get(req),))
        elif after_seen != lit_seen or a.get(req, (None,))[0] != 200:
            fails.append("3%s: the rewritten config sends %s upstream, the literal one %s (status %s)"
                         % (label, after_seen, lit_seen, a.get(req, (None,))[0]))
        if a.get("/api/openstack/regionone/manila/shares", (None,))[0] != 502 or not a.get("alive"):
            fails.append("3%s: the unresolvable location answered %s (want 502), nginx alive %s (run took %.1fs)"
                         % (label, a.get("/api/openstack/regionone/manila/shares"), a.get("alive"), took))
        counts["3"] = counts.get("3", 0) + 5
    finally:
        echo.shutdown()


def write(d, name, text):
    p = os.path.join(d, name)
    open(p, "w").write(text)
    return p


def main():
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        script, init, image = render(tmp, fails)
        if script:
            ng = Nginx(image)
            check_nginx(ng, script, tmp, fails)
            mutants = {
                "the rewrite disabled": ("        if m.group(\"host\") in upstreams:\n            continue",
                                         "        if True:\n            continue"),
                "the resolver dropped": ('"    resolver %s valid=30s;\\n" % " ".join(ns)', '""'),
                "the path suffix dropped": ("rewrite ^%s(.*)$ %s$1 break;", "rewrite ^%s(.*)$ %s break;"),
            }
            for what, (old, new) in mutants.items():
                if script.count(old) != 1:
                    fails.append("4: mutant '%s' did not apply: its target is not in the rendered script exactly once" % what)
                    continue
                mf = []
                check_nginx(ng, script.replace(old, new), tmp, mf, " (mutant: %s)" % what)
                if not mf:
                    fails.append("4: mutant '%s' passes every check: the checks cannot see it" % what)
                else:
                    print("   mutant caught: %s -> %s" % (what, mf[0][:120]))
                counts["4"] = counts.get("4", 0) + 1
    for k in ("1", "2", "3", "4"):
        if not counts.get(k):
            fails.append("part %s checked nothing" % k)
    if fails:
        for f in fails:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: skyline-nginx-tolerant.py rendered, mounted and run after the generator; on upstream's template it "
          "rewrites every endpoint location (%d check(s)); nginx -t fails before and passes after, proxies the same "
          "path and answers the unresolvable host 502 while running (%d check(s)); %d mutant(s) caught"
          % (counts["2"], counts["3"], counts["4"]))


main()
