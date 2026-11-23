"""What the umbrella hands each component as its Composition spec, rendered from compositions.yaml itself (#308).

    import umbrella_spec   (hack/ on sys.path, or importlib from this file)
    umbrella_spec.specs(root, values) -> {component: spec}

An offline render of the umbrella emits no component (no CRD is served), so the emitted manifest cannot say what a
component would receive. This lifts the lines of templates/compositions.yaml that build a component's spec -- from
`{{- $ov := ` up to the TWO PHASES block, which only re-emits a live object's spec -- into a probe template, renders the
umbrella with `values`, and reads one JSON spec per component. It is the umbrella's own code, not a copy of its rules,
so a gate that renders a chart "as the umbrella delivers it" cannot drift from what the umbrella does (the technique of
hack/validate-rerun-hooks.py and hack/validate-storage-backend.py).

Raises SpecError when the span cannot be found or the umbrella does not render: a caller must fail, never skip.
"""
import json
import os
import shutil
import subprocess
import tempfile

import yaml

START = "{{- $ov := "
END = "{{- /* TWO PHASES"


class SpecError(Exception):
    pass


def ceph_external_values():
    """storage values that make ceph-external render: it must name its source Secrets (#308). Placeholders: offline
    nothing resolves them."""
    return {"backend": "ceph-external",
            "ceph": {"keys": {k: {"secret": "gate-ceph-" + k.lower(), "key": "key"} for k in ("cinder", "glance", "monHost")}}}


def specs(root, values=None):
    chart = os.path.join(root, "blueprints", "openstack", "chart")
    comp = open(os.path.join(chart, "templates", "compositions.yaml")).read()
    start = comp.find(START)
    end = comp.find(END, start)
    if start < 0 or end < 0:
        raise SpecError("compositions.yaml has no spec-building span (from `%s` to `%s`)" % (START, END))
    span = comp[start:end]
    tmp = tempfile.mkdtemp()
    try:
        w = os.path.join(tmp, "umbrella")
        shutil.copytree(chart, w)
        cy = os.path.join(w, "Chart.yaml")
        body = open(cy).read()
        open(cy, "w").write(body.replace("CHART_VERSION", "0.0.0-gate"))
        open(os.path.join(w, "templates", "zz-umbrella-spec-probe.yaml"), "w").write(
            '{{- $sel := splitList " " (include "osh.selected" (list $)) }}\n'
            # $rot: compositions.yaml's RabbitMQ rotation state (#309 C2), derived from the live component; offline nothing
            # is live, so it is empty, as the chart's own render finds it.
            '{{- $dr := include "osh.deregistration" (list $ (splitList " " (include "osh.selected" (list $))) (list)) | fromYaml }}\n{{- range $c := .Values.components }}\n{{- $rot := dict }}\n' + span + '\n'
            # printf, not literal text: a right-trimming action at the end of the span would swallow a literal newline
            # and join two components' lines.
            '{{ printf "\\n# UMBRELLASPEC %s %s\\n" $c.name ($ov | toJson) }}\n{{- end }}\n')
        vf = os.path.join(tmp, "values.yaml")
        open(vf, "w").write(yaml.safe_dump(values or {}))
        r = subprocess.run(["helm", "template", "openstack-gate", w, "--namespace", "openstack", "-f", vf],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SpecError("the umbrella does not render with %s: %s" % (values, r.stderr.strip()[-400:]))
        out = {}
        for line in r.stdout.splitlines():
            if line.startswith("# UMBRELLASPEC "):
                _, _, name, js = line.split(" ", 3)
                try:
                    out[name] = json.loads(js)
                except ValueError as e:
                    raise SpecError("the probe's spec line for %s does not parse (%s): %s" % (name, e, js[:200]))
        if not out:
            raise SpecError("the probe rendered no component spec")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def selected(root, values=None):
    """The components the umbrella selects for `values`, as osh.selected returns them (rendered, not inferred). A spec
    from specs() is delivered only to these: identity selects no ceilometer, and ceilometer's spec for a profile that
    does not select it has no notification vhosts, which the chart refuses (#281). Raises SpecError like specs()."""
    chart = os.path.join(root, "blueprints", "openstack", "chart")
    tmp = tempfile.mkdtemp()
    try:
        w = os.path.join(tmp, "umbrella")
        shutil.copytree(chart, w)
        cy = os.path.join(w, "Chart.yaml")
        body = open(cy).read()
        open(cy, "w").write(body.replace("CHART_VERSION", "0.0.0-gate"))
        open(os.path.join(w, "templates", "zz-umbrella-selected-probe.yaml"), "w").write(
            '{{ printf "\\n# UMBRELLASELECTED %s\\n" (include "osh.selected" (list $)) }}\n')
        vf = os.path.join(tmp, "values.yaml")
        open(vf, "w").write(yaml.safe_dump(values or {}))
        r = subprocess.run(["helm", "template", "openstack-gate", w, "--namespace", "openstack", "-f", vf],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SpecError("the umbrella does not render with %s: %s" % (values, r.stderr.strip()[-400:]))
        for line in r.stdout.splitlines():
            if line.startswith("# UMBRELLASELECTED"):
                sel = set(line.split()[2:])
                if not sel:
                    raise SpecError("osh.selected returned nothing for %s" % (values,))
                return sel
        raise SpecError("the selection probe printed no line for %s" % (values,))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
