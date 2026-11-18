"""The Secret External Secrets Operator writes for an ExternalSecret, as far as an admission webhook can see it.

    import eso_model   (importlib from this file)
    eso_model.secret(external_secret, namespace) -> a v1 Secret (dict)
    eso_model.self_test() -> list of failures (empty when every case holds)

hack/validate-operator-admission.py runs the operators' real admission webhooks in kind (#405), and the topology
operator's User webhook reads the Secret a User imports (its name and labels, rabbitmq.com/topology-operator, #404). In
an install that Secret is written by ESO from the ExternalSecret's spec.target; in kind there is no ESO and no store, so
this function writes it. It is a MODEL of ESO (external-secrets v2.14.0, the version create.sh installs), so a check
built on it cannot validate ESO itself: what it shows is that the operators admit our objects given the Secret ESO is
documented to write. The H2 live test measures the real thing.

What it models (ESO's ExternalSecret spec.target, v1 API):
  - name: target.name, or the ExternalSecret's own name when unset;
  - type: target.template.type, else Opaque;
  - labels and annotations: target.template.metadata.{labels,annotations}, plus the label ESO adds to every Secret it
    manages (reconcile.external-secrets.io/managed: "true");
  - keys: target.template.data's keys when a template with data is set (mergePolicy Replace, the default), else each
    spec.data[].secretKey. Values are placeholders: no webhook here reads a value, and none must be invented.
What it does not model: dataFrom, templateFrom, mergePolicy Merge, creationPolicy other than Owner/Orphan (Merge and None
write no new Secret), and anything ESO does at refresh. secret() refuses the forms it does not model.
"""

PLACEHOLDER = "gate-placeholder"
MANAGED = ("reconcile.external-secrets.io/managed", "true")


class Unmodelled(Exception):
    pass


def secret(es, namespace):
    spec = es.get("spec") or {}
    target = spec.get("target") or {}
    tpl = target.get("template") or {}
    if spec.get("dataFrom") or tpl.get("templateFrom") or tpl.get("mergePolicy") not in (None, "Replace") \
            or target.get("creationPolicy") not in (None, "Owner", "Orphan"):
        raise Unmodelled("ExternalSecret %s uses a form eso_model does not model (dataFrom, templateFrom, "
                         "mergePolicy Merge, or creationPolicy %r)" % (es["metadata"]["name"], target.get("creationPolicy")))
    meta = tpl.get("metadata") or {}
    labels = dict(meta.get("labels") or {})
    labels[MANAGED[0]] = MANAGED[1]
    keys = list((tpl.get("data") or {}).keys()) if tpl.get("data") else [d["secretKey"] for d in spec.get("data") or []]
    out = {"apiVersion": "v1", "kind": "Secret",
           "metadata": {"name": target.get("name") or es["metadata"]["name"], "namespace": namespace, "labels": labels},
           "type": tpl.get("type") or "Opaque",
           "stringData": {k: PLACEHOLDER for k in keys}}
    if meta.get("annotations"):
        out["metadata"]["annotations"] = dict(meta["annotations"])
    return out


def self_test():
    """Cases from ESO's documented target semantics. A failure here means the model, not a chart, is wrong."""
    def es(name, target=None, data=None, **spec):
        s = {"target": target or {}, "data": data or [], **spec}
        return {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret", "metadata": {"name": name}, "spec": s}
    fails = []

    def want(label, got, exp):
        if got != exp:
            fails.append("%s: got %r, want %r" % (label, got, exp))
    s = secret(es("a", data=[{"secretKey": "password"}]), "ns")
    want("name defaults to the ExternalSecret's", s["metadata"]["name"], "a")
    want("type defaults to Opaque", s["type"], "Opaque")
    want("keys from spec.data without a template", sorted(s["stringData"]), ["password"])
    want("ESO's managed label is always set", s["metadata"]["labels"], {MANAGED[0]: MANAGED[1]})
    s = secret(es("a", target={"name": "b", "template": {"type": "kubernetes.io/basic-auth",
                                                         "metadata": {"labels": {"x": "y"}, "annotations": {"p": "q"}},
                                                         "data": {"username": "u", "password": "{{ .password }}"}}},
                  data=[{"secretKey": "password"}]), "ns")
    want("target.name wins", s["metadata"]["name"], "b")
    want("template type", s["type"], "kubernetes.io/basic-auth")
    want("template labels plus the managed label", s["metadata"]["labels"], {"x": "y", MANAGED[0]: MANAGED[1]})
    want("template annotations", s["metadata"].get("annotations"), {"p": "q"})
    want("template data keys replace spec.data keys", sorted(s["stringData"]), ["password", "username"])
    want("values are placeholders, never invented", set(s["stringData"].values()), {PLACEHOLDER})
    for label, bad in (("dataFrom", es("a", dataFrom=[{"extract": {"key": "k"}}])),
                       ("mergePolicy Merge", es("a", target={"template": {"mergePolicy": "Merge"}})),
                       ("creationPolicy Merge", es("a", target={"creationPolicy": "Merge"}))):
        try:
            secret(bad, "ns")
            fails.append("%s is modelled silently; it must be refused" % label)
        except Unmodelled:
            pass
    return fails
