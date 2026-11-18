#!/usr/bin/env python3
"""Every publisher in ceilometer's rendered pipelines has a consumer in ceilometer's dependency closure (issue #294).

    hack/validate-ceilometer-publishers.py [repo-root]

Up to 0.3.72 both of ceilometer's sinks listed notifier:// next to gnocchi://. The notifier publisher sends every sample
and event back onto RabbitMQ (ceilometer/publisher/messaging.py, stable/2026.1: oslo_messaging.Notifier(...).sample() on
topic [publisher_notifier] metering_topic / event_topic, `metering` / `event`, on ceilometer's notification transport),
which lands in the queues metering.sample and event.sample. Nothing in this blueprint listens on those topics (their
consumers, the collector and panko, are retired), so on GKE (2026-11-17) both queues had 0 consumers and only grew:
event.sample 954 -> 1062 messages in 10 minutes.

ceilometer is rendered at its defaults, and each publisher URL of pipeline.yaml and event_pipeline.yaml must reach a
consumer that a selected ceilometer can count on, which is one in its transitive `deps` closure in the umbrella's
components (or ceilometer itself):
  - gnocchi://       gnocchi's API (ceilometer/publisher/gnocchi.py, an HTTP client of the metric service).
  - notifier://      the queue <topic>.sample on the vhost of the URL's own host, or of ceilometer's notification
                     transport ([oslo_messaging_notifications] transport_url, else [DEFAULT] transport_url;
                     ceilometer/messaging.py get_transport -> get_notification_transport). A consumer is a listener
                     program below whose component is in the closure, on that vhost, listening on that topic.
  - any other scheme fails: add its consumer here, from a measurement, first.
Listener programs (stable/2026.1 source):
  - ceilometer-agent-notification: topics [oslo_messaging_notifications] topics, on each [notification] messaging_urls
    vhost, or its own notification transport when that is empty (ceilometer/notification.py get_targets, run).
  - aodh-listener: topic [listener] event_alarm_topic (default alarm.all), on aodh's notification transport (aodh's
    vhost). aodh is not in ceilometer's closure, so it is not counted today; it is listed so the rule is visible.
A topic that only ceilometer-agent-notification consumes fails too: ceilometer would re-ingest its own samples as
notifications (its SampleEndpoint implements `sample`, so it binds <topic>.sample), a loop rather than a consumer.

Fails when helm or PyYAML is missing, when the render fails, and when no publisher was read (no input is not a pass).
"""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
CHART = os.path.join(ROOT, "blueprints", "ceilometer", "chart")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
UMBRELLA = os.path.join(ROOT, "blueprints", "openstack", "chart", "values.yaml")
PIPELINES = {"pipeline.yaml": "metering_topic", "event_pipeline.yaml": "event_topic"}
TOPIC_DEFAULTS = {"metering_topic": "metering", "event_topic": "event"}
# The HTTP publishers whose consumer is a component's API.
API_CONSUMERS = {"gnocchi": "gnocchi"}
if shutil.which("helm") is None:
    print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
    sys.exit(2)
failures = []


def ini(text):
    """section -> option -> [values]; a MultiStrOpt is rendered as repeated keys."""
    out, sec = {}, None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ";")):
            continue
        if s.startswith("[") and s.endswith("]"):
            sec = s[1:-1]
            out.setdefault(sec, {})
        elif "=" in s and sec is not None:
            k, v = s.split("=", 1)
            out[sec].setdefault(k.strip(), []).append(v.strip())
    return out


def merged_conf(docs):
    """ceilometer.conf with every ceilometer drop-in (*.conf in a ceilometer-* Secret) laid over it."""
    conf, seen = {}, 0
    for d in docs:
        if d.get("kind") != "Secret" or not d["metadata"]["name"].startswith("ceilometer"):
            continue
        for k, v in sorted((d.get("data") or {}).items()):
            if not k.endswith(".conf") or k in ("logging.conf",):
                continue
            seen += 1
            for sec, opts in ini(base64.b64decode(v).decode()).items():
                conf.setdefault(sec, {}).update(opts)
    return conf, seen


def vhost(url):
    return urlsplit(url).path.lstrip("/") or "/"


def closure(components, name):
    deps = {c["name"]: list(c.get("deps") or []) for c in components}
    out, todo = set(), [name]
    while todo:
        n = todo.pop()
        if n in out:
            continue
        out.add(n)
        todo.extend(deps.get(n, []))
    return out


