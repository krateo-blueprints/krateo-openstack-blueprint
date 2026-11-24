#!/usr/bin/env python3
"""No container with a memory limit takes its number of processes from the node (#480).

    hack/validate-worker-counts.py [repo-root] [--self-test]

Measured on the 0.3.75 candidate (GKE, 16-CPU nodes, 2026-11-24): cloudkitty-processor's default, one worker and one reprocessing
worker per CPU, was OOM-killed in 5 s at its 1 GiB limit, and its component never became Ready. watcher-api (16 workers, 713 MiB
of 1 GiB) and skyline (8, 1152 MiB of 2 GiB) would go the same way on 32 CPUs. A limit measured on one node size fits that
size only.

  1. WORKERS: the options whose own default is the CPU count, read from each service's stable/2026.1 source on 2026-11-24 and from the
     live process counts of all 72 container kinds of that run. Each must be rendered as an integer from 1 to MAX. The defaults
     live in the images, so no render shows them, and the list is explicit.
  2. No rendered text of any chart asks the machine: cpu_count, nproc, os.sched_getaffinity.
  3. gnocchi-api recycles its workers: --reload-on-rss, between 100 and half of the container's memory limit in MiB, divided by its
     number of processes.

Fails when helm or PyYAML is missing, and when a chart does not render.
"""
import base64
import configparser
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

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
ROOT = os.path.abspath(ARGS[0] if ARGS else ".")
sys.path.insert(0, os.path.join(ROOT, "hack"))
import gate_values  # noqa: E402

TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
MAX = 8
# chart -> (rendered file, [(section, option)])
WORKERS = {
    "cloudkitty": ("cloudkitty.conf", [("orchestrator", "max_workers"), ("orchestrator", "max_workers_reprocessing")]),
    "watcher": ("watcher.conf", [("api", "workers")]),
    "magnum": ("magnum.conf", [("conductor", "workers")]),
}
ASKS = re.compile(r"cpu_count\s*\(|\bnproc\b|sched_getaffinity")
COMMENT = re.compile(r"^\s*(#|//|--)")


def ini(text):
    p = configparser.RawConfigParser(strict=False, default_section="\0none", interpolation=None)
    p.read_string(text)
    return {(s, o): v.strip() for s in p.sections() for o, v in p.items(s)}


def judge_workers(chart, files):
    name, opts = WORKERS[chart]
    if name not in files:
        return ["%s renders no %s, so its worker counts were not checked" % (chart, name)]
    conf = ini(files[name])
    bad = []
    for s, o in opts:
        v = conf.get((s, o))
        if not (v or "").isdigit() or not 1 <= int(v) <= MAX:
            bad.append("%s %s [%s] %s is %r, want an integer from 1 to %d: the service's own default is the node's CPU count" % (chart, name, s, o, v, MAX))
    return bad


def judge_asks(chart, files):
    return ["%s: %s asks the node for its CPUs: %s" % (chart, f, line.strip()[:110])
            for f, t in sorted(files.items()) for line in t.splitlines() if ASKS.search(line) and not COMMENT.match(line)]


def mib(v):
    m = re.match(r"^(\d+(?:\.\d+)?)(Mi|Gi)$", str(v))
    return float(m.group(1)) * (1024 if m.group(2) == "Gi" else 1) if m else None


def judge_recycle(script, limit):
    m = re.search(r"--reload-on-rss\s+(\d+)", script or "")
    n = re.search(r"--processes\s+(\d+)", script or "")
    if not m or not n:
        return ["gnocchi-api starts uWSGI without --reload-on-rss or --processes"]
    top = (mib(limit) or 0) / 2 / int(n.group(1))
    if not 100 <= int(m.group(1)) <= top:
        return ["gnocchi-api recycles at %s MiB with %s processes, want 100 to %d under its limit %s" % (m.group(1), n.group(1), top, limit)]
    return []


