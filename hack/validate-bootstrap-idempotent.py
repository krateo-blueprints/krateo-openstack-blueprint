#!/usr/bin/env python3
"""No bootstrap uploads a duplicate image, and a failure to reach the API is never read as "absent" (issue #295).

    hack/validate-bootstrap-idempotent.py [repo-root]

Measured on GKE 2026-11-17 (0.3.68 -> 0.3.70, full-strict): manila's bootstrap, a post-upgrade hook, ran upstream's
`openstack image show manila-service-image || { curl ...; openstack image create ... }` while glance-api was rolling. The
show failed to reach glance, the script read that as "missing", and it uploaded a second 952 MB image. glance accepts
two images with one name. The 2 GiB store filled, glance-api went unready (#163's free-space floor), and glance, manila
and nova never became Ready.

Every chart is rendered at its defaults, and with every workload manifest on. In every rendered
shell script (every ConfigMap and Secret key ending in .sh):

  1. NO CREATE ON A FAILED LOOKUP. No `openstack <resource> show|list ... || <...openstack <resource> create...>` and no
     `if ! openstack <resource> show|list ...; then <...create...>`: either reads a failure (the API unreachable, or
     "More than one Image exists") as "absent". Exempt, listed with the reason: resources whose API refuses a second
     resource of one name, so a create run after a failed lookup fails too and the Job retries; it cannot duplicate.
  2. RUN AGAINST A STAND-IN. Every script that creates an image is RUN with a stand-in `openstack` and `curl`: when the
     image lookup fails it must fail without downloading or creating anything; when glance holds no image of the name
     it creates each image exactly once; when it holds one, or two, it creates nothing (and with two says so).

Fails when helm or PyYAML is missing, when a chart does not render, when no script creates an image, and when no
script was run.
"""
import glob
import json
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

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
BP = os.path.join(ROOT, "blueprints")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

# Resources whose API refuses a second one of the same name: after a failed lookup, a create fails as well (409, or the
# same connection error), so the Job retries and no duplicate can exist. glance is not here: it accepts two images with
# one name (measured live, #295).
UNIQUE_NAME = {
    "flavor": "nova refuses a second flavor of one name (FlavorExists)",
    "volume type": "cinder refuses a second volume type of one name (VolumeTypeExists)",
    "volume qos": "cinder refuses a second QoS spec of one name (QoSSpecsExists)",
    "share type": "manila refuses a second share type of one name (ShareTypeExists)",
    "share group type": "manila refuses a second share group type of one name (ShareGroupTypeExists)",
    "role": "keystone refuses a second role of one name (Conflict; helm-toolkit's ks-domain-user.sh)",
}
LOOKUP = re.compile(r"openstack\s+(?P<res>[a-z]+(?:\s+[a-z]+){0,2}?)\s+(?:show|list)\b[^\n|;]*\|\|")
IF_NOT = re.compile(r"\bif\s+!\s+openstack\s+(?P<res>[a-z]+(?:\s+[a-z]+){0,2}?)\s+(?:show|list)\b[^\n;]*;\s*then"
                    r"(?P<body>.*?)\n\s*(?:fi|else|elif)\b", re.S)
failures = []
FOUND = []
ALLOWED = []
RUNS = []


# What the umbrella delivers for a chart's storage (#308): cinder's default refuses, so a chart render carries
# hack/gate_values.py's arguments (nothing for every other chart).
import importlib.util as _gv_util
import os as _gv_os
_gv_spec = _gv_util.spec_from_file_location(
    "gate_values", _gv_os.path.join(_gv_os.path.dirname(_gv_os.path.abspath(__file__)), "gate_values.py"))
gate_values = _gv_util.module_from_spec(_gv_spec)
_gv_spec.loader.exec_module(gate_values)


def fail(m):
    failures.append(m)


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


