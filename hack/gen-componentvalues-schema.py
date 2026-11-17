#!/usr/bin/env python3
"""Regenerate the umbrella's componentValues shape in
blueprints/openstack/chart/values.schema.json.

componentValues lists every component in the umbrella's chart/values.yaml by NAME, with
`additionalProperties: false`, so an unknown component is rejected. Each componentValues.<name> is
an OPEN object (`type: object` plus `x-kubernetes-preserve-unknown-fields: true`) with no embedded
properties: the umbrella carries the override through untouched, and the component's own
Composition CRD, generated from that component chart's values.schema.json, is what validates and
types it (#209).

Why not embed each component schema here, as this script used to:
  1. It validates nothing the component CRD does not already validate. The value at
     componentValues.<name> becomes that component's Composition spec, and that spec goes
     through the component CRD.
  2. It multiplies the umbrella CRD's size. Embedding all 36 full component schemas made the
     umbrella values.schema.json 376 KB and its generated CRD 396 KB per served version, and a CRD
     stores every version it still serves. Components that stay pinned at an origin version keep
     several served at once (two were observed together on a real cluster), which heads toward
     etcd's object limit. hack/validate-umbrella-crd-size gates that size.
  3. A narrower embedded copy prunes what the component would accept. That is how #209 began:
     nova's embedded schema declared only `images`, and every other override was dropped at the
     umbrella before the component ever saw it.

Each component must still have a values.schema.json, or generation fails: a component without one
has no CRD typing at all, and that should be noticed, not skipped.

Usage:  python3 hack/gen-componentvalues-schema.py
Requires: pyyaml.
"""
import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(ROOT, "blueprints/openstack/chart")


