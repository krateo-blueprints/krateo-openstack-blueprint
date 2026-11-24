#!/usr/bin/env python3
"""heat checks the signature of a CloudFormation request as its service user (#481).

    hack/validate-heat-ec2.py [repo-root]

Measured on GKE 2026-11-24: heat's CloudFormation API refused every signed request with 403. Keystone's /v3/ec2tokens requires an
authenticated caller, and heat's [ec2authtoken] had no credentials. With auth_section = keystone_authtoken and Keystone's
address without its path (heat appends /v3/ec2tokens; with the catalog's URL it asked /v3/v3/ec2tokens), it answered 200.

heat is rendered at its defaults, and with another Keystone path. In heat.conf:
  * [ec2authtoken] auth_section names a section that has an auth_type and an auth_url;
  * valid_interfaces is internal;
  * endpoint_override is the internal Keystone's scheme, host and port, with no path.
With endpoint_override set by the user, that value is kept. Fails when helm or PyYAML is missing.
"""
import base64
import configparser
import os
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")


def main():
    if shutil.which("helm") is None:
        print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
        return 2
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        w = os.path.join(tmp, "heat")
        shutil.copytree(os.path.join(ROOT, "blueprints", "heat", "chart"), w)
        cy = os.path.join(w, "Chart.yaml")
        text = open(cy).read()
        open(cy, "w").write(text.replace("CHART_VERSION", "0.0.0-gate"))
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(os.path.join(ROOT, "charts", "helm-toolkit"), os.path.join(w, "charts", "helm-toolkit"))

        def conf(*extra):
            r = subprocess.run(["helm", "template", "heat", w, "--namespace", "openstack"] + list(extra), capture_output=True, text=True)
            if r.returncode != 0:
                fails.append("heat does not render: %s" % r.stderr.strip()[-300:])
                return {}
            for d in yaml.safe_load_all(r.stdout):
                if d and d.get("kind") == "Secret" and "heat.conf" in (d.get("data") or {}):
                    p = configparser.RawConfigParser(strict=False, default_section="\0none", interpolation=None)
                    p.read_string(base64.b64decode(d["data"]["heat.conf"]).decode())
                    return {(s, o): v for s in p.sections() for o, v in p.items(s)}
            fails.append("heat renders no heat.conf")
            return {}
        cases = [("defaults", ()), ("another Keystone path", ("--set", "endpoints.identity.path.default=/identity/v3"))]
        for name, extra in cases:
            c = conf(*extra)
            sec = c.get(("ec2authtoken", "auth_section"))
            if not sec or not c.get((sec, "auth_type")) or not c.get((sec, "auth_url")):
                fails.append("%s: [ec2authtoken] auth_section is %r, which has auth_type %r and auth_url %r" % (name, sec, c.get((sec, "auth_type")), c.get((sec, "auth_url"))))
            if c.get(("ec2authtoken", "valid_interfaces")) != "internal":
                fails.append("%s: valid_interfaces is %r" % (name, c.get(("ec2authtoken", "valid_interfaces"))))
            u = urlparse(c.get(("ec2authtoken", "endpoint_override")) or "")
            if u.hostname != "keystone-api.openstack.svc.cluster.local" or u.port != 5000 or u.path not in ("", "/"):
                fails.append("%s: endpoint_override is %r, want the internal Keystone with no path" % (name, u.geturl()))
        c = conf("--set", "conf.heat.ec2authtoken.endpoint_override=http://elsewhere:1")
        if c.get(("ec2authtoken", "endpoint_override")) != "http://elsewhere:1":
            fails.append("a user's endpoint_override is not kept: %r" % c.get(("ec2authtoken", "endpoint_override")))
    for f in fails:
        print("FAIL: " + f)
    print("heat's [ec2authtoken]: %d render(s); %d failure(s)" % (len(cases) + 1, len(fails)))
    return 1 if fails else 0


sys.exit(main())
