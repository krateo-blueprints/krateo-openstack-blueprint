#!/usr/bin/env python3
"""Horizon keeps sessions in its database, and the back-channel logout receiver is really wired.

The receiver can only end a user's sessions if they are rows it can read, and it only exists in
Horizon if four separately rendered pieces line up: the package in horizon-bin, its mounts, the
copy step in horizon.sh, and the local_settings.d snippet that installs the app and mounts its
URL. Horizon executes local_settings.d snippets inside a try/except that only LOGS, so a snippet
that raises leaves Horizon serving normally with no receiver -- and Keycloak's logouts answered
404 by a Horizon that reports Ready. Nothing short of a live logout would show it, so this
renders the chart and checks each piece, and EXECUTES the snippet.

Default render (receiver disabled):
  1. local_settings sets SESSION_ENGINE to django.contrib.sessions.backends.db;
  2. the session table is created by a bounded hook: the horizon-db-sync Job is a Helm hook
     with activeDeadlineSeconds, runs db-sync.sh, which execs `manage.py migrate` with the same
     local_settings mounted (django.contrib.sessions is in Horizon's INSTALLED_APPS, so migrate
     creates django_session), and the dashboard waits for that Job;
  3. no receiver piece is rendered.
Enabled render (hack/tests/horizon_backchannel/enabled-values.yaml):
  4. the snippet executes, appends the app to ADD_INSTALLED_APPS and its urls module to
     AUTHENTICATION_URLS, adds the Service names to ALLOWED_HOSTS, and carries the values;
  5. urls.py routes backchannel-logout/, so with Horizon's ^auth/ mount the receiver answers
     POST /auth/backchannel-logout/;
  6. horizon-bin carries every file of files/krateo_backchannel/, byte-identical;
  7. the Deployment mounts the snippet, every package file, and the service-credential Secret
     (which the chart renders);
  8. horizon.sh installs the package and links the snippet before its first manage.py call;
  9. the chart REFUSES to render the receiver with the cache session engine, without an
     issuer, or with a forward_url that is not a back-channel endpoint.

Fails when helm or PyYAML is missing, or when a render produces no documents.

Usage: hack/validate-horizon-backchannel-render.py [repo-root]
"""
import base64
import os
import pathlib
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

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHART = ROOT / "blueprints/horizon/chart"
TOOLKIT = ROOT / "charts/helm-toolkit"
ENABLED = ROOT / "hack/tests/horizon_backchannel/enabled-values.yaml"
PACKAGE = CHART / "files/krateo_backchannel"
NAMESPACE = "openstack"
DB_ENGINE = "django.contrib.sessions.backends.db"
APP = "openstack_dashboard.local.krateo_backchannel"
SNIPPET = "_90_krateo_backchannel.py"
PKG_DIR = "/etc/openstack-dashboard/krateo_backchannel"
CREDS_DIR = "/etc/openstack-dashboard/backchannel-service-auth"

if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
for path in (CHART, TOOLKIT, ENABLED, PACKAGE):
    if not path.exists():
        print("FAILED MEASUREMENT: %s does not exist" % path)
        sys.exit(2)

failures = []


def fail(msg):
    failures.append(msg)


def helm_template(*value_args):
    """(returncode, stdout, stderr) of rendering the horizon chart as Krateo renders it."""
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp) / "chart"
        shutil.copytree(CHART, work)
        cy = work / "Chart.yaml"
        cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
        (work / "charts").mkdir(exist_ok=True)
        shutil.copytree(TOOLKIT, work / "charts/helm-toolkit", dirs_exist_ok=True)
        args = ["helm", "template", "horizon", str(work), "--namespace", NAMESPACE,
                "--kube-version", "1.36.0"]
        for v in value_args:
            args += ["--set" if "=" in v and not v.endswith(".yaml") else "-f", v]
        out = subprocess.run(args, capture_output=True, text=True)
        return out.returncode, out.stdout, out.stderr


def render(label, *value_args):
    """The rendered documents, or None with the failure recorded (the chart is broken)."""
    rc, out, err = helm_template(*value_args)
    if rc != 0:
        fail("%s: helm template failed: %s" % (label, err.strip()[:500]))
        return None
    docs = [d for d in yaml.safe_load_all(out) if d]
    if not docs:
        print("FAILED MEASUREMENT: the %s render produced no documents" % label)
        sys.exit(2)
    return docs


def find(docs, kind, name):
    for d in docs:
        if d.get("kind") == kind and d.get("metadata", {}).get("name") == name:
            return d
    return None


def secret_file(docs, name, key):
    s = find(docs, "Secret", name)
    if s is None or key not in (s.get("data") or {}):
        return None
    return base64.b64decode(s["data"][key]).decode()


def container(docs, kind, name, cname):
    obj = find(docs, kind, name)
    if obj is None:
        return None, None
    spec = obj["spec"]["template"]["spec"]
    for c in spec.get("containers", []):
        if c["name"] == cname:
            return c, spec
    return None, spec


