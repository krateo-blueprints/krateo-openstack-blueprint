#!/usr/bin/env python3
"""Uninstalling a component deletes its completed install/upgrade hook Jobs (issue #206).

    hack/validate-hook-cleanup.py [repo-root] [component ...]

Every Job is a post-install,post-upgrade hook (#179, 0.3.41), and Helm never deletes hook resources at uninstall: a
removed cinder left six completed Jobs and their pods (#204), and a full teardown 156 pods. hook-succeeded cannot
help, because kubernetes-entrypoint waits on a dependency Job by name while the component is installed. So every chart
that renders such a Job renders helm-toolkit.manifests.job_hook_cleanup, a pre-delete hook that deletes them. Each
chart is rendered at its defaults and with every manifests.job_* switch on (as hack/validate-job-hooks.py does), and
wherever a render has an install/upgrade hook Job:

  1. exactly one cleanup Job: helm.sh/hook pre-delete, before-hook-creation and hook-succeeded, a weight above every
     other pre-delete hook (it runs last), a positive activeDeadlineSeconds, command `python -c <script>`, a script
     that compiles with no warning;
  2. its ServiceAccount, and every Role bound to it, are plain release objects (so they exist while pre-delete hooks
     run), and together grant list and delete on batch/jobs and nothing else, through no ClusterRole;
  3. its SELECTOR matches the labels of every install/upgrade hook Job in the render, and not its own;
  4. its script, run against a stand-in API holding the render's Jobs plus another release's hook Job and a non-hook Job
     of this release, deletes exactly this release's install/upgrade hook Jobs, each with propagationPolicy
     Background, and exits 0; run against an API that answers 500, it still exits 0 (a failed cleanup must not block
     the uninstall).

Fails when helm or PyYAML is missing, when a chart does not render, and when no render had a hook Job to clean.
"""
import argparse
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import warnings
from pathlib import Path

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

RELEASE_SUFFIX = "gate1234"
NAMESPACE = "openstack"
INSTALL_HOOKS = {"post-install", "post-upgrade"}
ALLOWED_VERBS = {"get", "list", "delete"}
NEEDED_VERBS = {"list", "delete"}


# What the umbrella delivers for a chart's storage (#308): cinder's default refuses, so a chart render carries
# hack/gate_values.py's arguments (nothing for every other chart).
import importlib.util as _gv_util
import os as _gv_os
_gv_spec = _gv_util.spec_from_file_location(
    "gate_values", _gv_os.path.join(_gv_os.path.dirname(_gv_os.path.abspath(__file__)), "gate_values.py"))
gate_values = _gv_util.module_from_spec(_gv_spec)
_gv_spec.loader.exec_module(gate_values)


def hooks_of(d):
    a = (d.get("metadata") or {}).get("annotations") or {}
    return {h.strip() for h in str(a.get("helm.sh/hook", "")).split(",") if h.strip()}


def policy_of(d):
    a = (d.get("metadata") or {}).get("annotations") or {}
    return {p.strip() for p in str(a.get("helm.sh/hook-delete-policy", "")).split(",") if p.strip()}


def weight_of(d):
    a = (d.get("metadata") or {}).get("annotations") or {}
    try:
        return int(str(a.get("helm.sh/hook-weight", "0")))
    except ValueError:
        return None


def parse_selector(sel):
    terms = []
    for t in [x.strip() for x in sel.split(",") if x.strip()]:
        if "!=" in t:
            k, v = t.split("!=", 1)
            terms.append((k.strip(), "!=", v.strip()))
        elif "==" in t or "=" in t:
            k, v = t.replace("==", "=").split("=", 1)
            terms.append((k.strip(), "=", v.strip()))
        else:
            raise ValueError("selector term %r is not k=v or k!=v" % t)
    return terms


def matches(terms, labels):
    for k, op, v in terms:
        if op == "=" and labels.get(k) != v:
            return False
        if op == "!=" and labels.get(k) == v:
            return False
    return True


def job_switches(chart):
    values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
    manifests = values.get("manifests") or {}
    return sorted(k for k, v in manifests.items() if k.startswith("job_") and isinstance(v, bool))


