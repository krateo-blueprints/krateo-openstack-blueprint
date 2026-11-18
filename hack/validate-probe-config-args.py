#!/usr/bin/env python3
"""Every worker readiness probe reads exactly the configuration its program was started with (#231, 0.3.74 defect B).

    hack/validate-probe-config-args.py [repo-root]

The probe used to read the program's --config-file/--config-dir arguments back from the program's /proc cmdline,
which oslo.service's threading backend and cotyledon rewrite into a title, so on every titled service the configuration
was lost. Now each probed program's configuration is ONE list, `<chart>.config_args.<program>`, rendered by
helm-toolkit.snippets.oslo_config_args into both its start script's exec line and its probe's exec command. That is
the construction; this gate guards it against a later hand edit of either consumer:

  for every container whose readinessProbe runs /tmp/worker-health-probe.py in --rpc-topic or --broker mode, in every
  chart that ships it, rendered at its defaults (with hack/gate_values.py) and with the spec the umbrella gives it in
  identity, full and full-strict with a Ceph storage backend (hack/umbrella_spec.py; the components each profile
  selects): the probe's --config-file/--config-dir/--config-dir-if-present
  list must equal the config arguments of the program's invocation in the start script the container runs, evaluated
  by bash with each `[ -d D ]` directory present and again absent, in order. The invocation may expand no shell
  variable (ironic-conductor's `${OPTIONS}` once added --config-file entries no list saw), so evaluating it alone is
  evaluating what the program receives.

Positive controls, run first on the rendered objects: a probe given one more --config-file, and a script whose
invocation lost its --config-dir, must each be reported. Fails when helm or PyYAML is missing, when a chart does not
render, and when no probe is measured.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was rendered")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
BP = os.path.join(ROOT, "blueprints")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
SCRIPT = "/tmp/worker-health-probe.py"
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "hack", name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def render(chart, values, tmp):
    w = os.path.join(tmp, chart)
    if not os.path.exists(w):
        shutil.copytree(os.path.join(BP, chart, "chart"), w)
        cy = os.path.join(w, "Chart.yaml")
        txt = open(cy).read()
        open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    vf = os.path.join(tmp, "values.yaml")
    with open(vf, "w") as f:
        yaml.safe_dump(values or {}, f)
    r = subprocess.run(["helm", "template", chart + "-gate", w, "--namespace", "openstack", "--kube-version", "1.36.0",
                        *load("gate_values").helm_args(w), "-f", vf], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[-400:])
    return [d for d in yaml.safe_load_all(r.stdout) if isinstance(d, dict)]


def probe_sites(docs):
    """[(where, process, [(flag, path)], script text)] for every --rpc-topic/--broker worker probe."""
    bins = {}
    for d in docs:
        if d.get("kind") in ("ConfigMap", "Secret") and str(d["metadata"]["name"]).endswith("-bin"):
            bins.update(d.get("data") or {})
    out = []
    for d in docs:
        if d.get("kind") not in WORKLOADS:
            continue
        for c in d["spec"]["template"]["spec"].get("containers") or []:
            cmd = (((c.get("readinessProbe") or {}).get("exec") or {}).get("command")) or []
            if SCRIPT not in cmd or "--listen" in cmd:
                continue
            args = cmd[cmd.index(SCRIPT) + 1:]
            proc = args[args.index("--process") + 1]
            conf = load("config_argv").probe_config(args)
            scripts = [os.path.basename(a) for a in (c.get("command") or []) + (c.get("args") or [])
                       if re.match(r"^/tmp/[\w.-]+\.sh$", str(a))]
            where = "%s/%s container %s" % (d["kind"], d["metadata"]["name"], c["name"])
            if len(scripts) != 1 or scripts[0] not in bins:
                out.append((where, proc, conf, None))
                continue
            out.append((where, proc, conf, bins[scripts[0]]))
    return out


def invocation(text, prog):
    lines = text.split("\n")
    for i, l in enumerate(lines):
        if re.match(r"^\s*(exec\s+)?%s(\s|$)" % re.escape(prog), l):
            j = i
            while lines[j].rstrip().endswith("\\") and j + 1 < len(lines):
                j += 1
            return "\n".join(lines[i:j + 1])
    return None


def script_config(stmt, prog, present):
    """The config arguments bash gives the program, [(flag, path)], with every `[ -d D ]` directory present or not."""
    body = re.sub(r"^(\s*)(exec\s+)?%s" % re.escape(prog), r"\1__printargs %s" % prog, stmt, count=1)
    body = body.replace("[ -d ", "__isdir ").replace(" ] &&", " &&")
    sh = ("__printargs() { printf '%%s\\0' \"$@\"; }\n__isdir() { %s; }\nHOSTNAME=gate-host\n%s\n"
          % ("true" if present else "false", body))
    r = subprocess.run(["bash", "-c", sh], capture_output=True)
    if r.returncode:
        raise RuntimeError(r.stderr.decode().strip()[-300:])
    argv = []
    for a in (x.decode() for x in r.stdout.split(b"\0") if x):
        m = re.match(r"^(--config-(?:file|dir))=(.*)$", a)
        argv += [m.group(1), m.group(2)] if m else [a]
    return [(argv[i], argv[i + 1]) for i in range(len(argv) - 1) if argv[i] in ("--config-file", "--config-dir")]


def probe_config(conf, present):
    out = []
    for flag, path in conf:
        if flag == "--config-dir-if-present":
            if present:
                out.append(("--config-dir", path))
        else:
            out.append((flag, path))
    return out


def check(site):
    where, proc, conf, text = site
    if text is None:
        return ["%s: its command runs no single /tmp/*.sh from the chart's -bin ConfigMap, so the probe's "
                "configuration cannot be checked against the program's" % where]
    if not conf:
        return ["%s: the probe of %s is given no --config-file/--config-dir" % (where, proc)]
    stmt = invocation(text, proc)
    if stmt is None:
        return ["%s: the start script never invokes %s" % (where, proc)]
    # The comparison evaluates the invocation alone, which is exact only if it expands no variable: a `${OPTIONS}`
    # set earlier in the script is how ironic-conductor's extra --config-file entries used to bypass any list.
    # Everything the program receives must be in the invocation itself, so a variable there is refused.
    bare = re.sub(r"\$\(\[ -d \S+ \] && echo --config-dir \S+\)", "", stmt)
    var = re.search(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", bare)
    if var:
        return ["%s: %s's invocation expands %s; its arguments must come from its config list and literals, so the "
                "probe can be compared with what the program receives" % (where, proc, var.group(0))]
    problems = []
    for present in (True, False):
        want = script_config(stmt, proc, present)
        got = probe_config(conf, present)
        if want != got:
            problems.append("%s: with each conf.d %s, %s starts with %s but its probe reads %s"
                            % (where, "present" if present else "absent", proc, want, got))
    return problems


def main():
    if shutil.which("helm") is None or shutil.which("bash") is None:
        print("FAILED MEASUREMENT: helm or bash not on PATH")
        return 2
    charts = sorted(c for c in os.listdir(BP) if os.path.isdir(os.path.join(BP, c, "chart", "templates"))
                    and any("worker_readiness_probe" in open(os.path.join(dp, f)).read()
                            for dp, _, fs in os.walk(os.path.join(BP, c, "chart", "templates")) for f in fs))
    if not charts:
        print("FAILED MEASUREMENT: no chart renders a worker readiness probe")
        return 2
    um = load("umbrella_spec")
    cases = [(c, "defaults", {}) for c in charts]
    for profile in ("identity", "full", "full-strict"):
        # A storage backend, as the umbrella always has one: cinder refuses none (#308).
        uvals = {"profile": profile, "storage": um.ceph_external_values()}
        specs, sel = um.specs(ROOT, uvals), set(um.selected(ROOT, uvals))
        cases += [(c, "umbrella " + profile, specs[c]) for c in charts if c in specs and c in sel]
    failures, measured, control = [], 0, None
    with tempfile.TemporaryDirectory() as tmp:
        for chart, label, values in cases:
            try:
                docs = render(chart, values, tmp)
            except RuntimeError as e:
                failures.append("%s (%s): does not render: %s" % (chart, label, e))
                continue
            for site in probe_sites(docs):
                measured += 1
                failures += ["%s (%s): %s" % (chart, label, p) for p in check(site)]
                if control is None and site[3] is not None and site[2]:
                    control = site
    if control is None:
        print("FAILED MEASUREMENT: no worker probe with a start script was found")
        return 2
    # Positive controls: the check must see a probe and a script that disagree.
    where, proc, conf, text = control
    if not check((where, proc, conf + [("--config-file", "/etc/gate/extra.conf")], text)):
        failures.append("positive control: a probe given one more --config-file was not reported")
    stmt = invocation(text, proc)
    cut = re.sub(r"\s*(--config-dir \S+|\$\(\[ -d \S+ \] && echo --config-dir \S+\))(?=\s*(\\|$))", "", stmt, count=1)
    if cut == stmt:
        failures.append("positive control: %s's invocation carries no --config-dir to remove" % proc)
    elif not check((where, proc, conf, text.replace(stmt, cut))):
        failures.append("positive control: a start script that lost its --config-dir was not reported")
    if failures:
        print("FAIL (%d):" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: %d worker probe(s) in %d chart(s) x %d render(s) read exactly their program's configuration (both "
          "positive controls reported)" % (measured, len(charts), len(cases)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
