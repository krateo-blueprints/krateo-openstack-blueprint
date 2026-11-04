#!/usr/bin/env python3
"""The umbrella must emit well-formed multi-document YAML, one document per component.

Issue #152: the origin-version-pin comment closed with a right-trim marker directly above the
document separator. The trim ate the newline, so from the second loop iteration onward the
separator was appended to the previous document's last line -- `spec: {}` immediately followed
by three dashes -- and helm failed the whole render with

    error converting YAML to JSON: yaml: line 5: did not find expected key

A fresh install of a seven-component closure therefore produced ONE component and then stopped
forever, while the Composition reported Ready=True.

Nothing caught it, and the reason is the point of this gate. `helm template` NEVER resolves
`lookup` -- only `--dry-run=server` does -- so offline the component gate is closed, zero
component documents are emitted, and every offline check of this template compares empty output
against empty output. hack/validate-origin-version-pin.py passed on exactly that: a correct
result from an absent opportunity.

So this gate manufactures the opportunity. It copies the chart, replaces the three lookup-driven
gate helpers with stubs that return "true", renders offline, and asserts the output parses as
multi-document YAML with one document per selected component. With the gates open the render
produces many documents, which is the only state in which the defect exists.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("FAIL: PyYAML is required by validate-umbrella-multidoc.py and is not installed")

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHART = os.path.join(ROOT, "blueprints", "openstack", "chart")
# The gates that consult the cluster. Stubbing these is what opens the emission path offline.
STUB = {"osh.crdExists": "true", "osh.depsReady": "true", "osh.depsExist": "true"}


def stub_helpers(path):
    """Replace each named define's body with a constant, keeping the rest of the file intact."""
    src = open(path).read()
    for name, value in STUB.items():
        start = src.find('{{- define "%s" -}}' % name)
        if start < 0:
            sys.exit(f"FAIL: {name} is not defined in _helpers.tpl -- this gate is out of date "
                     "with the chart and is no longer checking what it claims")
        nxt = src.find('\n{{- define ', start + 1)
        nxt = len(src) if nxt < 0 else nxt
        block = src[start:nxt]
        close = block.rfind("{{- end -}}")
        if close < 0:
            sys.exit(f"FAIL: cannot find the closing marker of {name}")
        src = src[:start] + '{{- define "%s" -}}%s{{- end -}}' % (name, value) \
            + src[start + close + len("{{- end -}}"):]
    open(path, "w").write(src)


def main():
    if not os.path.isdir(CHART):
        sys.exit(f"FAIL: no umbrella chart at {CHART} -- nothing was measured")

    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "chart")
        shutil.copytree(CHART, dst)
        for f, pat, rep in (("Chart.yaml", r"^version: CHART_VERSION$", "version: 0.0.0-gate"),
                            ("values.yaml", r"^chartVersion: .*$", 'chartVersion: "0.0.0-gate"')):
            p = os.path.join(dst, f)
            body = open(p).read()
            open(p, "w").write(re.sub(pat, rep, body, count=1, flags=re.M))
        tk = os.path.join(ROOT, "charts", "helm-toolkit")
        if os.path.isdir(tk):
            os.makedirs(os.path.join(dst, "charts"), exist_ok=True)
            shutil.copytree(tk, os.path.join(dst, "charts", "helm-toolkit"))
        stub_helpers(os.path.join(dst, "templates", "_helpers.tpl"))

        # Render ONLY compositions.yaml. `helm template --debug` prints the rendered
        # manifest even when it is not valid YAML -- helm's own error says "Use --debug flag
        # to render out invalid YAML" -- so a malformed manifest arrives here as text to
        # examine rather than as an error with no output attached. `helm install --dry-run`
        # cannot be used: it contacts the apiserver for capabilities and fails with no cluster.
        tdir = os.path.join(dst, "templates")
        for f in os.listdir(tdir):
            if not f.startswith("_") and f != "compositions.yaml":
                os.remove(os.path.join(tdir, f))

        vals = os.path.join(tmp, "vals.yaml")
        open(vals, "w").write("enabled:\n  - gnocchi\n  - skyline\n  - zaqar\n")
        p = subprocess.run(["helm", "template", "gate", dst, "--debug",
                            "-n", "openstack", "-f", vals],
                           capture_output=True, text=True)
        text = p.stdout
        if "apiVersion: composition.krateo.io" not in text:
            print("FAIL: the umbrella emitted no component documents with every gate open -- "
                  "this gate cannot see the defect it exists for")
            for ln in (p.stderr.strip().splitlines() or ["(no stderr)"])[-3:]:
                print("  " + ln)
            return 1

        try:
            docs = [d for d in yaml.safe_load_all(text) if d]
        except yaml.YAMLError as e:
            print("FAIL: the umbrella's rendered components are not valid multi-document YAML")
            print(f"  {e}")
            glued = [ln for ln in text.splitlines()
                     if ln.rstrip().endswith("---") and ln.strip() != "---"]
            for ln in glued[:3]:
                print(f"  a document separator is glued to the previous line: {ln!r}")
            return 1

        docs = [d for d in docs
                if isinstance(d, dict)
                and str(d.get("apiVersion", "")).startswith("composition.krateo.io/")]
        kinds = [d.get("kind") for d in docs if isinstance(d, dict)]
        if len(docs) < 2:
            print(f"FAIL: only {len(docs)} component document(s) emitted with every gate open "
                  "-- this gate cannot see the multi-document defect it exists for")
            return 1
        names = [f"{d['kind']}/{d['metadata']['name']}" for d in docs]
        if len(set(names)) != len(names):
            print(f"FAIL: duplicate component documents: {names}")
            return 1
        print(f"OK: {len(docs)} component documents render as well-formed multi-document YAML "
              f"({', '.join(sorted(kinds))})")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
