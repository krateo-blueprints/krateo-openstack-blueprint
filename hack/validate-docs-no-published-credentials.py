#!/usr/bin/env python3
"""No document tells a reader to log in with an OpenStack-Helm published password (#228).

Since 0.3.53 the Keystone admin password is generated per install, and since 0.3.63-0.3.67 every service user's is
too. Yet the README, docs/usage.md, docs/quickstart.md, both quickstarts and the identity-profile example still said
"log in as `admin` / `password`" and passed `--env OS_PASSWORD=password` (found 2026-11-16). That instruction fails on
every install, and it publishes the very default #228 removed.

The published passwords are read from the charts themselves: every non-empty endpoints.identity.auth.<user>.password
in blueprints/*/chart/values.yaml. Every repo-authored Markdown file (vendored chart trees excluded) fails when it
passes one as a credential:
  - OS_PASSWORD=<value>                         (an openrc or `kubectl run --env`)
  - --password <value> / --os-password <value>  (a CLI argument)
  - as `<user>` / `<value>`                     (a login instruction)
  - password: <value> / os_password: <value>    (a YAML key in a clouds.yaml or values snippet)
A key-name list such as `username`/`password` is not a credential and is not matched. No file or no published password
read is a failure: an empty scan is not a pass. usage: validate-docs-no-published-credentials.py <repo>
"""
import glob
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("FAIL: PyYAML is required by validate-docs-no-published-credentials.py and is not installed")

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

published = set()
for vf in glob.glob(os.path.join(ROOT, "blueprints", "*", "chart", "values.yaml")):
    v = yaml.safe_load(open(vf)) or {}
    auth = (((v.get("endpoints") or {}).get("identity") or {}).get("auth") or {})
    for user in auth.values():
        pw = (user or {}).get("password") if isinstance(user, dict) else None
        if isinstance(pw, str) and len(pw) >= 4 and "{{" not in pw:
            published.add(pw)

files = subprocess.run(["git", "-C", ROOT, "ls-files", "*.md"], capture_output=True, text=True, check=True).stdout.split()
files = [f for f in files if not re.match(r"^blueprints/[^/]+/chart/", f)]
if not published or not files:
    print("FAILED MEASUREMENT: %d published password(s) read, %d document(s) found; nothing was checked"
          % (len(published), len(files)))
    sys.exit(2)

PATTERNS = [
    re.compile(r"""\bOS_PASSWORD=['"]?([^\s'"\\]+)"""),
    re.compile(r"""--(?:os-)?password[= ]['"]?([^\s'"\\]+)"""),
    re.compile(r"""\bas `[^`]+` */ *`([^`]+)`"""),
    # a YAML key in a snippet (clouds.yaml, values): `password: <value>`, `os_password: <value>`, also as a list item
    re.compile(r"""^\s*(?:-\s*)?(?:os_)?password:\s*['"]?([^\s'"#]+)['"]?\s*(?:#.*)?$"""),
]
failures = []
for f in files:
    for n, line in enumerate(open(os.path.join(ROOT, f), errors="replace"), 1):
        for rx in PATTERNS:
            for m in rx.finditer(line):
                if m.group(1) in published:
                    failures.append("%s:%d passes a published OpenStack-Helm password as a credential: %s"
                                    % (f, n, line.strip()[:120]))
for x in failures:
    print("FAIL: " + x)
if failures:
    print("Read the generated one instead, e.g. kubectl -n openstack get secret openstack-credential-identity-admin "
          "-o jsonpath='{.data.password}' | base64 -d, or envFrom the keystone-keystone-admin Secret.")
    sys.exit(1)
print("OK: %d document(s) pass no published OpenStack-Helm password (%d distinct published value(s) read from the charts)"
      % (len(files), len(published)))