def ceph_schema():
    """storage.ceph (#308): how the install consumes a Ceph, for the ceph backends. Typed closed, so a misspelt key is
    refused rather than silently ignored; no default names a cloud provider."""
    def s(desc, **kw):
        return dict({"type": "string", "description": desc}, **kw)

    def ref(desc):
        return {"type": "object", "additionalProperties": False, "description": desc,
                "properties": {"secret": s("The Secret's name (in sourceNamespace)."),
                               "key": s("The key in that Secret.", minLength=1)}}
    return {
        "type": "object",
        "title": "Ceph consumer",
        "description": ("How the install consumes Ceph when storage.backend is ceph-rook or ceph-external (#308): each "
                        "client key and the monitor addresses are copied by External Secrets Operator from where Ceph "
                        "keeps them, and the charts project the copies. No client.admin key is read anywhere."),
        "additionalProperties": False,
        "properties": {
            "sourceNamespace": s("Where the client-key and mon_host Secrets live; empty: this release's namespace. For "
                                 "ceph-rook, the CephCluster's namespace (its owner grants the copy store read access)."),
            "clients": {"type": "object", "additionalProperties": False,
                        "description": "The Ceph user each consumer authenticates as (client.<name>); for ceph-rook, the CephClient name.",
                        "properties": {"cinder": s("cinder-volume's user, also libvirt's for attached volumes.", minLength=1),
                                       "glance": s("glance-api's user.", minLength=1)}},
            "rook": {"type": "object", "additionalProperties": False,
                     "description": "Where Rook keeps the keys and monitors (ceph-rook).",
                     "properties": {"clientSecretPrefix": s("A CephClient's Secret is <prefix><name>.", minLength=1),
                                    "clientKey": s("The key holding a CephClient's key.", minLength=1),
                                    "monHostSecret": s("The Secret holding mon_host.", minLength=1),
                                    "monHostKey": s("Its key.", minLength=1)}},
            "keys": {"type": "object", "additionalProperties": False,
                     "description": "The operator-named Secrets (ceph-external); each secret is required with ceph-external.",
                     "properties": {"cinder": ref("client.<clients.cinder>'s key."),
                                    "glance": ref("client.<clients.glance>'s key."),
                                    "monHost": ref("The monitor addresses, as ceph.conf's mon_host.")}},
            "pools": {"type": "object", "additionalProperties": False,
                      "description": "The RBD pools, created where the users are (a CephBlockPool, or the Ceph admin).",
                      "properties": {"volumes": s("cinder's volumes.", minLength=1),
                                     "images": s("glance's images (glanceStore rbd).", minLength=1)}},
            "libvirtSecretUUID": s("The libvirt secret every hypervisor defines for cinder's user, passed to cinder, "
                                   "libvirt and nova.",
                                   pattern="^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
            "glanceStore": s("pvc keeps images on glance's volume; rbd stores them in the images pool.", enum=["pvc", "rbd"]),
            "glanceImagesMigrated": {"type": "boolean", "description": (
                "With glanceStore rbd on an install whose images are on the glance-images claim, glance refuses until "
                "this is true: copy the images to the rbd store first, or confirm there is none to keep.")},
        },
    }


def main():
    vals = yaml.safe_load(open(os.path.join(CHART, "values.yaml")))
    # The umbrella pulls its components at its own chart version (#179), so that is the version
    # each component CRD describes. In the tree it is the CHART_VERSION placeholder, which the
    # release stamps in values.schema.json too, so the descriptions name the released version.
    ver = str(yaml.safe_load(open(os.path.join(CHART, "Chart.yaml")))["version"])
    props = {}
    missing = []
    for c in vals["components"]:
        name = c["name"]
        if not os.path.exists(os.path.join(ROOT, f"blueprints/{name}/chart/values.schema.json")):
            missing.append(name)
            continue
        props[name] = {
            "type": "object",
            "description": (
                f"Overrides for the {name} Composition (chart {ver}), deep-merged into its spec. "
                f"Passed through untyped here; the {name} Composition CRD validates them against "
                f"blueprints/{name}/chart/values.schema.json."
            ),
            "x-kubernetes-preserve-unknown-fields": True,
        }
        print(f"  open {name} ({ver})")
    if missing:
        sys.exit(f"ERROR: no values.schema.json for {missing}: a component needs one for its CRD to type "
                 "its overrides")

    sp = os.path.join(CHART, "values.schema.json")
    schema = json.load(open(sp))
    schema["properties"]["componentValues"] = {
        "type": ["object", "null"],
        "title": "Per-component spec overrides",
        "description": (
            "Per-component Composition spec overrides, by component name (an unknown name is "
            "rejected). The value at componentValues.<name> becomes that component's Composition "
            "spec, deep-merged; components without an entry use chart defaults. It is passed through "
            "here and typed by the component's own Composition CRD (generated by "
            "hack/gen-componentvalues-schema.py)."
        ),
        "additionalProperties": False,
        "properties": props,
    }
    # rerunHooks (#297): one token per component, by the same names, passed to that component alone as its
    # krateo.rerunHooksToken, which every component schema must declare (a key the CRD does not declare is pruned).
    undeclared = [n for n in props
                  if "rerunHooksToken" not in ((json.load(open(os.path.join(ROOT, f"blueprints/{n}/chart/values.schema.json")))
                                                ["properties"].get("krateo") or {}).get("properties") or {})]
    if undeclared:
        sys.exit(f"ERROR: {undeclared} declare no krateo.rerunHooksToken in values.schema.json: the umbrella's "
                 "rerunHooks.<component> would be pruned from their Composition spec")
    schema["properties"]["rerunHooks"] = {
        "type": ["object", "null"],
        "title": "Re-run a component's hooks",
        "description": (
            "A token per component name (an unknown name is rejected). Setting or changing "
            "rerunHooks.<component> passes it to that component alone, as its krateo.rerunHooksToken: its values "
            "change, so core-provider runs a real helm upgrade, which runs every hook again. Use the current time. "
            "README: \"A component reads Ready=False/HooksMayNotHaveRun\" (#297)."
        ),
        "additionalProperties": False,
        "properties": {n: {"type": "string", "minLength": 1,
                           "description": f"Token for {n}: any change re-runs {n}'s hooks."} for n in props},
    }
    # storage (#196, #308): the block-storage backend, passed to each component that requires block-storage as its
    # krateo.storageBackend. Each must declare that key with every backend the umbrella accepts (a key the CRD does not
    # declare is pruned, and a value its enum lacks is refused by the component's CRD instead of its chart's message).
    backends = {
        "none": "no backend (the default): profile full-strict leaves cinder out, and a cinder selected anyway "
                "refuses to render, naming this value",
        "ceph-rook": "Ceph run by Rook, whose CephClients mint the client keys (#308); storage.ceph says where",
        "ceph-external": "an existing Ceph whose admin creates the users and places their keys (#308); "
                         "storage.ceph.keys names each Secret",
    }
    consumers = [c["name"] for c in vals["components"] if "block-storage" in (c.get("requires") or [])]
    if not consumers:
        sys.exit("ERROR: no component requires block-storage, so storage.backend would reach nothing")
    for n in consumers:
        decl = (((json.load(open(os.path.join(ROOT, f"blueprints/{n}/chart/values.schema.json")))["properties"]
                  .get("krateo") or {}).get("properties") or {}).get("storageBackend") or {})
        lacking = [b for b in backends if b not in (decl.get("enum") or [])]
        if not decl or lacking:
            sys.exit(f"ERROR: {n} requires block-storage, and its values.schema.json krateo.storageBackend "
                     f"{'is not declared' if not decl else 'lacks ' + ', '.join(lacking)}: the umbrella's "
                     "storage.backend would be pruned or refused by its CRD")
    if (vals.get("storage") or {}).get("backend") not in backends:
        sys.exit(f"ERROR: the umbrella's values.yaml storage.backend {(vals.get('storage') or {}).get('backend')!r} "
                 f"is not one of {list(backends)}")
    schema["properties"]["storage"] = {
        "type": ["object", "null"],
        "title": "Storage backend",
        "description": (
            "The backend that provides block storage, the `block-storage` capability "
            + ", ".join(consumers) + " requires (#196, #308). Profile full-strict selects "
            + ", ".join(consumers) + " only when storage.backend is not none, and the umbrella passes the value to "
            "each as its krateo.storageBackend (a componentValues override of that key is refused). With none, a "
            + ", ".join(consumers) + " selected anyway (profile full, or enabled) refuses to render, naming this "
            "value. With a ceph backend the install consumes Ceph through copies of the keys Ceph mints (storage.ceph), never "
            "client.admin. See hack/provisioning-preconditions.yaml."
        ),
        "additionalProperties": False,
        "properties": {
            "ceph": ceph_schema(),
            "backend": {
                "type": "string",
                "title": "Block-storage backend",
                "description": "; ".join(f"{k} = {v}" for k, v in backends.items()) + ".",
                "enum": list(backends),
                "default": "none",
            },
        },
    }
    json.dump(schema, open(sp, "w"), indent=2)
    open(sp, "a").write("\n")
    print(f"\nwrote componentValues for {len(props)} components -> {sp}")


if __name__ == "__main__":
    main()
