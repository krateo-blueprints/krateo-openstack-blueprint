#!/usr/bin/env python3
"""Horizon sends every user to a page they can open (#247), and that is really wired.

Horizon's default home is the default dashboard's default panel. On a deployment without compute
(profile identity) that is Project > Overview, which requires openstack.services.compute: every
login landed on 403. The chart's fix only exists in Horizon if four separately rendered pieces line
up -- the package in horizon-bin, its mounts, the copy step in horizon.sh, and the local_settings.d
snippet -- and Horizon executes snippets inside a try/except that only LOGS, so a broken snippet
leaves the old 403 with nothing failing. This renders the chart and checks each piece, EXECUTES the
snippet, then runs the view's unit tests (hack/tests/horizon_home) through Django.

Renders: default values and the back-channel-enabled values (the two snippets must coexist):
  1. horizon-bin carries every file of files/krateo_home/, byte-identical;
  2. horizon-etc carries the snippet; executed, it appends the package's urls module to
     AUTHENTICATION_URLS and sets HORIZON_CONFIG['user_home'] to <WEBROOT>/auth/home/ (WEBROOT with
     and without its trailing slash, as Horizon has it when snippets run);
  3. urls.py routes home/, so with Horizon's ^auth/ mount the view answers GET /auth/home/;
  4. the horizon Deployment's container mounts the snippet and every package file;
  5. horizon.sh installs the package and links the snippet before its first manage.py call.

Fails when helm, PyYAML or Django is missing, when a render produces no documents, when no unit test
ran and when any was skipped.

Usage: hack/validate-horizon-home.py [repo-root]
"""
import base64
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)
try:
    import django  # noqa: F401
except ImportError:
    print("FAILED MEASUREMENT: Django unavailable, so the view's tests cannot run (pip install Django)")
    sys.exit(2)

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints/horizon/chart"
TOOLKIT = ROOT / "charts/helm-toolkit"
ENABLED = ROOT / "hack/tests/horizon_backchannel/enabled-values.yaml"
PACKAGE = CHART / "files/krateo_home"
TESTS = ROOT / "hack/tests/horizon_home"
NAMESPACE = "openstack"
SNIPPET = "_80_krateo_home.py"
PKG_DIR = "/etc/openstack-dashboard/krateo_home"
URLS_MODULE = "openstack_dashboard.local.krateo_home.urls"

if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
for path in (CHART, TOOLKIT, ENABLED, PACKAGE, TESTS):
    if not path.exists():
        print("FAILED MEASUREMENT: %s does not exist" % path)
        sys.exit(2)

failures = []


def fail(msg):
    failures.append(msg)


def render(label, *values_files):
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp) / "chart"
        shutil.copytree(CHART, work)
        cy = work / "Chart.yaml"
        cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
        (work / "charts").mkdir(exist_ok=True)
        shutil.copytree(TOOLKIT, work / "charts/helm-toolkit", dirs_exist_ok=True)
        args = ["helm", "template", "horizon", str(work), "--namespace", NAMESPACE, "--kube-version", "1.36.0"]
        for f in values_files:
            args += ["-f", f]
        out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        fail("%s: helm template failed: %s" % (label, out.stderr.strip()[:500]))
        return None
    docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    if not docs:
        print("FAILED MEASUREMENT: the %s render produced no documents" % label)
        sys.exit(2)
    return docs


def find(docs, kind, name):
    for d in docs:
        if d.get("kind") == kind and d.get("metadata", {}).get("name") == name:
            return d
    return None


def run_snippet(text, webroot):
    env = {"AUTHENTICATION_URLS": ["openstack_auth.urls"], "WEBROOT": webroot,
           "HORIZON_CONFIG": {"user_home": "openstack_dashboard.views.get_user_home"}}
    exec(compile(text, SNIPPET, "exec"), env)   # noqa: S102 -- our own rendered settings
    return env