def helm_template(chart, release, values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    try:
        r = subprocess.run(["helm", "template", release, chart, "--namespace", "openstack", "--kube-version", "1.36.0",
                            *gate_values.helm_args(chart), "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def consequent(text, at):
    """The command that runs when the lookup before `at` (just after its ||) fails: a { } or ( ) group, balanced, or
    the rest of the logical line."""
    i = at
    while i < len(text) and text[i] in " \t":
        i += 1
    if i < len(text) and text[i] in "{(":
        open_c, close_c = text[i], "}" if text[i] == "{" else ")"
        depth = 0
        for j in range(i, len(text)):
            if text[j] == open_c:
                depth += 1
            elif text[j] == close_c:
                depth -= 1
                if depth == 0:
                    return text[i:j + 1]
        return text[i:]
    end = text.find("\n", i)
    return text[i:] if end < 0 else text[i:end]


def static_check(where, script):
    logical = re.sub(r"\\\n\s*", " ", script)
    code = "\n".join(l for l in logical.splitlines() if not l.lstrip().startswith("#"))
    hits = []
    for m in LOOKUP.finditer(code):
        res = m.group("res")
        if re.search(r"openstack\s+%s\s+create\b" % re.escape(res), consequent(code, m.end())):
            hits.append((res, code[code.rfind("\n", 0, m.start()) + 1:m.end()].strip()))
    for m in IF_NOT.finditer(code):
        res = m.group("res")
        if re.search(r"openstack\s+%s\s+create\b" % re.escape(res), m.group("body")):
            hits.append((res, m.group(0).splitlines()[0].strip()))
    for res, line in hits:
        FOUND.append((where, res))
        if res in UNIQUE_NAME:
            ALLOWED.append((where, res))
            continue
        fail("1: %s: `%s` creates %s when the lookup fails; a failure to reach the API is read as \"absent\", and %s "
             "accepts a duplicate name (#295)" % (where, line[:120], ("an " if res[0] in "aeiou" else "a ") + res, "glance" if res == "image" else "this API"))


OPENSTACK_STUB = r"""#!/bin/bash
printf '%s\n' "openstack $*" >> "$GATE_LOG"
case "$1 $2" in
  "image list"|"image show")
    name=""
    prev=""
    for a in "$@"; do [ "$prev" = "--name" ] && name="$a"; prev="$a"; done
    [ -z "$name" ] && [ "$2" = "show" ] && name="$3"
    case "$GATE_MODE" in
      fail) echo "Failed to contact the endpoint at http://glance-api:9292 for discovery (gate stand-in)" >&2; exit 1;;
      empty) [ "$2" = "show" ] && { echo "No Image found for $name" >&2; exit 1; }; exit 0;;
      one) printf '%s\n' "$name"; exit 0;;
      two) [ "$2" = "show" ] && { echo "More than one Image exists with the name '$name'." >&2; exit 1; }
           printf '%s\n%s\n' "$name" "$name"; exit 0;;
    esac;;
esac
exit 0
"""
CURL_STUB = r"""#!/bin/bash
printf '%s\n' "curl $*" >> "$GATE_LOG"
# -o FILE writes FILE; otherwise (-O, or -sSL -O) the URL's basename
out=""
prev=""
url=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  case "$a" in http://*|https://*) url="$a";; esac
  prev="$a"
done
[ -n "$out" ] || out="$(basename "$url")"
: > "$out"
exit 0
"""


def run_check(where, script, tmp):
    """2. The script against a stand-in openstack and curl, in each lookup outcome."""
    names = re.findall(r"openstack\s+image\s+create\s+(\"[^\"]+\"|'[^']+'|\S+)", re.sub(r"\\\n\s*", " ", script))
    names = [n.strip("\"'") for n in names]
    if not names:
        return
    work = tempfile.mkdtemp(dir=tmp, prefix="boot-")
    os.makedirs(os.path.join(work, "bin"))
    for n, body in (("openstack", OPENSTACK_STUB), ("curl", CURL_STUB)):
        p = os.path.join(work, "bin", n)
        open(p, "w").write(body)
        os.chmod(p, 0o755)
    # the scripts work under /tmp in the pod (glance: cd /tmp/images); here, under this run's directory
    text = script.replace("export HOME=/tmp", "export HOME=%s" % work).replace("/tmp/images", os.path.join(work, "images"))
    os.makedirs(os.path.join(work, "images"))
    sp = os.path.join(work, "bootstrap.sh")
    open(sp, "w").write(text)
    for mode in ("fail", "empty", "one", "two"):
        log = os.path.join(work, "%s.log" % mode)
        env = {"PATH": os.path.join(work, "bin") + os.pathsep + os.environ.get("PATH", ""), "GATE_LOG": log,
               "GATE_MODE": mode, "TMPDIR": work, "HOME": work}
        r = subprocess.run(["bash", sp], env=env, capture_output=True, text=True, cwd=work)
        calls = open(log).read().splitlines() if os.path.exists(log) else []
        creates = [c for c in calls if re.match(r"openstack image create\b", c)]
        curls = [c for c in calls if c.startswith("curl ")]
        RUNS.append((where, mode))
        if mode == "fail":
            if r.returncode == 0:
                fail("2: %s: with glance unreachable the script exits 0; it must fail so the Job retries" % where)
            if creates or curls:
                fail("2: %s: with glance unreachable the script %s: a failure was read as \"absent\" (#295)" % (
                    where, "creates %d image(s)" % len(creates) if creates else "downloads %d file(s)" % len(curls)))
        elif mode == "empty":
            if r.returncode != 0:
                fail("2: %s: with no image in glance the script fails (rc %d): %s" % (where, r.returncode,
                                                                                    r.stderr.strip()[-200:]))
            for n in names:
                k = len([c for c in creates if re.match(r"openstack image create %s( |$)" % re.escape(n), c)])
                if k != 1:
                    fail("2: %s: with no image in glance the script creates %s %d time(s), want 1" % (where, n, k))
        else:
            if r.returncode != 0:
                fail("2: %s: with %s image(s) of each name in glance the script fails (rc %d): %s" % (
                    where, mode, r.returncode, r.stderr.strip()[-200:]))
            if creates or curls:
                fail("2: %s: with %s image(s) of each name already in glance the script still %s" % (
                    where, mode, "creates %d" % len(creates) if creates else "downloads"))
            if mode == "two" and "uploading nothing" not in r.stderr:
                fail("2: %s: with two images of one name the script does not say it uploads nothing" % where)


def main():
    charts = sorted(d for d in os.listdir(BP) if d != "openstack" and os.path.isfile(os.path.join(BP, d, "chart",
                                                                                                    "values.yaml")))
    if not charts:
        print("FAILED MEASUREMENT: no chart found under %s" % BP)
        sys.exit(2)
    with tempfile.TemporaryDirectory() as tmp:
        for d in charts:
            v = yaml.safe_load(open(os.path.join(BP, d, "chart", "values.yaml"))) or {}
            man = v.get("manifests") or {}
            on = {k: True for k, x in man.items() if x is False and (k.startswith(("daemonset_", "deployment_",
                                                                                    "statefulset_", "cron_", "job_"))
                                                                    or k.endswith("_monitor"))
                  and k != "job_image_repo_sync"}
            modes = [("", {}), (" (every workload on)", {"manifests": on})]
            w = stage(os.path.join(BP, d, "chart"), tmp, d)
            seen = set()
            for label, vals in modes:
                rc, out, err = helm_template(w, d + "-gate1234", vals)
                if rc != 0:
                    fail("%s%s: does not render: %s" % (d, label, err.strip()[:200]))
                    continue
                for doc in yaml.safe_load_all(out):
                    if not doc or doc.get("kind") not in ("ConfigMap", "Secret"):
                        continue
                    for k, val in (doc.get("data") or {}).items():
                        if not k.endswith(".sh"):
                            continue
                        if doc["kind"] == "Secret":
                            import base64
                            try:
                                val = base64.b64decode(val).decode("utf-8", "replace")
                            except Exception:
                                continue
                        key = (doc["metadata"]["name"], k, val)
                        if key in seen:
                            continue
                        seen.add(key)
                        where = "%s%s: %s/%s %s" % (d, label, doc["kind"], doc["metadata"]["name"], k)
                        static_check(where, str(val))
                        if re.search(r"openstack\s+image\s+create\b", str(val)):
                            run_check(where, str(val), tmp)
    if not RUNS:
        fail("no rendered script creates an image, so the bootstrap check had no input")
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: %d script run(s) against a stand-in (%d script(s) creating an image); %d lookup-then-create pattern(s), "
          "all on resources whose API refuses a duplicate name: %s" % (
              len(RUNS), len(RUNS) // 4, len(ALLOWED), ", ".join(sorted({"%s (%s)" % (r, w.split(":")[0])
                                                                          for w, r in ALLOWED}))))


main()
