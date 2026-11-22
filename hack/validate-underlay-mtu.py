#!/usr/bin/env python3
"""network.underlayMTU sets neutron's tunnel MTU, and the OVS agent refuses a node whose tunnel NIC is smaller (#433).

    hack/validate-underlay-mtu.py [repo-root]

Measured by dp303 run 4 (2026-11-21, 0.3.74-main.gc16973f) on nodes whose NIC MTU is 1460: neutron advertised 1450 to
a VXLAN network's guests, but only 1410 fit through the tunnel. A full-size DF ping from the router's namespace got
"Frag needed and DF set (mtu = 1410)". The umbrella's network.underlayMTU (default 1500) now reaches neutron as
global_physnet_mtu and ML2 path_mtu, so VXLAN networks advertise underlayMTU - 50. The OVS agent's init refuses to start
when the tunnel NIC's real MTU is below the MTU neutron uses for tunnels.

  1. THE UMBRELLA. The lines of compositions.yaml that build a component's spec are rendered for every component with
     network absent, underlayMTU 1500 and 1460. neutron's spec carries conf.neutron.DEFAULT.global_physnet_mtu and
     conf.plugins.ml2_conf.ml2.path_mtu equal to the value (1500 when absent); every other component's spec is the same
     in all three. An explicit componentValues.neutron entry wins. The schema refuses 1279, 9217 and a string, and
     accepts 1280 and 9216.
  2. THE CHART. neutron rendered with the spec the umbrella builds for 1460 and for 1500 renders that value as
     global_physnet_mtu in [DEFAULT] of neutron.conf and as path_mtu in [ml2] of ml2_conf.ini. These are the files the
     services read, not the values. The OVS agent init script renders UNDERLAY_MTU as path_mtu, else
     global_physnet_mtu, else 1500, and calls the check after LOCAL_IP, inside the tunnel block.
  3. THE REFUSAL RUNS. check_underlay_mtu, extracted from the rendered script, is RUN against a fake /sys/class/net:
     - a NIC below the value is refused, the message naming network.underlayMTU: <NIC MTU> and the advertised and
       fitting MTUs, and no cloud provider;
     - an equal or larger NIC passes;
     - a missing, empty or non-numeric MTU is refused.
  4. MUTANTS. The same checks are run on three mutants: the comparison removed from the function, the call removed
     from the script, and the umbrella's merge removed. Each must be caught, or the checks above prove nothing.

Fails when helm or PyYAML is missing, and when any part finds nothing to check.
"""
import base64
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
UMBRELLA = os.path.join(BP, "openstack", "chart")
NEUTRON = os.path.join(BP, "neutron", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
INIT = "templates/bin/_neutron-openvswitch-agent-init.sh.tpl"
CALL = 'check_underlay_mtu "${tunnel_interface}" "${UNDERLAY_MTU}" || exit 1'
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
counts = {}
caught = []


def stage(chart_dir, tmp, name, edit=None):
    w = os.path.join(tmp, name)
    shutil.copytree(chart_dir, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    if "helm-toolkit" in txt:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    if edit:
        rel, old, new = edit
        p = os.path.join(w, rel)
        s = open(p).read()
        if s.count(old) != 1:
            return None
        open(p, "w").write(s.replace(old, new))
    return w


def helm_template(chart, release, values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    try:
        r = subprocess.run(["helm", "template", release, chart, "--namespace", "openstack", "--kube-version", "1.36.0",
                            "-f", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def text_of(d, k):
    v = (d.get("data") or {}).get(k)
    if v is None:
        return ""
    return base64.b64decode(v).decode("utf-8", "replace") if d.get("kind") == "Secret" else str(v)


def ini_get(text, section, key):
    """every value of `key` in every [section] of an ini text"""
    out, cur = [], None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
        elif cur == section and "=" in s and not s.startswith(("#", ";")):
            k, _, v = s.partition("=")
            if k.strip() == key:
                out.append(v.strip())
    return out


# ---- 1. the umbrella ------------------------------------------------------------------------------------------------
def umbrella_probe(tmp, name, comp):
    """stage the umbrella with a template rendering each component's spec; None if the span is not there"""
    start = comp.find("{{- $ov := ")
    mark = comp.find("$.Values.network", start)
    end = comp.find("{{- end }}", mark) if mark >= 0 else -1
    if start < 0 or mark < 0 or end < 0:
        return None
    u = stage(UMBRELLA, tmp, name)
    open(os.path.join(u, "templates", "compositions.yaml"), "w").write(comp)
    open(os.path.join(u, "templates", "zz-gate-mtu-probe.yaml"), "w").write(
        '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
        '{{- $dr := include "osh.deregistration" (list $ (splitList " " (include "osh.selected" (list $))) (list)) | fromYaml }}\n{{- range $c := .Values.components }}\n' + comp[start:end + len("{{- end }}")] + '\n'
        '# MTUSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n')
    return u


def specs_of(u, vals):
    rc, out, err = helm_template(u, "openstack-gate", vals)
    if rc != 0:
        return None, err
    return {l.split(" ", 3)[2]: json.loads(l.split(" ", 3)[3]) for l in out.splitlines() if l.startswith("# MTUSPEC ")}, ""


def check_umbrella(u, names, fails):
    if u is None:
        fails.append("1: compositions.yaml builds no spec from $.Values.network (the span from `{{- $ov := ` through the "
                     "network merge), so network.underlayMTU reaches no component")
        return {}
    specs = {}
    for key, vals in (("absent", {"network": None}), ("1500", {"network": {"underlayMTU": 1500}}),
                      ("1460", {"network": {"underlayMTU": 1460}})):
        s, err = specs_of(u, vals)
        if s is None:
            fails.append("1: the umbrella probe does not render with network %s: %s" % (key, err.strip()[:200]))
            return {}
        specs[key] = s
    for key, want in (("absent", 1500), ("1500", 1500), ("1460", 1460)):
        n = specs[key].get("neutron") or {}
        got = ((n.get("conf") or {}).get("neutron") or {}).get("DEFAULT", {}).get("global_physnet_mtu"), \
            (((n.get("conf") or {}).get("plugins") or {}).get("ml2_conf") or {}).get("ml2", {}).get("path_mtu")
        if got != (want, want):
            fails.append("1: with network %s neutron's spec has global_physnet_mtu, path_mtu = %s, %s, want %s, %s"
                         % (key, got[0], got[1], want, want))
    for n in names:
        if n == "neutron":
            continue
        if not (specs["absent"].get(n) == specs["1500"].get(n) == specs["1460"].get(n)):
            fails.append("1: %s's spec changes with network.underlayMTU; only neutron may receive it" % n)
    s, err = specs_of(u, {"network": {"underlayMTU": 1460}, "componentValues": {"neutron": {"conf": {"neutron": {
        "DEFAULT": {"global_physnet_mtu": 9000}}}}}})
    if s is None:
        fails.append("1: the umbrella probe does not render with a componentValues override: %s" % err.strip()[:200])
    elif ((s.get("neutron") or {}).get("conf") or {}).get("neutron", {}).get("DEFAULT", {}).get("global_physnet_mtu") != 9000:
        fails.append("1: an explicit componentValues.neutron global_physnet_mtu does not win over network.underlayMTU")
    counts["1"] = len(names)
    return specs


def check_schema(u, fails):
    for v, ok in ((1279, False), (9217, False), ("1460", False), (1280, True), (9216, True)):
        rc, _, err = helm_template(u, "openstack-gate", {"network": {"underlayMTU": v}})
        if (rc == 0) != ok:
            fails.append("1: network.underlayMTU %r %s, want %s" % (v, "renders" if rc == 0 else "is refused",
                                                                   "rendered" if ok else "refused by the schema"))
    counts["schema"] = 5


# ---- 2. the chart ---------------------------------------------------------------------------------------------------
def render_neutron(w, values, fails, label):
    rc, out, err = helm_template(w, "neutron-gate1234", values)
    if rc != 0:
        fails.append("2: neutron%s does not render: %s" % (label, err.strip()[:200]))
        return None
    return [d for d in yaml.safe_load_all(out) if d]


def init_script(docs):
    for d in docs:
        if d.get("kind") in ("ConfigMap", "Secret"):
            t = text_of(d, "neutron-openvswitch-agent-init.sh")
            if t:
                return t
    return ""


def check_chart(w, specs, fails, label=""):
    for key in ("1460", "1500"):
        docs = render_neutron(w, specs.get(key, {}).get("neutron") or {}, fails, "%s (underlayMTU %s)" % (label, key))
        if docs is None:
            continue
        # EVERY rendered neutron.conf and ml2_conf.ini (the chart renders several objects carrying them)
        g, p = [], []
        for d in docs:
            if d.get("kind") not in ("ConfigMap", "Secret"):
                continue
            if text_of(d, "neutron.conf"):
                g.append(ini_get(text_of(d, "neutron.conf"), "DEFAULT", "global_physnet_mtu"))
            if text_of(d, "ml2_conf.ini"):
                p.append(ini_get(text_of(d, "ml2_conf.ini"), "ml2", "path_mtu"))
        if not g or not p or any(x != [key] for x in g + p):
            fails.append("2: neutron%s with underlayMTU %s renders neutron.conf [DEFAULT] global_physnet_mtu %s and "
                         "ml2_conf.ini [ml2] path_mtu %s, want [%s] in each" % (label, key, g, p, key))
        counts["files"] = counts.get("files", 0) + len(g) + len(p)
        s = init_script(docs)
        if "UNDERLAY_MTU=%s\n" % key not in s:
            fails.append("2: neutron%s's OVS agent init does not set UNDERLAY_MTU=%s" % (label, key))
        check_call(s, fails, label)
    for vals, want in (({}, "1500"), ({"conf": {"neutron": {"DEFAULT": {"global_physnet_mtu": 9000}}}}, "9000"),
                       ({"conf": {"neutron": {"DEFAULT": {"global_physnet_mtu": 9000}},
                                  "plugins": {"ml2_conf": {"ml2": {"path_mtu": 1400}}}}}, "1400")):
        docs = render_neutron(w, vals, fails, "%s (standalone %s)" % (label, json.dumps(vals)))
        if docs is not None and "UNDERLAY_MTU=%s\n" % want not in init_script(docs):
            fails.append("2: neutron%s standalone with %s: UNDERLAY_MTU is not %s (path_mtu, else global_physnet_mtu, "
                         "else 1500)" % (label, json.dumps(vals), want))
    counts["2"] = counts.get("2", 0) + 5
    return init_script(docs) if docs else ""


def check_call(s, fails, label=""):
    i = s.find(CALL)
    lip = s.find('echo "Var LOCAL_IP is empty"')
    tun = s.find('if [[ -n "${tunnel_types}" ]] ; then\n  LOCAL_IP=')
    if i < 0:
        fails.append("2: neutron%s's OVS agent init never calls check_underlay_mtu: a node with a small NIC would start "
                     "and drop full-size guest frames" % label)
    elif not (0 <= tun < lip < i):
        fails.append("2: neutron%s calls check_underlay_mtu outside the tunnel block, or before LOCAL_IP" % label)


# ---- 3. the refusal -------------------------------------------------------------------------------------------------
def function_of(s):
    m = re.search(r"^function check_underlay_mtu \{\n.*?^\}\n", s, re.M | re.S)
    return m.group(0) if m else None


def run_fn(fn, tmp, iface_mtu, want):
    sysnet = os.path.join(tmp, "sys-class-net")
    shutil.rmtree(sysnet, ignore_errors=True)
    os.makedirs(os.path.join(sysnet, "eth9"))
    if iface_mtu is not None:
        open(os.path.join(sysnet, "eth9", "mtu"), "w").write(iface_mtu)
    p = os.path.join(tmp, "check.sh")
    open(p, "w").write("set -e\n" + fn + 'check_underlay_mtu eth9 "%s" "%s"\n' % (want, sysnet))
    r = subprocess.run(["bash", p], env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True)
    return r.returncode, " ".join((r.stdout + r.stderr).split())


def check_refusal(s, tmp, fails, label=""):
    fn = function_of(s)
    if not fn:
        fails.append("3: neutron%s's OVS agent init defines no check_underlay_mtu" % label)
        return
    rc, msg = run_fn(fn, tmp, "1460\n", 1500)
    if rc == 0:
        fails.append("3: check_underlay_mtu%s passes a 1460 NIC under underlay MTU 1500; it must refuse" % label)
    else:
        missing = [t for t in ("network.underlayMTU: 1460", "1450", "1410", "eth9") if t not in msg]
        if missing:
            fails.append("3: check_underlay_mtu%s refuses without saying %s: %s" % (label, missing, msg[:300]))
        prov = re.findall(r"\b(GKE|GCP|GCE|gcloud|Google|AWS|EC2|Amazon|Azure)\b", msg)
        if prov:
            fails.append("3: the refusal names a cloud provider (%s): the blueprint is provider-agnostic" % sorted(set(prov)))
    for have, want in (("1500\n", 1500), ("9000\n", 1460), ("1460", 1460)):
        rc, msg = run_fn(fn, tmp, have, want)
        if rc != 0:
            fails.append("3: check_underlay_mtu%s refuses a %s NIC under underlay MTU %s: %s" % (label, have.strip(), want,
                                                                                               msg[:200]))
    for have in (None, "", "auto\n"):
        rc, _ = run_fn(fn, tmp, have, 1500)
        if rc == 0:
            fails.append("3: check_underlay_mtu%s passes an unreadable MTU (%r): an unmeasured NIC must refuse" % (label, have))
    counts["3"] = counts.get("3", 0) + 7


def main():
    uv = yaml.safe_load(open(os.path.join(UMBRELLA, "values.yaml")))
    names = [c["name"] for c in uv.get("components") or []]
    if not names or "neutron" not in names:
        print("FAILED MEASUREMENT: the umbrella lists no component, or no neutron")
        sys.exit(2)
    comp = open(os.path.join(UMBRELLA, "templates", "compositions.yaml")).read()
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        u = umbrella_probe(tmp, "umbrella", comp)
        specs = check_umbrella(u, names, fails)
        if u:
            check_schema(u, fails)
        if specs:
            w = stage(NEUTRON, tmp, "neutron")
            s = check_chart(w, specs, fails)
            check_refusal(s, tmp, fails)
        # 4. mutants: each must be caught by the checks above
        mutants = {
            "the comparison removed": ("chart", (INIT, 'if [ "${have}" -lt "${want}" ]; then', 'if false; then')),
            "the call removed": ("chart", (INIT, "  " + CALL + "\n", "")),
            "the umbrella's merge removed": ("umbrella", '{{- if eq $c.name "neutron" }}\n{{- $mtu := '),
        }
        for mi, (what, (kind, edit)) in enumerate(mutants.items()):
            mf = []
            if kind == "chart":
                mw = stage(NEUTRON, tmp, "neutron-mutant-%d" % mi, edit)
                if mw is None:
                    fails.append("4: mutant '%s' did not apply: its target is not in %s exactly once" % (what, INIT))
                    continue
                if specs:
                    ms = check_chart(mw, specs, mf, " (mutant)")
                    check_refusal(ms, tmp, mf, " (mutant)")
            else:
                if comp.count(edit) != 1:
                    fails.append("4: mutant '%s' did not apply" % what)
                    continue
                mc = comp.replace(edit, '{{- if eq $c.name "neutron-never" }}\n{{- $mtu := ')
                mu = umbrella_probe(tmp, "umbrella-mutant-%d" % mi, mc)
                check_umbrella(mu, names, mf)
            if not mf:
                fails.append("4: mutant '%s' passes every check: the checks cannot see it" % what)
            else:
                caught.append("%s -> %s" % (what, mf[0][:110]))
            counts["4"] = counts.get("4", 0) + 1
    for k in ("1", "2", "3", "4"):
        if not counts.get(k):
            fails.append("part %s checked nothing" % k)
    if fails:
        for f in fails:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: network.underlayMTU: %d component spec(s) compared (only neutron receives it, componentValues wins); schema "
          "bounds %d case(s); %d rendered neutron.conf/ml2_conf.ini file(s); the init's UNDERLAY_MTU in %d render(s); check_underlay_mtu "
          "run %d time(s); %d mutant(s) caught" % (counts["1"], counts.get("schema", 0), counts.get("files", 0), counts["2"], counts["3"], counts["4"]))
    for c in caught:
        print("   mutant caught: " + c)


main()