def listeners(conf, in_closure):
    """(component, program, vhost, topic) for every notification listener a selected ceilometer can count on."""
    rows = []
    if "ceilometer" in in_closure:
        topics = []
        for v in (conf.get("oslo_messaging_notifications") or {}).get("topics") or ["notifications"]:
            topics.extend(t.strip() for t in v.split(",") if t.strip())
        urls = (conf.get("notification") or {}).get("messaging_urls") or []
        if not urls:
            urls = [notification_transport(conf)]
        for u in urls:
            for t in topics:
                rows.append(("ceilometer", "ceilometer-agent-notification", vhost(u), t))
    # aodh-listener would be counted here, on aodh's vhost, once aodh is in ceilometer's closure; its config is aodh's,
    # so the gate would render aodh first.
    if "aodh" in in_closure:
        failures.append("aodh is now in ceilometer's closure: render aodh here and count aodh-listener's "
                        "[listener] event_alarm_topic on its vhost")
    return rows


def notification_transport(conf):
    n = (conf.get("oslo_messaging_notifications") or {}).get("transport_url")
    d = (conf.get("DEFAULT") or {}).get("transport_url")
    return (n or d or [""])[-1]


def main():
    components = (yaml.safe_load(open(UMBRELLA)) or {}).get("components") or []
    if not any(c.get("name") == "ceilometer" for c in components):
        failures.append("the umbrella lists no ceilometer component, so its closure cannot be read")
    in_closure = closure(components, "ceilometer")
    with tempfile.TemporaryDirectory() as tmp:
        w = os.path.join(tmp, "chart")
        shutil.copytree(CHART, w)
        cy = os.path.join(w, "Chart.yaml")
        txt = open(cy).read()
        open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-gate"))
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
        r = subprocess.run(["helm", "template", "ceilometer-gate1234", w, "--namespace", "openstack",
                            "--kube-version", "1.36.0"], capture_output=True, text=True)
    if r.returncode != 0:
        print("FAILED MEASUREMENT: ceilometer does not render: " + r.stderr.strip()[:300])
        sys.exit(2)
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    conf, nconf = merged_conf(docs)
    if nconf == 0:
        failures.append("no ceilometer .conf was rendered, so no transport or listener could be read")
    pub_topics = conf.get("publisher_notifier") or {}
    rows = listeners(conf, in_closure)
    checked = []
    for d in docs:
        if d.get("kind") != "Secret":
            continue
        for key, opt in PIPELINES.items():
            if key not in (d.get("data") or {}):
                continue
            pipeline = yaml.safe_load(base64.b64decode(d["data"][key]).decode()) or {}
            for sink in pipeline.get("sinks") or []:
                for pub in sink.get("publishers") or []:
                    where = "%s %s sink %s: %s" % (d["metadata"]["name"], key, sink.get("name"), pub)
                    checked.append(where)
                    u = urlsplit(pub)
                    if u.scheme in API_CONSUMERS:
                        comp = API_CONSUMERS[u.scheme]
                        if comp not in in_closure:
                            failures.append("%s: its consumer, %s's API, is not in ceilometer's deps closure %s, so "
                                            "a selected ceilometer cannot count on it" % (where, comp,
                                                                                         sorted(in_closure)))
                    elif u.scheme == "notifier":
                        topics = parse_qs(u.query).get("topic") or (pub_topics.get(opt) or [TOPIC_DEFAULTS[opt]])
                        vh = vhost(pub) if u.netloc else vhost(notification_transport(conf))
                        for t in topics:
                            got = [r for r in rows if r[2] == vh and r[3] == t]
                            if got and all(r[0] == "ceilometer" for r in got):
                                failures.append(
                                    "%s: publishes to queue %s.sample on vhost %s, which only ceilometer's own "
                                    "notification agent consumes: it would re-ingest its own samples as notifications "
                                    "(a loop), which is not a consumer" % (where, t, vh))
                            elif not got:
                                failures.append(
                                    "%s: publishes to queue %s.sample on vhost %s, which no listener in ceilometer's "
                                    "closure consumes (listeners: %s), so it grows without bound (#294)"
                                    % (where, t, vh, sorted({"%s %s/%s" % (r[1], r[2], r[3]) for r in rows})))
                    else:
                        failures.append("%s: no consumer is known for the %r publisher; measure its consumer and add "
                                        "it to this gate first" % (where, u.scheme))
    if not checked:
        failures.append("no publisher was read from ceilometer's pipeline.yaml or event_pipeline.yaml; the check had "
                        "no input")
    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    print("OK: %d ceilometer publisher(s), each with a consumer in its closure %s:" % (len(checked), sorted(in_closure)))
    for c in checked:
        print("  " + c)


main()
