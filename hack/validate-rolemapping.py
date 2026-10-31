#!/usr/bin/env python3
"""Every serviceContract.roleMapping entry must name a real CMP role or capability.

The schema types `cmpRole` as a free string and only *describes* it as "CMP role or capability
name (see capabilities[].name / aggregateTo)". A description is not a constraint, so a name
that exists nowhere validates cleanly and binds nothing -- which is what happened: the defaults
read openstack-viewer / openstack-operator / openstack-admin, matching neither the six
predefined roles nor any declared capability. A mapping keyed on roles that do not exist is as
inert as a label naming a role that does not exist, and reads just as plausibly.

This checks the chart's own declaration, not any deployment: the deployer's descriptor is merged
over these defaults at install time and the CMP reads the merged values. The declaration is
still worth protecting, because it is what a reader consults to learn which roles the service
understands, and because `additionalProperties: false` on serviceContract means the section is
a contract surface rather than a local convenience.

Reads values.yaml rather than the rendered chart: roleMapping is a declaration, and no template
renders it. Deliberately a separate gate from validate-capability-roles.py, which checks the
rendered ClusterRole objects -- a different artifact and a different failure.
"""
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("SKIP: PyYAML unavailable")
    sys.exit(0)

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
VALUES = ROOT / "blueprints/openstack/chart/values.yaml"

if not VALUES.is_file():
    print(f"FAILED MEASUREMENT: no values.yaml at {VALUES} -- wrong path?")
    sys.exit(2)

contract = (yaml.safe_load(VALUES.read_text()) or {}).get("serviceContract") or {}
mapping = contract.get("roleMapping") or []
caps = contract.get("capabilities") or []

if not mapping:
    print("FAILED MEASUREMENT: serviceContract.roleMapping is absent or empty; the schema "
          "requires at least one entry, so a gate that finds none is not passing, it is "
          "measuring the wrong file")
    sys.exit(2)

roles = set()
cap_names = set()
for c in caps:
    if isinstance(c, dict):
        if c.get("name"):
            cap_names.add(c["name"])
        roles.update(c.get("aggregateTo") or [])

if not roles:
    print("FAILED MEASUREMENT: no capability aggregateTo roles found; cannot tell a real role "
          "from an invented one")
    sys.exit(2)

allowed = roles | cap_names
bad = [e.get("cmpRole") for e in mapping
       if isinstance(e, dict) and e.get("cmpRole") not in allowed]

if bad:
    print(f"FAIL: {len(bad)} roleMapping cmpRole name(s) match no CMP role or capability")
    for name in bad:
        print(f"      {name!r}")
    print()
    print("      cmpRole must name a predefined role or a declared capability, or the entry")
    print("      maps nothing however plausible it reads.")
    print("      roles:        " + ", ".join(sorted(roles)))
    print("      capabilities: " + ", ".join(sorted(cap_names)))
    sys.exit(1)

print(f"OK: all {len(mapping)} roleMapping entries name a real CMP role or capability")
