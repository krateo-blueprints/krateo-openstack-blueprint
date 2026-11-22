#!/usr/bin/env python3
"""openstack-monitoring (issue #312): every alert can fire, says what to do, and is about a metric that exists; its
credentials reach only their readers; and the umbrella's `monitoring` value is the one switch, off byte-for-byte.

    hack/validate-openstack-monitoring.py [repo-root]

The alerts are defined in ONE file, blueprints/openstack-monitoring/chart/files/alerts.yaml, and rendered as
alert-provider CRs. Everything below is checked on what the charts RENDER, not on that file's intent.

  1. ALERTS (the chart rendered at defaults, which select every component an alert names):
     a. every alert in the source renders, in the release namespace (the only one it writes), and every rendered Alert comes from the
        source or is a companion;
     b. RUNBOOK: every Alert's spec.runbook ends with a link to docs/runbooks/<name>.md at the chart's version, and that
        file exists with frontmatter type Runbook; the text holds no `{{` and no `@word` (HyperDX renders it as a
        Handlebars template and reads @word as a channel), the runbook is at most 3072 characters, and with the
        description at most 4096 (the CRD's rules);
     c. COMPANION: for every scrape job a non-companion Alert reads, a companion Alert aggregates `up` with
        service.name = that job, min below 1, and the collector config scrapes that job. HyperDX reads a window with no
        data as 0, so without one an "above" alert goes silent when its scrape dies;
     d. EVIDENCE: every rendered Metric's name comes from a source entry with evidence (kind cited or measured, a
        source URL at a tag -- never a branch), pinnedTo names a chart's images.tags key, and the URL is at THAT image's
        tag (so an image bump without re-citing fails);
     e. TYPE: a Metric's type is the source's; a metric the exporter declares as a counter but recorded as a gauge is
        converted by the collector (convert_sum_to_gauge on exactly its name);
     f. SELECTION: rendered for an install of keystone and mariadb alone, no alert needs an absent component, no
        companion or scrape job exists for rabbitmq, and openstack-exporter disables the absent services; with
        alerts.enabled false no Metric or Alert renders;
     g. PRE-RELEASE: an image tag named alpha, beta or rc runs only as an exception recorded in hack/app-versions.yaml, with
        a reason and an issue (openstack-exporter 2.0.0-alpha, #348).
  2. READERS (the chart, and mariadb with krateo.exporterUser, in values mode and with the umbrella's references):
     a. each credential Secret is referenced by exactly its readers' pods: the exporter user's openrc (which carries its
        clouds.yaml) by openstack-exporter, the .my.cnf by mysqld-exporter, the admin openrc and the exporter user's root by the ks-user Job, mariadb's
        mariadb-exporter-user by its Job; the collector references no Secret;
     b. with references, a sentinel password in values appears nowhere, and every ExternalSecret reads a root through
        the store;
     c. FIDELITY: each projection, its {{ .password }} replaced by a known password, equals the values-mode Secret
        rendered with that password;
     d. mariadb's StatefulSet renders identically with krateo.exporterUser on and off (switching it on must not
        restart the database);
     e. the umbrella lists identity-user-openstack-exporter for openstack-monitoring alone, and oslo-db-exporter for
        mariadb and openstack-monitoring alone.
  3. SWITCH (the umbrella), explicit opt-in (Diego, 2026-11-18):
     a. the default render (monitoring.enabled unset) equals null, false, and the renders with alert-provider's CRDs
        served (--api-versions) with the value unset or false, and names neither openstack-monitoring nor
        openstack-exporter anywhere -- no component, definition, root or reader-Role entry;
     b. monitoring.enabled true adds the openstack-monitoring definition, the identity-user-openstack-exporter root and
        its reader-Role entry;
     c. `enabled:` listing openstack-monitoring is refused, naming monitoring.enabled;
     e. PRECONDITION, its reads replaced by stand-ins: on a live cluster without alert-provider's CRDs, true is refused
        naming alert-provider and the missing CRD; with them served, or unset, or offline, it renders;
     d. SPAN: the lines of compositions.yaml from `$ov :=` to the end of the monitoring span, rendered for every
        component: unset (with or without the CRDs served) and false give the same specs; on, only mariadb (gains exactly
        krateo.exporterUser) and openstack-monitoring (gains the selection and the OTLP endpoint) change, and an explicit
        componentValues entry wins.
  4. MUTATIONS: each part above is run against a copy with one defect planted, and must fail. A gate that cannot see
     its defect is not a gate.

Fails when helm or PyYAML is missing, and when a part has nothing to check.
"""
import base64
import copy
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
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)

CHART = "openstack-monitoring"
NS = "openstack"
VERSION = "0.0.0-gate"
CAPS = ["observability.krateo.io/v1alpha1/Alert", "observability.krateo.io/v1alpha1/Metric"]
STORE = "openstack-credentials"
ROOTS = {
    "admin": "openstack-credential-identity-admin",
    "openstack_exporter": "openstack-credential-identity-user-openstack-exporter",
    "exporter": "openstack-credential-oslo-db-exporter",
}
# 2a: credential Secret -> the workloads that must (and alone may) reference it.
# The exporter user's openrc Secret also carries its clouds.yaml: openstack-exporter mounts that key alone. With values
# the ks-user Job reads the openrc keys; with the root it reads the root directly.
READERS_REFS = {
    "openstack-monitoring-keystone-user": {"Deployment/openstack-exporter"},
    "openstack-monitoring-mysqld-exporter": {"Deployment/openstack-mysqld-exporter"},
    "openstack-monitoring-keystone-admin": {"Job/openstack-monitoring-ks-user"},
    ROOTS["openstack_exporter"]: {"Job/openstack-monitoring-ks-user"},
}
READERS_VALUES = {
    "openstack-monitoring-keystone-user": {"Deployment/openstack-exporter", "Job/openstack-monitoring-ks-user"},
    "openstack-monitoring-mysqld-exporter": {"Deployment/openstack-mysqld-exporter"},
    "openstack-monitoring-keystone-admin": {"Job/openstack-monitoring-ks-user"},
}
# 2e: umbrella credential item -> the components that alone may list it.
LISTERS = {
    "identity-user-openstack-exporter": {"openstack-monitoring"},
    "oslo-db-exporter": {"mariadb", "openstack-monitoring"},
}