def check_session_engine(docs, label):
    ls = secret_file(docs, "horizon-etc", "local_settings")
    if ls is None:
        fail("%s: horizon-etc has no local_settings" % label)
        return
    m = re.findall(r"^SESSION_ENGINE\s*=\s*['\"]([^'\"]+)['\"]", ls, re.M)
    if m != [DB_ENGINE]:
        fail("%s: local_settings SESSION_ENGINE is %s, want exactly one %s" % (label, m, DB_ENGINE))


def check_migration(docs, values):
    job = find(docs, "Job", "horizon-db-sync")
    if job is None:
        fail("default: no horizon-db-sync Job, so nothing creates the django_session table")
        return
    ann = job["metadata"].get("annotations") or {}
    if "helm.sh/hook" not in ann:
        fail("default: horizon-db-sync is not a Helm hook")
    if not job["spec"].get("activeDeadlineSeconds"):
        fail("default: horizon-db-sync has no activeDeadlineSeconds")
    c, _spec = container(docs, "Job", "horizon-db-sync", "horizon-db-sync")
    if c is None:
        fail("default: horizon-db-sync has no horizon-db-sync container")
        return
    if "/tmp/db-sync.sh" not in " ".join(c.get("command") or []):
        fail("default: horizon-db-sync does not run /tmp/db-sync.sh")
    mounts = {m["mountPath"]: m for m in c.get("volumeMounts", [])}
    ls = mounts.get("/etc/openstack-dashboard/local_settings")
    if not ls or ls.get("subPath") != "local_settings":
        fail("default: horizon-db-sync does not mount Horizon's local_settings, so it would "
             "migrate a different database than the sessions live in")
    cm = find(docs, "ConfigMap", "horizon-bin")
    script = (cm or {}).get("data", {}).get("db-sync.sh", "")
    if not re.search(r"^exec /tmp/manage.py migrate\b", script, re.M):
        fail("default: db-sync.sh does not exec `manage.py migrate`")
    jobs = (((values.get("dependencies") or {}).get("static") or {}).get("dashboard") or {}).get("jobs") or []
    if "horizon-db-sync" not in jobs:
        fail("default: the dashboard does not wait for horizon-db-sync (dependencies.static."
             "dashboard.jobs), so it could serve before the session table exists")


def run_snippet(text):
    env = {
        "ADD_INSTALLED_APPS": [],
        "AUTHENTICATION_URLS": ["openstack_auth.urls"],
        "ALLOWED_HOSTS": ["*"],
    }
    exec(compile(text, SNIPPET, "exec"), env)   # noqa: S102 -- our own rendered settings
    return env


def main():
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    enabled_values = yaml.safe_load(ENABLED.read_text())
    bcl = enabled_values["conf"]["horizon"]["backchannel_logout"]
    package_files = sorted(p.name for p in PACKAGE.glob("*.py"))
    if "urls.py" not in package_files or "views.py" not in package_files:
        print("FAILED MEASUREMENT: %s lacks urls.py/views.py" % PACKAGE)
        sys.exit(2)

    # -- default render
    default = render("default")
    if default is not None:
        check_default(default, values)

    # -- enabled render
    docs = render("enabled", str(ENABLED))
    if docs is not None:
        check_enabled(docs, values, bcl, package_files)

    check_refusals()
    report()


def check_default(default, values):
    check_session_engine(default, "default")
    check_migration(default, values)
    if secret_file(default, "horizon-etc", SNIPPET) is not None:
        fail("default: the receiver snippet renders although backchannel_logout is disabled")
    cm = find(default, "ConfigMap", "horizon-bin") or {}
    if any(k.startswith("krateo_backchannel.") for k in cm.get("data", {})):
        fail("default: horizon-bin carries the receiver although it is disabled")