def render(chart, toolkit, component, workdir, extra):
    staged = workdir / component
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(chart, staged)
    cm = staged / "Chart.yaml"
    cm.write_text(cm.read_text().replace("CHART_VERSION", "0.0.0-gate"))
    (staged / "charts").mkdir(exist_ok=True)
    if not (staged / "charts" / "helm-toolkit").exists():
        shutil.copytree(toolkit, staged / "charts" / "helm-toolkit")
    r = subprocess.run(["helm", "template", f"{component}-{RELEASE_SUFFIX}", str(staged), "--namespace", NAMESPACE,
                        "--kube-version", "1.36.0", *gate_values.helm_args(staged), *extra], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-600:])
    return [d for d in yaml.safe_load_all(r.stdout) if isinstance(d, dict)]


class StandIn(http.server.BaseHTTPRequestHandler):
    """A namespaced batch/v1 Jobs API: list with a labelSelector, delete by name. Records every call."""
    jobs, calls, fail = [], [], False

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        StandIn.calls.append(("GET", u.path, urllib.parse.parse_qs(u.query), self.headers.get("Authorization")))
        if StandIn.fail:
            return self._send(500, {"message": "stand-in failure"})
        if u.path != "/apis/batch/v1/namespaces/%s/jobs" % NAMESPACE:
            return self._send(404, {"message": "not found"})
        sel = (urllib.parse.parse_qs(u.query).get("labelSelector") or [""])[0]
        terms = parse_selector(sel)
        items = [j for j in StandIn.jobs if matches(terms, j["metadata"].get("labels") or {})]
        self._send(200, {"kind": "JobList", "items": items})

    def do_DELETE(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null")
        StandIn.calls.append(("DELETE", self.path, body, self.headers.get("Authorization")))
        self._send(200, {"kind": "Status", "status": "Success"})


def run_script(script, selector, jobs, fail=False):
    StandIn.jobs, StandIn.calls, StandIn.fail = jobs, [], fail
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with tempfile.TemporaryDirectory() as sa:
            Path(sa, "token").write_text("gate-token\n")
            env = dict(os.environ, API_URL="http://127.0.0.1:%d" % srv.server_address[1], SA_DIR=sa,
                       NAMESPACE=NAMESPACE, SELECTOR=selector)
            r = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
    return r, list(StandIn.calls)


def check(component, label, docs, failure_probe):
    """Return (install/upgrade hook Jobs in the render, failures)."""
    out = []
    jobs = [d for d in docs if d.get("kind") == "Job"]
    hook_jobs = [j for j in jobs if hooks_of(j) & INSTALL_HOOKS]
    cleanups = [j for j in jobs if hooks_of(j) == {"pre-delete"} and j["metadata"]["name"].endswith("-hook-cleanup")]
    if not hook_jobs:
        return 0, out
    if len(cleanups) != 1:
        return len(hook_jobs), ["%d install/upgrade hook Job(s) and %d hook-cleanup Job(s), want exactly one: add "
                   "templates/job-hook-cleanup.yaml (helm-toolkit.manifests.job_hook_cleanup)" % (len(hook_jobs),
                                                                                                 len(cleanups))]
    c = cleanups[0]
    name = c["metadata"]["name"]
    pol = policy_of(c)
    if not {"before-hook-creation", "hook-succeeded"} <= pol:
        out.append("%s: delete policy %s, want before-hook-creation and hook-succeeded" % (name, sorted(pol)))
    w = weight_of(c)
    others = [(j["metadata"]["name"], weight_of(j)) for j in jobs if j is not c and "pre-delete" in hooks_of(j)]
    late = [o for o in others if o[1] is None or w is None or o[1] >= w]
    if late:
        out.append("%s (weight %s) does not sort after every other pre-delete hook: %s" % (name, w, late))
    spec = c.get("spec") or {}
    ads = spec.get("activeDeadlineSeconds")
    if not isinstance(ads, int) or isinstance(ads, bool) or ads <= 0:
        out.append("%s: activeDeadlineSeconds %r, want a positive bound" % (name, ads))
    pod = (spec.get("template") or {}).get("spec") or {}
    conts = pod.get("containers") or []
    cmd = (conts[0].get("command") or []) if len(conts) == 1 else []
    env = {e.get("name"): e.get("value") for e in (conts[0].get("env") or [])} if len(conts) == 1 else {}
    if len(cmd) != 3 or cmd[:2] != ["python", "-c"]:
        out.append("%s: command %r, want [python, -c, <script>]" % (name, cmd[:2]))
        return len(hook_jobs), out
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # as validate-shipped-python.py, which reads ConfigMaps, not a -c script
        try:
            compile(cmd[2], name, "exec")
        except (SyntaxError, Warning) as e:
            out.append("%s: its script does not compile cleanly: %s" % (name, e))
    selector = env.get("SELECTOR") or ""
    try:
        terms = parse_selector(selector)
    except ValueError as e:
        out.append("%s: %s" % (name, e))
        return len(hook_jobs), out
    # 2. RBAC
    sa = pod.get("serviceAccountName")
    sas = [d for d in docs if d.get("kind") == "ServiceAccount" and d["metadata"]["name"] == sa]
    if len(sas) != 1 or hooks_of(sas[0]):
        out.append("%s: its ServiceAccount %r is rendered %d time(s)%s; want once, as a plain release object"
                   % (name, sa, len(sas), " as a hook" if sas and hooks_of(sas[0]) else ""))
    rules, bound = [], 0
    for b in docs:
        if b.get("kind") not in ("RoleBinding", "ClusterRoleBinding"):
            continue
        if not any(s.get("kind") == "ServiceAccount" and s.get("name") == sa for s in b.get("subjects") or []):
            continue
        bound += 1
        ref = b.get("roleRef") or {}
        if b["kind"] == "ClusterRoleBinding" or ref.get("kind") != "Role":
            out.append("%s: %s %s binds its ServiceAccount to %s %s; want a namespaced Role only"
                       % (name, b["kind"], b["metadata"]["name"], ref.get("kind"), ref.get("name")))
            continue
        roles = [d for d in docs if d.get("kind") == "Role" and d["metadata"]["name"] == ref.get("name")]
        if len(roles) != 1:
            out.append("%s: Role %s is rendered %d time(s)" % (name, ref.get("name"), len(roles)))
            continue
        for obj in (b, roles[0]):
            if hooks_of(obj):
                out.append("%s: %s %s is a hook; it must be a plain release object to exist during pre-delete"
                           % (name, obj["kind"], obj["metadata"]["name"]))
        rules += roles[0].get("rules") or []
    if not bound:
        out.append("%s: nothing binds its ServiceAccount %r to a Role, so the API refuses its calls" % (name, sa))
    verbs = set()
    for r in rules:
        if set(r.get("apiGroups") or []) != {"batch"} or set(r.get("resources") or []) != {"jobs"} \
                or r.get("resourceNames") or r.get("nonResourceURLs"):
            out.append("%s: grants %s; want batch/jobs only" % (name, r))
        verbs |= set(r.get("verbs") or [])
    if bound and not (NEEDED_VERBS <= verbs <= ALLOWED_VERBS):
        out.append("%s: grants verbs %s on jobs; want list and delete, within %s" % (name, sorted(verbs),
                                                                                    sorted(ALLOWED_VERBS)))
    # 3. selector
    for j in hook_jobs:
        if not matches(terms, j["metadata"].get("labels") or {}):
            out.append("%s: SELECTOR %r does not match hook Job %s (labels %s), so uninstall leaves it behind"
                       % (name, selector, j["metadata"]["name"], j["metadata"].get("labels")))
    if matches(terms, c["metadata"].get("labels") or {}):
        out.append("%s: SELECTOR %r matches the cleanup Job itself" % (name, selector))
    # 4. stand-in run
    fixture = [{"metadata": {"name": j["metadata"]["name"], "labels": j["metadata"].get("labels") or {},
                             "annotations": j["metadata"].get("annotations") or {}}} for j in jobs]
    if hook_jobs:
        foreign = json.loads(json.dumps(fixture[jobs.index(hook_jobs[0])]))
        foreign["metadata"]["name"] = "other-release-" + foreign["metadata"]["name"]
        foreign["metadata"]["labels"] = {k: ("other-release" if v == f"{component}-{RELEASE_SUFFIX}" else v)
                                         for k, v in foreign["metadata"]["labels"].items()}
        child = json.loads(json.dumps(fixture[jobs.index(hook_jobs[0])]))
        child["metadata"]["name"] = "cron-child-" + child["metadata"]["name"]
        child["metadata"]["annotations"] = {}
        fixture += [foreign, child]
    r, calls = run_script(cmd[2], selector, fixture)
    deleted = {urllib.parse.unquote(p.rsplit("/", 1)[-1]) for m, p, _, _ in calls if m == "DELETE"}
    want = {j["metadata"]["name"] for j in hook_jobs}
    if r.returncode != 0:
        out.append("%s: script exited %d against the stand-in: %s" % (name, r.returncode, r.stderr.strip()[-300:]))
    if deleted != want:
        out.append("%s: script deleted %s, want exactly this release's install/upgrade hook Jobs %s"
                   % (name, sorted(deleted), sorted(want)))
    bad = [p for m, p, b, _ in calls if m == "DELETE" and (b or {}).get("propagationPolicy") != "Background"]
    if bad:
        out.append("%s: deleted %s without propagationPolicy Background, so their pods stay" % (name, bad))
    if any(a != "Bearer gate-token" for _, _, _, a in calls):
        out.append("%s: a call did not carry the ServiceAccount token" % name)
    if failure_probe:
        r2, _ = run_script(cmd[2], selector, fixture, fail=True)
        if r2.returncode != 0:
            out.append("%s: script exited %d when the API failed; it must exit 0 so it cannot block the uninstall"
                       % (name, r2.returncode))
    return len(hook_jobs), out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("components", nargs="*")
    a = ap.parse_args()
    if shutil.which("helm") is None:
        print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
        return 2
    root = Path(a.root).resolve()
    toolkit = root / "charts" / "helm-toolkit"
    charts = sorted(p.parent for p in (root / "blueprints").glob("*/chart/Chart.yaml")
                    if p.parent.parent.name != "openstack")
    if a.components:
        charts = [c for c in charts if c.parent.name in a.components]
    if not charts:
        print("FAILED MEASUREMENT: no component chart found")
        return 2
    failures, cleaned, renders, probed = [], 0, 0, False
    with tempfile.TemporaryDirectory() as td:
        for chart in charts:
            comp = chart.parent.name
            on = [x for k in job_switches(chart) for x in ("--set", f"manifests.{k}=true")]
            n_chart = 0
            for label, extra in (("defaults", []), ("all jobs on", on)):
                if label == "all jobs on" and not on:
                    continue
                try:
                    docs = render(chart, toolkit, comp, Path(td), extra)
                except RuntimeError as e:
                    failures.append(f"{comp} ({label}): does not render: {e}")
                    continue
                renders += 1
                n, f = check(comp, label, docs, failure_probe=not probed)
                probed = probed or (n > 0 and not f)  # the failure probe runs once, on a clean render
                n_chart = max(n_chart, n)
                failures += [f"{comp} ({label}): {x}" for x in f]
            cleaned += n_chart
            mine = [x for x in failures if x.startswith(comp + " ")]
            print(f"{'FAIL' if mine else 'OK  '} {comp}: {n_chart} install/upgrade hook Job(s)")
    if renders == 0 or cleaned == 0:
        failures.append("no render had an install/upgrade hook Job, so nothing was checked")
    for f in failures:
        print("FAIL " + f)
    if failures:
        print(f"FAIL: {len(failures)} problem(s) across {len(charts)} chart(s)")
        return 1
    print(f"OK: {len(charts)} chart(s), {cleaned} install/upgrade hook Job(s), each deleted by its release's "
          f"pre-delete cleanup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