def P(root, *parts):
    return os.path.join(root, *parts)


def stage(root, chart, tmp, name):
    w = os.path.join(tmp, name)
    if os.path.exists(w):
        shutil.rmtree(w)
    shutil.copytree(P(root, "blueprints", chart, "chart"), w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", VERSION))
    if "helm-toolkit" in txt:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(P(root, "charts", "helm-toolkit"), os.path.join(w, "charts", "helm-toolkit"))
    return w


def helm_template(chart, release, values=None, caps=()):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    cmd = ["helm", "template", release, chart, "--namespace", NS, "--kube-version", "1.36.0", "-f", f.name]
    for c in caps:
        cmd += ["--api-versions", c]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return r.returncode, r.stdout, r.stderr


def docs_of(text):
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def by_kind(docs, kind):
    return [d for d in docs if d.get("kind") == kind]


def pod_spec(d):
    k = d.get("kind")
    if k in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
        return ((d.get("spec") or {}).get("template") or {}).get("spec") or {}
    if k == "CronJob":
        return ((((d.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return None


def secret_refs(spec):
    out = set()
    for v in spec.get("volumes") or []:
        if v.get("secret"):
            out.add(v["secret"].get("secretName"))
        for src in ((v.get("projected") or {}).get("sources") or []):
            if src.get("secret"):
                out.add(src["secret"].get("name"))
    for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        for e in c.get("env") or []:
            ref = ((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
            if ref:
                out.add(ref)
        for e in c.get("envFrom") or []:
            ref = (e.get("secretRef") or {}).get("name")
            if ref:
                out.add(ref)
    return out


def readers(docs):
    m = {}
    for d in docs:
        spec = pod_spec(d)
        if spec is None:
            continue
        who = "%s/%s" % (d["kind"], d["metadata"]["name"])
        for s in secret_refs(spec):
            m.setdefault(s, set()).add(who)
    return m


def secret_text(d):
    """Decoded data of a Secret, as {key: text}."""
    return {k: base64.b64decode(v).decode("utf-8", "replace") for k, v in (d.get("data") or {}).items()}


def refs_values(sentinels=None):
    s = sentinels or {}
    v = {"endpoints": {"identity": {"auth": {}}, "oslo_db": {"auth": {}}}}
    for user in ("admin", "openstack_exporter"):
        v["endpoints"]["identity"]["auth"][user] = {"passwordFrom": {"store": STORE, "secret": ROOTS[user], "key": "password"}}
        if user in s:
            v["endpoints"]["identity"]["auth"][user]["password"] = s[user]
    v["endpoints"]["oslo_db"]["auth"]["exporter"] = {"passwordFrom": {"store": STORE, "secret": ROOTS["exporter"], "key": "password"}}
    if "exporter" in s:
        v["endpoints"]["oslo_db"]["auth"]["exporter"]["password"] = s["exporter"]
    return v


def password_values(pw):
    return {"endpoints": {"identity": {"auth": {"admin": {"password": pw}, "openstack_exporter": {"password": pw}}},
                          "oslo_db": {"auth": {"exporter": {"password": pw}}}}}


def image_tag(root, chart, key):
    vals = yaml.safe_load(open(P(root, "blueprints", chart, "chart", "values.yaml"))) or {}
    ref = ((vals.get("images") or {}).get("tags") or {}).get(key)
    if not ref or ":" not in ref.rsplit("/", 1)[-1]:
        return None
    return ref.rsplit(":", 1)[1]


# ---------------------------------------------------------------------------------------------------------------- 1
def part_alerts(root, tmp, counts):
    fails = []
    src = yaml.safe_load(open(P(root, "blueprints", CHART, "chart", "files", "alerts.yaml"))) or {}
    vals = yaml.safe_load(open(P(root, "blueprints", CHART, "chart", "values.yaml"))) or {}
    base = ((vals.get("alerts") or {}).get("runbookBase") or "").rstrip("/")
    w = stage(root, CHART, tmp, "alerts")
    rc, out, err = helm_template(w, CHART + "-gate1234")
    if rc != 0:
        return ["1: %s does not render at defaults: %s" % (CHART, err.strip()[:300])]
    docs = docs_of(out)
    metrics = {d["metadata"]["name"]: d for d in by_kind(docs, "Metric")}
    alerts = {d["metadata"]["name"]: d for d in by_kind(docs, "Alert")}
    cms = [d for d in by_kind(docs, "ConfigMap") if d["metadata"]["name"] == "openstack-monitoring-collector"]
    if not cms:
        return ["1c: the chart renders no openstack-monitoring-collector ConfigMap: nothing scrapes anything"]
    collector = yaml.safe_load(cms[0]["data"]["collector.yaml"])
    scrape_jobs = {j["job_name"] for j in ((collector.get("receivers") or {}).get("prometheus") or {})
                   .get("config", {}).get("scrape_configs") or []}
    statements = (((collector.get("processors") or {}).get("transform/counter-to-gauge") or {}).get("metric_statements")
                  or [])
    pipeline = ((((collector.get("service") or {}).get("pipelines") or {}).get("metrics") or {}).get("processors") or [])
    if statements and "transform/counter-to-gauge" not in pipeline:
        fails.append("1e: the counter-to-gauge transform is configured but not in the metrics pipeline: it converts nothing")
    src_alerts = {a["name"]: a for a in src.get("alerts") or []}
    src_metrics = {m["name"]: m for m in src.get("metrics") or []}
    scrape_up = src.get("scrapeUp") or {}
    if not src_alerts:
        fails.append("1a: files/alerts.yaml defines no alert; the check had no input")
    # a. every source alert renders, where it should; nothing else renders
    for n in src_alerts:
        if n not in alerts:
            fails.append("1a: source alert %s does not render at defaults" % n)
    companions = {n: a for n, a in alerts.items()
                  if (a["metadata"].get("labels") or {}).get("openstack.krateo.io/companion") == "true"}
    for n, a in alerts.items():
        if n not in src_alerts and n not in companions:
            fails.append("1a: Alert %s is rendered but defined nowhere in files/alerts.yaml" % n)
        if a["metadata"].get("namespace") != NS:
            fails.append("1a: Alert %s is in namespace %r, not the release namespace %s: the release writes nowhere else"
                         % (n, a["metadata"].get("namespace"), NS))
    for n, m in metrics.items():
        if m["metadata"].get("namespace") != NS:
            fails.append("1a: Metric %s is not in the release namespace %s, where its Alerts' metricRef resolves"
                         % (n, NS))
    counts["1a"] = len(alerts)
    # b. runbooks
    link = re.compile(r"Runbook: (\S+)$")
    for n, a in alerts.items():
        spec = a.get("spec") or {}
        rb = spec.get("runbook") or ""
        desc = spec.get("description") or ""
        m = link.search(rb.strip())
        want_prefix = "%s/%s/docs/runbooks/" % (base, VERSION)
        if not m or not m.group(1).startswith(want_prefix) or not m.group(1).endswith(".md"):
            fails.append("1b: Alert %s's runbook does not end with a link to %s<name>.md" % (n, want_prefix))
        else:
            f = P(root, "docs", "runbooks", m.group(1)[len(want_prefix):])
            if not os.path.isfile(f):
                fails.append("1b: Alert %s links %s, which does not exist" % (n, os.path.relpath(f, root)))
            elif not re.search(r"^type:\s*Runbook\s*$", open(f).read().split("\n---", 1)[0], re.M):
                fails.append("1b: %s has no frontmatter type Runbook" % os.path.relpath(f, root))
        if "{{" in rb or "{{" in desc or re.search(r"(^|\s)@\w", rb + " " + desc):
            fails.append("1b: Alert %s's runbook or description holds `{{` or an @word, which HyperDX's Handlebars "
                         "template renders or reads as a channel" % n)
        if len(rb) > 3072 or (rb and len(desc) + 2 + len(rb) > 4096):
            fails.append("1b: Alert %s's runbook (%d) or runbook+description exceeds HyperDX's limits" % (n, len(rb)))
    counts["1b"] = len(alerts)
    # c. companions
    up_metrics = {n for n, m in metrics.items() if (m.get("spec") or {}).get("metricName") == "up"}
    covered = {}
    for n, a in companions.items():
        spec = a.get("spec") or {}
        job = (spec.get("labels") or {}).get("service.name")
        if ((spec.get("metricRef") or {}).get("name") in up_metrics and job and spec.get("aggregation") == "min"
                and spec.get("thresholdType") == "below" and spec.get("threshold") == 1):
            covered[job] = n
        else:
            fails.append("1c: companion %s is not `up` (service.name = its job) aggregated min below 1" % n)
    jobs_read = set()
    for n, a in alerts.items():
        if n in companions:
            continue
        ref = ((a.get("spec") or {}).get("metricRef") or {}).get("name")
        sm = src_metrics.get(ref)
        if not sm:
            fails.append("1c: Alert %s reads Metric %s, which files/alerts.yaml does not define" % (n, ref))
            continue
        jobs_read.add(sm["job"])
        if sm["job"] not in covered:
            fails.append("1c: Alert %s reads metric %s of scrape job %s, and no companion Alert fires when that job's "
                         "scrape stops: HyperDX reads no data as 0" % (n, sm["metricName"], sm["job"]))
    for job in jobs_read | set(covered):
        if job not in scrape_jobs:
            fails.append("1c: scrape job %s is read by an Alert but the collector config does not scrape it" % job)
    counts["1c"] = len(jobs_read)
    # d. evidence
    for n, m in metrics.items():
        mn = (m.get("spec") or {}).get("metricName")
        entry = scrape_up if mn == "up" and n == scrape_up.get("name") else next(
            (e for e in src_metrics.values() if e.get("name") == n), None)
        if not entry:
            fails.append("1d: Metric %s (%s) has no entry in files/alerts.yaml" % (n, mn))
            continue
        if entry.get("metricName") != mn:
            fails.append("1d: Metric %s renders %s, the source says %s" % (n, mn, entry.get("metricName")))
        ev = entry.get("evidence") or {}
        source = " ".join(str(ev.get("source") or "").split())
        if ev.get("kind") not in ("cited", "measured") or not source.startswith("https://"):
            fails.append("1d: metric %s (%s) has no evidence (kind cited or measured, and a source URL): a name nobody "
                         "measured or cited is not shipped" % (n, mn))
            continue
        url = source.split()[0]
        if re.search(r"/(blob|tree)/(main|master|HEAD)/", url) or "/blob/" not in url:
            fails.append("1d: metric %s cites %s, which is not at a tag: a branch moves under the citation" % (n, url))
        pin = str(ev.get("pinnedTo") or "")
        if ":" not in pin:
            fails.append("1d: metric %s's evidence names no pinnedTo <chart>:<images.tags key>" % n)
            continue
        chart, key = pin.split(":", 1)
        tag = image_tag(root, chart, key)
        if not tag:
            fails.append("1d: metric %s is pinned to %s, which names no image tag" % (n, pin))
            continue
        bare = tag[1:] if tag.startswith("v") else tag
        if not re.search(r"/blob/v?%s/" % re.escape(bare), url):
            fails.append("1d: metric %s cites %s, but %s runs %s: re-cite the metric at the tag that runs" % (n, url, pin, tag))
    counts["1d"] = len(metrics)
    # e. types
    for n, m in metrics.items():
        entry = src_metrics.get(n) or (scrape_up if n == scrape_up.get("name") else None)
        if not entry:
            continue
        if (m.get("spec") or {}).get("type") != entry.get("type"):
            fails.append("1e: Metric %s records type %s, the source says %s" % (n, (m.get("spec") or {}).get("type"),
                                                                                 entry.get("type")))
        if entry.get("upstreamType") == "counter" and entry.get("type") == "gauge":
            want = 'convert_sum_to_gauge() where metric.name == "%s"' % entry["metricName"]
            if want not in statements:
                fails.append("1e: %s is a counter upstream and a gauge here, and the collector does not convert it "
                             "(%s): HyperDX would read a cumulative sum" % (entry["metricName"], want))
    counts["1e"] = len(metrics)
    # f. selection
    rc, out2, err = helm_template(w, CHART + "-gate1234", {"openstack": {"components": ["keystone", "mariadb"]}})
    if rc != 0:
        fails.append("1f: the chart does not render for keystone and mariadb alone: %s" % err.strip()[:200])
    else:
        d2 = docs_of(out2)
        cm = [d for d in by_kind(d2, "ConfigMap") if d["metadata"]["name"] == "openstack-monitoring-collector"]
        jobs2 = {j["job_name"] for j in (yaml.safe_load(cm[0]["data"]["collector.yaml"])["receivers"]["prometheus"]
                                         ["config"]["scrape_configs"] or [])} if cm else set()
        if "openstack-rabbitmq" in jobs2:
            fails.append("1f: the collector scrapes rabbitmq without rabbitmq selected")
        for a in by_kind(d2, "Alert"):
            n = a["metadata"]["name"]
            spec = a.get("spec") or {}
            if (a["metadata"].get("labels") or {}).get("openstack.krateo.io/companion") == "true":
                job = (spec.get("labels") or {}).get("service.name")
            else:
                job = (src_metrics.get((spec.get("metricRef") or {}).get("name")) or {}).get("job")
            if job not in jobs2:
                fails.append("1f: Alert %s renders for keystone and mariadb alone, but its scrape job %s is not scraped "
                             "there: its metric never arrives, reads 0, and the alert fires forever" % (n, job))
        dep = [d for d in by_kind(d2, "Deployment") if d["metadata"]["name"] == "openstack-exporter"]
        args = dep[0]["spec"]["template"]["spec"]["containers"][0].get("args") if dep else []
        for svc in ("compute", "network", "volume", "image", "placement"):
            if "--disable-service.%s" % svc not in (args or []):
                fails.append("1f: openstack-exporter scrapes %s although its component is not selected" % svc)
        if "--disable-service.identity" in (args or []):
            fails.append("1f: openstack-exporter disables identity although keystone is selected")
    # g. a pre-release-named image (alpha, beta, rc) runs only as a recorded exception: a reason and an issue in
    #    hack/app-versions.yaml, on the entry of this chart whose image it is.
    av = (yaml.safe_load(open(P(root, "hack", "app-versions.yaml"))) or {}).get("charts") or {}
    entry = av.get("blueprints/%s/chart" % CHART) or {}
    pre = 0
    for key, ref in ((vals.get("images") or {}).get("tags") or {}).items():
        tag = ref.rsplit(":", 1)[1] if ":" in ref.rsplit("/", 1)[-1] else ""
        if re.search(r"(^|[-.])(alpha|beta|rc)[0-9.]*$", tag, re.I):
            pre += 1
            exc = entry.get("exception") or {}
            if entry.get("image") != key or not str(exc.get("reason") or "").strip() or not str(exc.get("issue") or "").strip():
                fails.append("1g: images.tags.%s is the pre-release-named %s, and hack/app-versions.yaml records no exception "
                             "for it with a reason and an issue" % (key, ref))
    counts["1g"] = pre
    rc, out3, err = helm_template(w, CHART + "-gate1234", {"alerts": {"enabled": False}})
    if rc != 0 or by_kind(docs_of(out3), "Alert") or by_kind(docs_of(out3), "Metric"):
        fails.append("1f: with alerts.enabled false the chart still renders Alert or Metric CRs (or fails to render)")
    counts["1f"] = 1
    return fails


# ---------------------------------------------------------------------------------------------------------------- 2
def part_readers(root, tmp, counts):
    fails = []
    w = stage(root, CHART, tmp, "readers")
    sent = {"admin": "gate-sentinel-admin-6c1d", "openstack_exporter": "gate-sentinel-osexp-6c1d",
            "exporter": "gate-sentinel-dbexp-6c1d"}
    modes = {"values": ({}, READERS_VALUES), "refs": (refs_values(sent), READERS_REFS)}
    rendered = {}
    for mode, (vals, want) in modes.items():
        rc, out, err = helm_template(w, CHART + "-gate1234", vals)
        if rc != 0:
            fails.append("2a: %s does not render in %s mode: %s" % (CHART, mode, err.strip()[:200]))
            continue
        docs = docs_of(out)
        rendered[mode] = docs
        got = readers(docs)
        for secret, who in want.items():
            if got.get(secret, set()) != who:
                fails.append("2a: %s mode: %s is read by %s, want exactly %s" % (mode, secret, sorted(got.get(secret, set())),
                                                                                 sorted(who)))
        for secret, who in got.items():
            if secret not in want:
                fails.append("2a: %s mode: %s references Secret %s, which is no credential of theirs" % (mode, sorted(who), secret))
        coll = [d for d in by_kind(docs, "Deployment") if d["metadata"]["name"] == "openstack-monitoring-collector"]
        if not coll or secret_refs(pod_spec(coll[0])):
            fails.append("2a: %s mode: the collector is missing or references a Secret; it holds no credential" % mode)
        counts["2a"] = counts.get("2a", 0) + len(want)
    if "refs" in rendered:
        text = yaml.safe_dump_all(rendered["refs"])
        decoded = text + "".join("\n".join(secret_text(d).values()) for d in by_kind(rendered["refs"], "Secret"))
        for user, s in sent.items():
            if s in decoded:
                fails.append("2b: with references, %s's password from values still appears in the render" % user)
        for es in by_kind(rendered["refs"], "ExternalSecret"):
            spec = es.get("spec") or {}
            if (spec.get("secretStoreRef") or {}).get("name") != STORE or any(
                    ((d.get("remoteRef") or {}).get("key") not in ROOTS.values()) for d in spec.get("data") or []):
                fails.append("2b: ExternalSecret %s reads something other than a root through %s"
                             % (es["metadata"]["name"], STORE))
        counts["2b"] = len(sent)
    # c. fidelity
    pw = "GateFidelity42"
    rc, outv, err = helm_template(w, CHART + "-gate1234", password_values(pw))
    if rc == 0 and "refs" in rendered:
        vsec = {d["metadata"]["name"]: secret_text(d) for d in by_kind(docs_of(outv), "Secret")}
        n = 0
        for es in by_kind(rendered["refs"], "ExternalSecret"):
            name = es["metadata"]["name"]
            tmpl = (((es.get("spec") or {}).get("target") or {}).get("template") or {}).get("data") or {}
            got = {k: v.replace("{{ .password }}", pw) for k, v in tmpl.items()}
            if name not in vsec:
                fails.append("2c: projection %s has no values-mode Secret of the same name to match" % name)
            elif got != vsec[name]:
                fails.append("2c: projection %s does not hold what the values-mode Secret holds with the same password"
                             % name)
            n += 1
        counts["2c"] = n
        if not n:
            fails.append("2c: no projection rendered; the check had no input")
    else:
        fails.append("2c: %s does not render with passwords in values: %s" % (CHART, err.strip()[:200]))
    # d. mariadb: the exporter user's Secret is read by its Job alone, and the StatefulSet does not move
    m = stage(root, "mariadb", tmp, "readers-mariadb")
    mref = {"endpoints": {"oslo_db": {"auth": {"exporter": {"passwordFrom": {
        "store": STORE, "secret": ROOTS["exporter"], "key": "password"}}}}}}
    for mode, base_vals in (("values", {}), ("refs", mref)):
        on = copy.deepcopy(base_vals)
        on.setdefault("krateo", {})["exporterUser"] = True
        r0 = helm_template(m, "mariadb", base_vals)
        r1 = helm_template(m, "mariadb", on)
        if r0[0] != 0 or r1[0] != 0:
            fails.append("2d: mariadb does not render (%s mode, exporterUser off/on): %s" % (mode, (r0[2] or r1[2]).strip()[:200]))
            continue
        d0, d1 = docs_of(r0[1]), docs_of(r1[1])
        s0 = [d for d in by_kind(d0, "StatefulSet")]
        s1 = [d for d in by_kind(d1, "StatefulSet")]
        if not s0 or s0 != s1:
            fails.append("2d: %s mode: mariadb's StatefulSet renders differently with krateo.exporterUser on: it would "
                         "restart the database" % mode)
        got = readers(d1)
        if got.get("mariadb-exporter-user") != {"Job/mariadb-exporter-user"}:
            fails.append("2d: %s mode: mariadb-exporter-user is read by %s, want exactly the mariadb-exporter-user Job"
                         % (mode, sorted(got.get("mariadb-exporter-user", set()))))
        if "mariadb-exporter-user" in readers(d0) or any(d["metadata"]["name"] == "mariadb-exporter-user" for d in d0):
            fails.append("2d: %s mode: mariadb renders the exporter user's objects with krateo.exporterUser off" % mode)
        jobs = [d for d in by_kind(d1, "Job") if d["metadata"]["name"] == "mariadb-exporter-user"]
        cmd = " ".join(jobs[0]["spec"]["template"]["spec"]["containers"][0].get("command") or []) if jobs else ""
        if "@'%'" not in cmd:
            fails.append("2d: the exporter user is not created for any host ('%'): a standalone exporter cannot log in")
        if re.search(r"GRANT[^;]*\bSELECT\b", cmd, re.I):
            fails.append("2d: the exporter user is granted SELECT, which reads every OpenStack database")
        counts["2d"] = counts.get("2d", 0) + 1
    # e. the umbrella's listings
    uv = yaml.safe_load(open(P(root, "blueprints", "openstack", "chart", "values.yaml"))) or {}
    for item, want in LISTERS.items():
        got = {c["name"] for c in uv.get("components") or [] if item in (c.get("credentials") or [])}
        if got != want:
            fails.append("2e: the umbrella lists %s for %s, want exactly %s" % (item, sorted(got), sorted(want)))
    counts["2e"] = len(LISTERS)
    return fails


# ---------------------------------------------------------------------------------------------------------------- 3
PRECOND = "monitoring-precondition.yaml"
LIVE_LOOKUP = 'lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "compositiondefinitions.core.krateo.io"'
CRD_LOOKUP = 'lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" $crd'


def stub_precondition(chart_dir, live, served):
    """Replace the precondition's two reads with stand-ins: a live cluster (core-provider's CRD found) or not, and
    alert-provider's CRDs served or not. Returns the number of reads replaced (2, or the template changed)."""
    p = os.path.join(chart_dir, "templates", PRECOND)
    t = open(p).read()
    n = t.count(LIVE_LOOKUP) + t.count(CRD_LOOKUP)
    t = t.replace(LIVE_LOOKUP, '(dict "kind" "CustomResourceDefinition")' if live else "(dict)")
    t = t.replace(CRD_LOOKUP, '(dict "kind" "CustomResourceDefinition")' if served else "(dict)")
    open(p, "w").write(t)
    return n


def part_switch(root, tmp, counts):
    fails = []
    u = stage(root, "openstack", tmp, "switch")
    cases = {
        "default": ({}, ()),
        "false": ({"monitoring": {"enabled": False}}, ()),
        "null": ({"monitoring": {"enabled": None}}, ()),
        "caps": ({}, tuple(CAPS)),
        "caps-false": ({"monitoring": {"enabled": False}}, tuple(CAPS)),
        "true": ({"monitoring": {"enabled": True}}, ()),
    }
    out = {}
    for k, (vals, caps) in cases.items():
        rc, o, err = helm_template(u, "openstack", vals, caps)
        if rc != 0:
            fails.append("3: the umbrella does not render (%s): %s" % (k, err.strip()[:200]))
        else:
            out[k] = o
    if len(out) == len(cases):
        # a. explicit opt-in: unset, null and false are one render whatever the cluster serves
        for k in ("false", "null", "caps", "caps-false"):
            if out[k] != out["default"]:
                fails.append("3a: the %s render differs from the default: only monitoring.enabled true may turn "
                             "monitoring on (explicit opt-in)" % k)
        for word in ("openstack-monitoring", "openstack-exporter"):
            if word in out["default"]:
                fails.append("3a: the default render names %s: off must add nothing (byte-identical to a release "
                             "without monitoring)" % word)
        on = docs_of(out["true"])
        if not [d for d in by_kind(on, "CompositionDefinition") if d["metadata"]["name"] == CHART]:
            fails.append("3b: monitoring on registers no openstack-monitoring CompositionDefinition")
        if not [d for d in by_kind(on, "ExternalSecret") if d["metadata"]["name"] == ROOTS["openstack_exporter"]]:
            fails.append("3b: monitoring on renders no %s root" % ROOTS["openstack_exporter"])
        roles = [d for d in by_kind(on, "Role") if ROOTS["openstack_exporter"] in json.dumps(d)]
        if not roles:
            fails.append("3b: monitoring on does not let the store read the %s root" % ROOTS["openstack_exporter"])
        counts["3ab"] = len(cases)
    rc, o, err = helm_template(u, "openstack", {"enabled": [CHART]})
    if rc == 0 or "monitoring.enabled" not in err:
        fails.append("3c: `enabled:` listing openstack-monitoring is not refused naming monitoring.enabled")
    counts["3c"] = 1
    # e. the precondition: on a live cluster without alert-provider's CRDs, true is refused naming alert-provider
    pre = {}
    for k, live, served, vals in (("live-absent-true", True, False, {"monitoring": {"enabled": True}}),
                                  ("live-served-true", True, True, {"monitoring": {"enabled": True}}),
                                  ("live-absent-unset", True, False, {}),
                                  ("offline-true", False, False, {"monitoring": {"enabled": True}})):
        w = stage(root, "openstack", tmp, "pre-" + k)
        if stub_precondition(w, live, served) != 2:
            fails.append("3e: templates/%s no longer reads exactly core-provider's CRD and each alert-provider CRD by "
                         "name; the stand-ins this check uses would test nothing" % PRECOND)
            break
        pre[k] = helm_template(w, "openstack", vals)
    if len(pre) == 4:
        rc, o, err = pre["live-absent-true"]
        if rc == 0 or "alert-provider" not in err or "alerts.observability.krateo.io" not in err:
            fails.append("3e: monitoring.enabled true on a live cluster without alert-provider's CRDs renders (or is "
                         "refused without naming alert-provider and the missing CRD): its Alert CRs would fail to apply")
        for k in ("live-served-true", "live-absent-unset", "offline-true"):
            if pre[k][0] != 0:
                fails.append("3e: the %s render is refused: %s" % (k, pre[k][2].strip()[:200]))
        counts["3e"] = len(pre)
    # d. the span
    comp = open(P(root, "blueprints", "openstack", "chart", "templates", "compositions.yaml")).read()
    start = comp.find("{{- $ov := ")
    end = comp.find("end of the monitoring span", start)
    end = comp.rfind("{{", 0, end) if end >= 0 else -1
    if start < 0 or end < 0:
        return fails + ["3d: compositions.yaml has no span from `{{- $ov := ` to the end of the monitoring span"]
    open(os.path.join(u, "templates", "zz-gate-monitoring-probe.yaml"), "w").write(
        '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
        '{{- range $c := .Values.components }}\n' + comp[start:end] + '\n'
        '# MONSPEC {{ $c.name }} {{ $ov | toJson }}\n{{- end }}\n'
        '# MONSEL {{ $sel | toJson }}\n')
    specs, sels = {}, {}
    probe = {"off": ({"monitoring": {"enabled": False}}, ()), "default": ({}, ()), "caps": ({}, tuple(CAPS)),
             "on": ({"monitoring": {"enabled": True}}, ()),
             "override": ({"monitoring": {"enabled": True},
                           "componentValues": {CHART: {"collector": {"otlpEndpoint": "http://gate-elsewhere:4318"}}}}, ())}
    for k, (vals, caps) in probe.items():
        rc, o, err = helm_template(u, "openstack", vals, caps)
        if rc != 0:
            fails.append("3d: the span probe does not render (%s): %s" % (k, err.strip()[:200]))
            continue
        specs[k] = {l.split(" ", 3)[2]: json.loads(l.split(" ", 3)[3]) for l in o.splitlines() if l.startswith("# MONSPEC ")}
        sels[k] = next((json.loads(l[len("# MONSEL "):]) for l in o.splitlines() if l.startswith("# MONSEL ")), [])
    if len(specs) == len(probe):
        names = list(specs["off"])
        if not names:
            fails.append("3d: the probe rendered no component spec; the check had no input")
        for n in names:
            for k in ("default", "caps"):
                if specs[k].get(n) != specs["off"].get(n):
                    fails.append("3d: monitoring %s changes %s's spec" % ("unset" if k == "default" else
                                                                         "unset with the CRDs served", n))
            a, b = specs["off"].get(n), specs["on"].get(n)
            if n == "mariadb":
                if "exporterUser" in ((a or {}).get("krateo") or {}):
                    fails.append("3d: with monitoring off, mariadb's spec carries krateo.exporterUser: off must not move it")
                want = copy.deepcopy(a)
                want.setdefault("krateo", {})["exporterUser"] = True
                if b != want:
                    fails.append("3d: with monitoring on, mariadb's spec is not its spec plus krateo.exporterUser")
            elif n == CHART:
                ok = (b.get("openstack", {}).get("components") == sels["on"]
                      and b.get("collector", {}).get("otlpEndpoint", "").startswith("http")
                      and "alerts" not in b
                      and specs["override"].get(n, {}).get("collector", {}).get("otlpEndpoint") == "http://gate-elsewhere:4318")
                if not ok:
                    fails.append("3d: with monitoring on, openstack-monitoring's spec does not carry the selection and "
                                 "the OTLP endpoint (and nothing about alerts), or an explicit componentValues entry "
                                 "does not win")
                if any(k in (a or {}) for k in ("openstack", "collector", "alerts")):
                    fails.append("3d: with monitoring off, openstack-monitoring's spec still carries monitoring values")
            elif n == "keystone":
                # keystone deregisters the components outside the selection (#444): with monitoring off,
                # openstack-monitoring is one, with no Composition or Service in this offline render, so it is handed
                # over in krateo.deregister.components; on, it is selected and leaves that list. Nothing else moves
                # (it registers no catalog service, so krateo.deregister.services is the same either way).
                comps = (((a or {}).get("krateo") or {}).get("deregister") or {}).get("components") or []
                if CHART not in comps:
                    fails.append("3d: with monitoring off, keystone's krateo.deregister.components does not list %s, "
                                 "which is outside the selection and gone" % CHART)
                want = copy.deepcopy(a)
                if CHART in comps:
                    want["krateo"]["deregister"]["components"] = [c for c in comps if c != CHART]
                if b != want:
                    fails.append("3d: with monitoring on, keystone's spec is not its spec with %s out of "
                                 "krateo.deregister.components; only that may move" % CHART)
            elif a != b:
                fails.append("3d: monitoring on changes %s's spec; only mariadb, keystone's krateo.deregister and "
                             "openstack-monitoring may move" % n)
        if CHART not in sels["on"] or CHART in sels["off"] or CHART in sels["default"] or CHART in sels["caps"]:
            fails.append("3d: the selection does not follow the monitoring value alone")
        counts["3d"] = len(names)
    return fails


# ---------------------------------------------------------------------------------------------------------------- 4
def sub(path, old, new, count=1):
    s = open(path).read()
    if old not in s:
        raise RuntimeError("mutation anchor not found in %s: %r" % (path, old[:80]))
    open(path, "w").write(s.replace(old, new, count))


def mutations(r):
    """(name, part, mutate) -- each must make its part fail."""
    osm = P(r, "blueprints", CHART, "chart")
    umb = P(r, "blueprints", "openstack", "chart")
    return [
        ("an alert links a runbook that does not exist", part_alerts,
         lambda: sub(P(osm, "files", "alerts.yaml"), "runbook: openstack-mariadb-down", "runbook: openstack-no-such-runbook")),
        ("the companions are not rendered", part_alerts,
         lambda: sub(P(osm, "templates", "observability.yaml"), "{{- range $jn := $usedJobs }}",
                     "{{- range $jn := list }}")),
        ("a metric loses its evidence", part_alerts,
         lambda: sub(P(osm, "files", "alerts.yaml"), "      kind: cited\n      pinnedTo: openstack-monitoring:mysqld_exporter",
                     "      kind: guessed\n      pinnedTo: openstack-monitoring:mysqld_exporter")),
        ("a metric is cited at a branch", part_alerts,
         lambda: sub(P(osm, "files", "alerts.yaml"), "rabbitmq-server/blob/v3.10.18/", "rabbitmq-server/blob/main/")),
        ("the alpha-tag exception loses its issue", part_alerts,
         lambda: sub(P(r, "hack", "app-versions.yaml"), '      issue: "348"\n', "")),
        ("the exporter image moves without re-citing", part_alerts,
         lambda: sub(P(osm, "values.yaml"), "openstack-exporter:2.0.0-alpha", "openstack-exporter:2.0.1")),
        ("a counter is recorded as a gauge without conversion", part_alerts,
         lambda: (sub(P(osm, "files", "alerts.yaml"), "    metricName: openstack_nova_agent_state\n    type: gauge\n",
                      "    metricName: openstack_nova_agent_state\n    type: gauge\n    upstreamType: counter\n"),
                  sub(P(osm, "templates", "configmap-collector.yaml"), "convert_sum_to_gauge()", "convert_gauge_to_sum()"))),
        ("an alert renders without its component", part_alerts,
         lambda: sub(P(osm, "files", "alerts.yaml"), "    requires: rabbitmq\n    displayName", "    requires: mariadb\n    displayName")),
        ("the collector mounts the exporter's clouds.yaml", part_readers,
         lambda: sub(P(osm, "templates", "deployment-collector.yaml"),
                     "        - name: config\n          configMap:",
                     "        - name: stolen\n          secret:\n            secretName: openstack-monitoring-keystone-user\n"
                     "        - name: config\n          configMap:")),
        ("keystone lists the exporter's credential", part_readers,
         lambda: sub(P(umb, "values.yaml"), "      - identity-user-keystone-test\n",
                     "      - identity-user-keystone-test\n      - identity-user-openstack-exporter\n")),
        ("the exporter user's Secret lands in the StatefulSet's hashed file", part_readers,
         lambda: sub(P(r, "blueprints", "mariadb", "chart", "templates", "statefulset.yaml"),
                     '        secrets-etc-exporter-hash: {{ tuple "exporter-secrets-etc.yaml" . | include "helm-toolkit.utils.hash" }}',
                     '        secrets-etc-exporter-hash: {{ tuple "exporter-secrets-etc.yaml" . | include "helm-toolkit.utils.hash" }}\n'
                     '        exporter-user-hash: {{ tuple "secret-exporter-user.yaml" . | include "helm-toolkit.utils.hash" }}')),
        ("the root renders with monitoring off", part_switch,
         lambda: sub(P(umb, "templates", "credentials.yaml"),
                     "{{- if and (not $i.source) (or (not $i.monitoring) $monitoring) }}", "{{- if not $i.source }}")),
        ("mariadb gets exporterUser with monitoring off", part_switch,
         lambda: sub(P(umb, "templates", "compositions.yaml"),
                     '{{- if include "osh.monitoringOn" (list $) }}\n{{- if eq $c.name "mariadb" }}',
                     '{{- if true }}\n{{- if eq $c.name "mariadb" }}')),
        ("unset turns monitoring on", part_switch,
         lambda: sub(P(umb, "templates", "_helpers.tpl"), "{{- if and (kindIs \"bool\" $m.enabled) $m.enabled }}true{{ end -}}",
                     "{{- if or (not (kindIs \"bool\" $m.enabled)) $m.enabled }}true{{ end -}}")),
        ("enabled true renders where alert-provider's CRDs are missing", part_switch,
         lambda: sub(P(umb, "templates", "monitoring-precondition.yaml"), "{{- if $absent }}", "{{- if and false $absent }}")),
        ("the enabled: refusal is dropped", part_switch,
         lambda: sub(P(umb, "templates", "_helpers.tpl"), "    {{- if and $c.monitoring (has $c.name $enabled) -}}",
                     "    {{- if and false $c.monitoring (has $c.name $enabled) -}}")),
    ]


def copy_tree(dst):
    for rel in (os.path.join("blueprints", CHART), os.path.join("blueprints", "openstack"),
                os.path.join("blueprints", "mariadb"), os.path.join("blueprints", "rabbitmq"),
                os.path.join("charts", "helm-toolkit"), os.path.join("docs", "runbooks")):
        shutil.copytree(P(ROOT, rel), P(dst, rel))
    os.makedirs(P(dst, "hack"), exist_ok=True)
    shutil.copy(P(ROOT, "hack", "app-versions.yaml"), P(dst, "hack", "app-versions.yaml"))


def main():
    failures, counts = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        for part in (part_alerts, part_readers, part_switch):
            failures += part(ROOT, tmp, counts)
        caught = 0
        ms = mutations("@")
        for i, (name, part, _) in enumerate(ms):
            mroot = os.path.join(tmp, "m%d" % i)
            copy_tree(mroot)
            mutate = mutations(mroot)[i][2]
            try:
                mutate()
            except RuntimeError as e:
                failures.append("4: mutation %r could not be planted: %s" % (name, e))
                continue
            got = part(mroot, os.path.join(mroot, "work"), {})
            if got:
                caught += 1
            else:
                failures.append("4: mutation %r passed %s: the part cannot see its own defect" % (name, part.__name__))
            shutil.rmtree(mroot, ignore_errors=True)
        counts["4"] = caught
    for k in ("1a", "1b", "1c", "1d", "1e", "1g", "2a", "2c", "2d", "3ab", "3d", "3e", "4"):
        if not counts.get(k):
            failures.append("%s: nothing was checked" % k)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: openstack-monitoring: %d Alert(s) rendered, each with a runbook; %d scrape job(s), each with a companion; "
          "%d Metric(s) cited at their image's tag; %d credential reader set(s) exact, %d projection(s) faithful, mariadb's "
          "StatefulSet unmoved in %d mode(s); %d umbrella render(s) of the switch agree, %d component spec(s) probed; "
          "%d of %d mutation(s) caught"
          % (counts["1a"], counts["1c"], counts["1d"], counts["2a"], counts["2c"], counts["2d"], counts["3ab"],
             counts["3d"], counts["4"], len(mutations("@"))))


main()