def check_enabled(docs, values, bcl, package_files):
    check_session_engine(docs, "enabled")

    snippet = secret_file(docs, "horizon-etc", SNIPPET)
    if snippet is None:
        fail("enabled: horizon-etc has no %s" % SNIPPET)
    else:
        try:
            env = run_snippet(snippet)
        except Exception as e:  # the failure Horizon would swallow
            fail("enabled: the settings snippet raises %s: %s" % (e.__class__.__name__, e))
            env = None
        if env is not None:
            if env["ADD_INSTALLED_APPS"] != [APP]:
                fail("enabled: ADD_INSTALLED_APPS is %r" % env["ADD_INSTALLED_APPS"])
            if env["AUTHENTICATION_URLS"] != ["openstack_auth.urls", APP + ".urls"]:
                fail("enabled: AUTHENTICATION_URLS is %r" % env["AUTHENTICATION_URLS"])
            want_host = "horizon-int.%s.svc" % NAMESPACE
            if want_host not in env["ALLOWED_HOSTS"]:
                fail("enabled: ALLOWED_HOSTS lacks %s, the host Keycloak calls" % want_host)
            cfg = env.get("KRATEO_BACKCHANNEL") or {}
            for key in ("issuer", "audience", "jwks_url", "forward_url"):
                if cfg.get(key) != bcl[key]:
                    fail("enabled: KRATEO_BACKCHANNEL[%r] does not carry the value" % key)
            if cfg.get("require_typ") is not True or not isinstance(cfg.get("iat_skew_seconds"), int):
                fail("enabled: KRATEO_BACKCHANNEL types are wrong: require_typ=%r iat_skew=%r"
                     % (cfg.get("require_typ"), cfg.get("iat_skew_seconds")))
            if cfg.get("service_credentials_dir") != CREDS_DIR:
                fail("enabled: service_credentials_dir is %r" % cfg.get("service_credentials_dir"))

    urls = (PACKAGE / "urls.py").read_text()
    if not re.search(r"""path\(\s*["']backchannel-logout/["']\s*,\s*views\.backchannel_logout""", urls):
        fail("urls.py does not route backchannel-logout/ to views.backchannel_logout")
    if "@csrf_exempt" not in (PACKAGE / "views.py").read_text():
        fail("views.py: the receiver is not csrf_exempt; Keycloak sends no CSRF token")

    cm = find(docs, "ConfigMap", "horizon-bin") or {}
    data = cm.get("data", {})
    for name in package_files:
        key = "krateo_backchannel." + name
        if key not in data:
            fail("enabled: horizon-bin lacks %s" % key)
        elif data[key].rstrip("\n") != (PACKAGE / name).read_text().rstrip("\n"):
            fail("enabled: horizon-bin %s differs from files/krateo_backchannel/%s" % (key, name))

    c, spec = container(docs, "Deployment", "horizon", "horizon")
    if c is None:
        fail("enabled: no horizon container in the horizon Deployment")
    else:
        mounts = {m["mountPath"]: m for m in c.get("volumeMounts", [])}
        m = mounts.get("/etc/openstack-dashboard/local_settings.d/" + SNIPPET)
        if not m or m.get("name") != "horizon-etc" or m.get("subPath") != SNIPPET:
            fail("enabled: the snippet is not mounted into local_settings.d")
        for name in package_files:
            m = mounts.get("%s/%s" % (PKG_DIR, name))
            if not m or m.get("name") != "horizon-bin" or m.get("subPath") != "krateo_backchannel." + name:
                fail("enabled: package file %s is not mounted" % name)
        m = mounts.get(CREDS_DIR)
        vols = {v["name"]: v for v in spec.get("volumes", [])}
        secret_name = values["secrets"]["identity"]["admin"]
        if not m or (vols.get(m["name"]) or {}).get("secret", {}).get("secretName") != secret_name:
            fail("enabled: the service credentials (%s) are not mounted at %s" % (secret_name, CREDS_DIR))
        if find(docs, "Secret", secret_name) is None:
            fail("enabled: the chart does not render %s" % secret_name)

    sh = data.get("horizon.sh", "")
    copy = sh.find("cp %s/*.py ${SITE_PACKAGES_ROOT}/openstack_dashboard/local/krateo_backchannel/" % PKG_DIR)
    link = sh.find("/etc/openstack-dashboard/local_settings.d/%s ${SITE_PACKAGES_ROOT}/openstack_dashboard/local/local_settings.d/%s" % (SNIPPET, SNIPPET))
    first_manage = sh.find("/tmp/manage.py")
    if first_manage < 0:
        fail("enabled: horizon.sh never calls /tmp/manage.py, so the ordering check measured nothing")
    if copy < 0:
        fail("enabled: horizon.sh does not copy the package into openstack_dashboard/local/krateo_backchannel")
    if link < 0:
        fail("enabled: horizon.sh does not link the snippet into local_settings.d")
    if first_manage >= 0 and (copy < 0 or link < 0 or max(copy, link) > first_manage):
        fail("enabled: horizon.sh runs manage.py before installing the receiver")


# -- the chart refuses a receiver that cannot work
REFUSALS = {
    "cache session engine": ["conf.horizon.local_settings.config.session_engine="
                             "django.contrib.sessions.backends.cache"],
    "no issuer": ["conf.horizon.backchannel_logout.issuer="],
    "forward_url without ?logout=backchannel": [
        "conf.horizon.backchannel_logout.forward_url=http://keystone-api:5000/redirect"],
}


def check_refusals():
    for label, sets in REFUSALS.items():
        rc, _out, err = helm_template(str(ENABLED), *sets)
        if rc == 0:
            fail("refusal: the chart renders a receiver with %s" % label)
        elif "conf.horizon.backchannel_logout" not in err:
            fail("refusal: rendering with %s failed for another reason: %s"
                 % (label, err.strip()[:300]))


def report():
    if failures:
        print("FAIL: %d problem(s) with Horizon's DB sessions / back-channel logout receiver"
              % len(failures))
        for f in failures:
            print("  " + f)
        sys.exit(1)
    print("OK: Horizon sessions use %s, the session table is created by the bounded db-sync "
          "hook, the back-channel logout receiver is installed, mounted and routed at "
          "/auth/backchannel-logout/, and %d unworkable configurations are refused"
          % (DB_ENGINE, len(REFUSALS)))


main()