def check(docs, label, package_files):
    cm = find(docs, "ConfigMap", "horizon-bin")
    data = (cm or {}).get("data") or {}
    for name in package_files:
        if data.get("krateo_home.%s" % name) != (PACKAGE / name).read_text():
            fail("%s: horizon-bin key krateo_home.%s is missing or differs from files/krateo_home/%s"
                 % (label, name, name))

    etc = find(docs, "Secret", "horizon-etc")
    raw = ((etc or {}).get("data") or {}).get(SNIPPET)
    if raw is None:
        fail("%s: horizon-etc has no %s" % (label, SNIPPET))
    else:
        text = base64.b64decode(raw).decode()
        for webroot, want in (("/", "/auth/home/"), ("/dashboard", "/dashboard/auth/home/"),
                              ("/dashboard/", "/dashboard/auth/home/")):
            try:
                env = run_snippet(text, webroot)
            except Exception as e:  # noqa: BLE001 -- Horizon would only log this
                fail("%s: the snippet raises %s: %s (Horizon would log it and keep the 403 home)"
                     % (label, e.__class__.__name__, e))
                break
            if env["HORIZON_CONFIG"].get("user_home") != want:
                fail("%s: with WEBROOT %r the snippet sets user_home %r, want %r"
                     % (label, webroot, env["HORIZON_CONFIG"].get("user_home"), want))
            if env["AUTHENTICATION_URLS"] != ["openstack_auth.urls", URLS_MODULE]:
                fail("%s: AUTHENTICATION_URLS after the snippet is %r" % (label, env["AUTHENTICATION_URLS"]))

    dep = find(docs, "Deployment", "horizon")
    c = None
    for cont in ((dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []):
        if cont["name"] == "horizon":
            c = cont
    if c is None:
        fail("%s: no horizon container in Deployment horizon" % label)
    else:
        mounts = {m["mountPath"]: m for m in c.get("volumeMounts", [])}
        m = mounts.get("/etc/openstack-dashboard/local_settings.d/%s" % SNIPPET)
        if not m or m.get("name") != "horizon-etc" or m.get("subPath") != SNIPPET:
            fail("%s: the horizon container does not mount %s from horizon-etc" % (label, SNIPPET))
        for name in package_files:
            m = mounts.get("%s/%s" % (PKG_DIR, name))
            if not m or m.get("name") != "horizon-bin" or m.get("subPath") != "krateo_home.%s" % name:
                fail("%s: the horizon container does not mount %s/%s from horizon-bin" % (label, PKG_DIR, name))

    script = data.get("horizon.sh", "")
    copy = script.find("cp %s/*.py ${SITE_PACKAGES_ROOT}/openstack_dashboard/local/krateo_home/" % PKG_DIR)
    link = script.find("local_settings.d/%s ${SITE_PACKAGES_ROOT}/openstack_dashboard/local/local_settings.d/%s"
                       % (SNIPPET, SNIPPET))
    manage = script.find("manage.py")
    if copy < 0:
        fail("%s: horizon.sh does not copy the krateo_home package" % label)
    if link < 0:
        fail("%s: horizon.sh does not link %s into local_settings.d" % (label, SNIPPET))
    if manage >= 0 and (0 <= manage < copy or 0 <= manage < link):
        fail("%s: horizon.sh calls manage.py before installing the package and snippet" % label)


def main():
    package_files = sorted(p.name for p in PACKAGE.glob("*.py"))
    if not {"__init__.py", "urls.py", "views.py"} <= set(package_files):
        print("FAILED MEASUREMENT: %s lacks __init__.py/urls.py/views.py" % PACKAGE)
        sys.exit(2)
    if not re.search(r'path\(\s*"home/"\s*,\s*views\.home\b', (PACKAGE / "urls.py").read_text()):
        fail('files/krateo_home/urls.py does not route "home/" to views.home')
    for label, files in (("default", ()), ("back-channel enabled", (str(ENABLED),))):
        docs = render(label, *files)
        if docs is not None:
            check(docs, label, package_files)

    sys.path[0:0] = [str(TESTS), str(CHART / "files")]
    suite = unittest.defaultTestLoader.discover(str(TESTS), pattern="test_*.py", top_level_dir=str(TESTS))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if result.testsRun == 0:
        print("FAILED MEASUREMENT: discovered 0 tests under %s" % TESTS)
        sys.exit(2)
    if result.skipped:
        fail("%d view test(s) skipped" % len(result.skipped))
    if not result.wasSuccessful():
        fail("%d failure(s), %d error(s) in %d view tests"
             % (len(result.failures), len(result.errors), result.testsRun))

    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: the home page is wired in both renders and %d view tests passed" % result.testsRun)


main()
