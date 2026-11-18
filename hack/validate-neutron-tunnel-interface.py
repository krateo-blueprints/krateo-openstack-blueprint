#!/usr/bin/env python3
"""neutron finds its VXLAN tunnel interface on any node; no provider's NIC name is assumed.

    hack/validate-neutron-tunnel-interface.py [repo-root]

network.interface.tunnel defaulted to ens4, a GCE NIC name. On a node without ens4 the OVS agent's init found no
address ("Var LOCAL_IP is empty") and the agent never started. Its init script already detects the interface of the route
to network.interface.tunnel_network_cidr (0/0: the default route) when the field is empty; the ens4 default skipped
that. Checked here:

  1. THE DEFAULT IS EMPTY. values.yaml sets network.interface.tunnel to "" (or null), and values.schema.json declares
     no default for it (a CRD default is written into every stored object, #179) and names no provider's NIC.
  2. THE FALLBACK IS RENDERED. neutron rendered at its defaults: neutron-openvswitch-agent-init.sh assigns an empty
     tunnel_interface and, under `if [ -z "${tunnel_interface}" ]`, takes it from `ip -4 route list`.
  3. THE FALLBACK RUNS. That block, as rendered, is run with a stand-in `ip` whose default route leaves through a NIC
     no provider names (gate0): it picks gate0. Rendered with network.interface.tunnel eth9, it picks eth9 (an
     explicit NIC still wins), and a tunnel_network_cidr is routed through that network's NIC.

Fails when helm or PyYAML is missing, and when the block cannot be found.
"""
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
CHART = os.path.join(ROOT, "blueprints", "neutron", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
PROVIDER_NICS = re.compile(r"\b(ens4|ens5|eth0|enp\d+s\d+|GKE|GCE|GCP|AWS|EC2|Azure)\b")
failures = []
checked = []


def fail(m):
    failures.append(m)


def stage(tmp):
    w = os.path.join(tmp, "neutron")
    shutil.copytree(CHART, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
    os.makedirs(os.path.join(w, "charts"), exist_ok=True)
    shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    return w


def init_script(w, values):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
    try:
        r = subprocess.run(["helm", "template", "neutron", w, "--namespace", "openstack", "--kube-version", "1.36.0",
                            "-f", f.name, "--show-only", "templates/configmap-bin.yaml"], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    if r.returncode != 0:
        fail("neutron does not render with %s: %s" % (json.dumps(values), r.stderr.strip()[:300]))
        return None
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    return next((d["data"].get("neutron-openvswitch-agent-init.sh") for d in docs if d.get("kind") == "ConfigMap"
                 and "neutron-openvswitch-agent-init.sh" in (d.get("data") or {})), None)


def tunnel_block(script):
    m = re.search(r'^tunnel_types=.*?\n(if \[\[ -n "\$\{tunnel_types\}" \]\] ; then\n.*?\n)fi\n', script, re.M | re.S)
    return (m.group(0) if m else None)


STUB_IP = """#!/bin/bash
# stand-in ip: `ip -4 route list <cidr>`
if [ "$1 $2 $3" = "-4 route list" ]; then
  case "$4" in
    0/0) echo "default via 192.0.2.1 dev gate0 proto dhcp src 192.0.2.10 metric 100" ;;
    198.51.100.0/24) echo "198.51.100.0/24 dev gate1 proto kernel scope link src 198.51.100.7" ;;
  esac
  exit 0
fi
exit 1
"""


def run_block(block, tmp, label, want):
    d = tempfile.mkdtemp(dir=tmp)
    ip = os.path.join(d, "ip")
    open(ip, "w").write(STUB_IP)
    os.chmod(ip, 0o755)
    p = os.path.join(d, "block.sh")
    open(p, "w").write(block + 'echo "TUNNEL=${tunnel_interface}"\n')
    r = subprocess.run(["bash", p], capture_output=True, text=True, env={"PATH": d + ":/usr/bin:/bin"})
    got = (re.findall(r"^TUNNEL=(.*)$", r.stdout, re.M) or [None])[-1]
    checked.append(label)
    if r.returncode != 0 or got != want:
        fail("3: %s: the rendered tunnel block picks %r (exit %d), want %r: %s" % (label, got, r.returncode, want,
                                                                                  r.stderr.strip()[:200]))


def main():
    values = yaml.safe_load(open(os.path.join(CHART, "values.yaml")))
    tunnel = (((values.get("network") or {}).get("interface") or {})).get("tunnel", "missing")
    if tunnel not in ("", None):
        fail("1: values.yaml sets network.interface.tunnel to %r: a NIC name holds on some nodes only; empty lets the "
             "OVS agent use the default route's interface on any node" % tunnel)
    schema = json.load(open(os.path.join(CHART, "values.schema.json")))
    prop = (((((schema.get("properties") or {}).get("network") or {}).get("properties") or {}).get("interface") or {})
            .get("properties") or {}).get("tunnel")
    if prop is None:
        fail("1: values.schema.json does not declare network.interface.tunnel, so the check had no input")
    else:
        if "default" in prop:
            fail("1: values.schema.json declares a default for network.interface.tunnel (%r): a CRD default is written "
                 "into every stored object (#179)" % prop["default"])
        named = PROVIDER_NICS.findall(json.dumps(prop))
        if named:
            fail("1: values.schema.json's network.interface.tunnel names %s: the blueprint is provider-agnostic"
                 % sorted(set(named)))
    with tempfile.TemporaryDirectory() as tmp:
        w = stage(tmp)
        script = init_script(w, {})
        block = tunnel_block(script or "")
        if not block:
            fail("2: the rendered neutron-openvswitch-agent-init.sh has no tunnel_types block, so the check had no input")
        else:
            if not re.search(r'^\s*tunnel_interface=""\s*$', block, re.M):
                fail("2: the rendered init script does not assign an empty tunnel_interface at defaults: %s"
                     % (re.findall(r"^\s*tunnel_interface=.*$", block, re.M) or ["none"])[0].strip())
            if not re.search(r'if \[ -z "\$\{tunnel_interface\}" \] ; then.*?ip -4 route list', block, re.S):
                fail("2: the rendered init script has no default-route fallback for an empty tunnel_interface")
            run_block(block, tmp, "defaults", "gate0")
        for label, vals, want in (("network.interface.tunnel eth9", {"network": {"interface": {"tunnel": "eth9"}}}, "eth9"),
                                  ("tunnel_network_cidr 198.51.100.0/24",
                                   {"network": {"interface": {"tunnel_network_cidr": "198.51.100.0/24"}}}, "gate1")):
            b = tunnel_block(init_script(w, vals) or "")
            if not b:
                fail("2: with %s the rendered init script has no tunnel_types block" % label)
            else:
                run_block(b, tmp, label, want)
    if not checked:
        fail("3: the tunnel block was never run")
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: network.interface.tunnel defaults to empty with no schema default; the rendered init script falls back "
          "to the default route's NIC, run in %d case(s) (%s)" % (len(checked), ", ".join(checked)))


main()