def self_test():
    ok = {"cloudkitty.conf": "[orchestrator]\nmax_workers = 1\nmax_workers_reprocessing = 1\n"}
    cases = [
        ("fixed", judge_workers("cloudkitty", ok), 0),
        ("unset: the candidate's cloudkitty", judge_workers("cloudkitty", {"cloudkitty.conf": "[collect]\ncollector = gnocchi\n"}), 2),
        ("zero", judge_workers("cloudkitty", {"cloudkitty.conf": "[orchestrator]\nmax_workers = 0\nmax_workers_reprocessing = 1\n"}), 1),
        ("too many", judge_workers("cloudkitty", {"cloudkitty.conf": "[orchestrator]\nmax_workers = 16\nmax_workers_reprocessing = 1\n"}), 1),
        ("no file", judge_workers("cloudkitty", {}), 1),
        ("skyline's old gunicorn", judge_asks("skyline", {"g.py": "workers = (1 + multiprocessing.cpu_count()) // 2"}), 1),
        ("a shell that asks", judge_asks("x", {"a.sh": "exec uwsgi --processes $(nproc)"}), 1),
        ("a comment that mentions it", judge_asks("x", {"a.sh": "# not nproc\nexec uwsgi --processes 2"}), 0),
        ("recycling", judge_recycle("uwsgi --processes 2 --reload-on-rss 200", "1Gi"), 0),
        ("no recycling", judge_recycle("uwsgi --processes 2", "1Gi"), 1),
        ("recycling above what the limit allows", judge_recycle("uwsgi --processes 2 --reload-on-rss 400", "1Gi"), 1),
        ("no limit known", judge_recycle("uwsgi --processes 2 --reload-on-rss 200", None), 1),
    ]
    wrong = [(n, g, w) for n, g, w in cases if len(g) != w]
    for n, g, w in wrong:
        print("SELF-TEST FAIL: %s: %d finding(s), want %d: %s" % (n, len(g), w, g))
    print("self-test: %d case(s), %d wrong" % (len(cases), len(wrong)))
    return 1 if wrong else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    if shutil.which("helm") is None:
        print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
        return 2
    fails, n, texts, recycled = [], 0, 0, False
    comps = [c["name"] for c in yaml.safe_load(open(os.path.join(ROOT, "blueprints", "openstack", "chart", "values.yaml")))["components"]]
    with tempfile.TemporaryDirectory() as tmp:
        for c in comps:
            src = os.path.join(ROOT, "blueprints", c, "chart")
            if not os.path.isdir(src):
                continue
            w = os.path.join(tmp, c)
            shutil.copytree(src, w)
            cy = os.path.join(w, "Chart.yaml")
            text = open(cy).read()
            open(cy, "w").write(text.replace("CHART_VERSION", "0.0.0-gate"))
            if "helm-toolkit" in text:
                os.makedirs(os.path.join(w, "charts"), exist_ok=True)
                shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
            r = subprocess.run(["helm", "template", c, w, "--namespace", "openstack"] + gate_values.helm_args(w), capture_output=True, text=True)
            if r.returncode != 0:
                fails.append("%s does not render: %s" % (c, r.stderr.strip()[-300:]))
                continue
            n += 1
            docs = [d for d in yaml.safe_load_all(r.stdout) if d]
            files = {}
            for d in docs:
                if d.get("kind") in ("ConfigMap", "Secret"):
                    for k, v in (d.get("data") or {}).items():
                        files[k] = base64.b64decode(v).decode("utf-8", "replace") if d["kind"] == "Secret" else str(v)
                for x in ((d.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers", []) if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet") else []:
                    files["command of %s/%s" % (d["metadata"]["name"], x["name"])] = "\n".join(str(y) for y in (x.get("command") or []) + (x.get("args") or []))
                    if c == "gnocchi" and x["name"] == "gnocchi-api":
                        script = next((files[k] for k in files if k == "gnocchi-api.sh"), None)
                        fails += judge_recycle(script, ((x.get("resources") or {}).get("limits") or {}).get("memory"))
                        recycled = True
            texts += len(files)
            if c in WORKERS:
                fails += judge_workers(c, files)
            fails += judge_asks(c, files)
    if not recycled:
        fails.append("no gnocchi-api container found, so its recycling was not checked")
    for f in fails:
        print("FAIL: " + f)
    print("%d chart(s), %d rendered text(s), %d worker option(s); %d failure(s)" % (n, texts, sum(len(o) for _, o in WORKERS.values()), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
