"""No two resources of one render may share an identity: helm template accepts that, helm install refuses it.

    import render_identity   (importlib from this file, as the gates load gate_values and umbrella_spec)
    ledger = render_identity.Ledger("gate name")
    ledger.check(label, rendered_text_or_docs)   # records a failure per duplicate
    ledger.control(rendered_text)                # once: a planted duplicate must be found
    ledger.report() -> list of failure strings    # also fails when nothing was checked

H2 run 2 (#309, 2026-11-18): every messaging client of 0.3.74-main.ge82f577 rendered ExternalSecret <svc>-rabbitmq-user
twice in operator mode, the topology projection and the chart's own connection Secret. `helm template` printed both,
every gate passed, and on GKE chart-inspector answered "rendered manifest contains duplicate resource" for eleven
components. An install needs every chart to install, so the gates that already render every chart, in each mode they
render it, pass their output through here rather than render it again:
  - validate-pod-references.py: every component chart at its defaults, under each storage.backend it depends on, and
    as the umbrella delivers it (its spec from compositions.yaml, hack/umbrella_spec.py);
  - validate-messaging-topology.py: every messaging client in operator mode;
  - validate-messaging-operator.py: the rabbitmq chart and every messaging client in operator mode.

The identity is what helm compares: the apiVersion's group (not its version), the kind, the namespace (the release's
when the object names none) and the name. A List or an empty document is not a resource.
"""
import yaml


def identities(docs, namespace):
    out = []
    for d in docs:
        if not isinstance(d, dict) or not d.get("kind") or d.get("kind") == "List":
            continue
        api = str(d.get("apiVersion") or "")
        group = api.split("/")[0] if "/" in api else ""
        meta = d.get("metadata") or {}
        out.append((group, d["kind"], meta.get("namespace") or namespace, meta.get("name")))
    return out


def duplicates(docs, namespace):
    seen, dups = set(), []
    for ident in identities(docs, namespace):
        if ident in seen and ident not in dups:
            dups.append(ident)
        seen.add(ident)
    return dups


def _docs(rendered):
    if isinstance(rendered, str):
        return [d for d in yaml.safe_load_all(rendered) if d]
    return [d for d in rendered if d]


def _fmt(ident):
    group, kind, ns, name = ident
    return "%s%s/%s/%s" % (group + "/" if group else "", kind, ns, name)


class Ledger:
    def __init__(self, gate, namespace="openstack"):
        self.gate, self.namespace = gate, namespace
        self.renders, self.resources, self.failures, self.controlled = 0, 0, [], False

    def check(self, label, rendered):
        docs = _docs(rendered)
        self.renders += 1
        self.resources += len(identities(docs, self.namespace))
        for ident in duplicates(docs, self.namespace):
            self.failures.append("%s renders %s twice: helm template accepts it, helm install refuses the release "
                                 "(\"rendered manifest contains duplicate resource\")" % (label, _fmt(ident)))

    def control(self, rendered):
        """A copy of the render's first resource appended must be reported: the check can say no."""
        if self.controlled:
            return
        docs = _docs(rendered)
        if not identities(docs, self.namespace):
            return
        first = next(d for d in docs if isinstance(d, dict) and d.get("kind") and d.get("kind") != "List")
        if not duplicates(docs + [first], self.namespace):
            self.failures.append("control: a planted duplicate of %s was not found: this check cannot say no"
                                 % _fmt(identities([first], self.namespace)[0]))
        self.controlled = True

    def report(self):
        out = list(self.failures)
        if self.renders == 0 or self.resources == 0:
            out.append("identity: %d render(s), %d resource(s) checked: nothing was measured" % (self.renders, self.resources))
        if not self.controlled:
            out.append("identity: the planted-duplicate control never ran")
        return out

    def summary(self):
        return "%d render(s), %d resource(s): no duplicate identity (group, kind, namespace, name)" % (
            self.renders, self.resources)
