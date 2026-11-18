"""The values a gate renders a component chart with: what the umbrella delivers for its storage, nothing else (#308).

    gate_values.helm_args(chart_dir) -> ["-f", <file>] or []

cinder's chart default refuses to render (krateo.storageBackend: none, #196): through the umbrella it always receives
storage.backend, and a standalone Cinder Composition must choose. So a gate that renders every chart at its defaults
would render cinder's refusal and measure nothing of it. Each such gate passes these arguments, placed BEFORE its own
values files so its own overrides still win.

For a chart whose umbrella entry requires block-storage (cinder), the values are exactly what storage.backend
ceph-external adds to the spec the umbrella delivers it, compared with none: krateo.storageBackend, the krateo.ceph
references to the copied keys, and the rbd1 pool, user and libvirt secret UUID. They are computed from the umbrella's own
spec-building lines (hack/umbrella_spec.py), never written here, so they cannot drift from what the umbrella does. One
render covers both Ceph backends: the chart receives the same values for ceph-rook, whose references name the same
copied roots. For every other chart: no arguments, so its render is the command it always was, byte for byte.

The component is the staged chart's Chart.yaml `name`, which equals its blueprint directory for every chart here, so a
gate need not thread the name through its staging. Raises GateValuesError when the umbrella cannot be rendered: a gate
must fail, never render cinder's refusal and call that a measurement.
"""
import atexit
import os
import shutil
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GateValuesError(Exception):
    pass


_cache = {}
_tmp = []


def _cleanup():
    for d in _tmp:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)


def block_storage_components(root=ROOT):
    uv = yaml.safe_load(open(os.path.join(root, "blueprints", "openstack", "chart", "values.yaml"))) or {}
    return {c["name"] for c in uv.get("components") or [] if "block-storage" in (c.get("requires") or [])}


def delta(new, old):
    """The parts of `new` that differ from `old`, recursively over mappings."""
    out = {}
    for k, v in (new or {}).items():
        if isinstance(v, dict) and isinstance((old or {}).get(k), dict):
            d = delta(v, old[k])
            if d:
                out[k] = d
        elif (old or {}).get(k) != v:
            out[k] = v
    return out


def values_for(component, root=ROOT):
    key = (root, component)
    if key in _cache:
        return _cache[key]
    if component not in block_storage_components(root):
        _cache[key] = {}
        return {}
    import importlib.util
    spec = importlib.util.spec_from_file_location("umbrella_spec", os.path.join(root, "hack", "umbrella_spec.py"))
    us = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(us)
    try:
        none = us.specs(root, {}).get(component)
        ext = us.specs(root, {"storage": us.ceph_external_values()}).get(component)
    except us.SpecError as e:
        raise GateValuesError("cannot compute what the umbrella delivers %s: %s" % (component, e))
    if none is None or ext is None:
        raise GateValuesError("the umbrella delivers no spec to %s" % component)
    d = delta(ext, none)
    if ((d.get("krateo") or {}).get("storageBackend")) != "ceph-external" or not (d.get("krateo") or {}).get("ceph"):
        raise GateValuesError("the umbrella's ceph-external delta for %s carries no storageBackend and references: %s"
                              % (component, d))
    _cache[key] = d
    return d


def component_of(chart_dir):
    with open(os.path.join(str(chart_dir), "Chart.yaml")) as f:
        return (yaml.safe_load(f) or {}).get("name") or os.path.basename(os.path.dirname(os.path.abspath(str(chart_dir))))


def helm_args(chart_dir, root=ROOT):
    """["-f", file] carrying values_for(the chart's component), or [] when there are none."""
    vals = values_for(component_of(chart_dir), root)
    if not vals:
        return []
    d = tempfile.mkdtemp(prefix="gate-values-")
    _tmp.append(d)
    p = os.path.join(d, "values.yaml")
    with open(p, "w") as f:
        yaml.safe_dump(vals, f)
    return ["-f", p]
