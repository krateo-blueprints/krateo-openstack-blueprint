#!/usr/bin/env python3
"""Every Python script we ship into a container must compile without warnings.

Found the hard way: the keystone fernet init container logged

    /tmp/fernet-manage.py:115: SyntaxWarning: invalid escape sequence '\\d'
      and re.match("^\\d+$", name)]

`"^\\d+$"` should be a raw string. Harmless today, but the warning has been escalating across
Python releases and eventually becomes an error -- at which point a probe or a setup job stops
running, in a container, at install time, where nobody is reading warnings.

**Checks the RENDERED script, not the template.** The templates are not Python: they contain Go
template directives, and no textual substitution makes them parseable in every context. A first
attempt replaced `{{ ... }}` with `""`, which turns `log.{{ .Values.x }}` into `log.""` -- and
duly reported three SyntaxErrors in scripts that demonstrably run. All three were artifacts of
the check itself. So this renders each chart and pulls the scripts out of the ConfigMaps, which
is the text the interpreter actually sees.

Scripts under files/ are plain Python and are compiled directly.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import warnings

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
TOOLKIT = ROOT / "charts/helm-toolkit"


def render(chart: pathlib.Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp) / "chart"
        shutil.copytree(chart, work)
        cy = work / "Chart.yaml"
        cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
        if TOOLKIT.is_dir():
            (work / "charts").mkdir(exist_ok=True)
            shutil.copytree(TOOLKIT, work / "charts/helm-toolkit", dirs_exist_ok=True)
        out = subprocess.run(
            # kubeVersion is constrained in these charts; without --kube-version helm renders
            # nothing and a gate over an empty document set passes everything.
            ["helm", "template", "gate", str(work), "--kube-version", "1.36.0"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            return ""
        return out.stdout


def check(name: str, body: str, origin: str, findings: list) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            compile(body, name, "exec")
        except SyntaxError as e:
            findings.append((origin, name, f"SyntaxError line {e.lineno}: {e.msg}"))
            return
        for item in caught:
            if issubclass(item.category, SyntaxWarning):
                findings.append((origin, name, f"{item.category.__name__} line {item.lineno}: {item.message}"))


try:
    import yaml
except ImportError:
    print("SKIP: PyYAML unavailable")
    sys.exit(0)

charts = sorted(p for p in ROOT.glob("blueprints/*/chart") if p.is_dir())
if not charts:
    print(f"FAILED MEASUREMENT: no blueprints/*/chart under {ROOT}")
    sys.exit(2)

findings: list = []
checked = 0
rendered_charts = 0

for chart in charts:
    bp = chart.parent.name
    has_tpl = any((chart / "templates/bin").glob("*.py.tpl")) if (chart / "templates/bin").is_dir() else False
    files_py = sorted((chart / "files").glob("*.py")) if (chart / "files").is_dir() else []

    for f in files_py:
        check(f.name, f.read_text(errors="replace"), bp, findings)
        checked += 1

    if not has_tpl:
        continue
    out = render(chart)
    if not out:
        findings.append((bp, "<render>", "helm template failed, so its scripts were never checked"))
        continue
    rendered_charts += 1
    for doc in yaml.safe_load_all(out):
        if not doc or doc.get("kind") != "ConfigMap":
            continue
        for name, body in (doc.get("data") or {}).items():
            if name.endswith(".py") and isinstance(body, str):
                check(name, body, bp, findings)
                checked += 1

if checked == 0:
    print("FAILED MEASUREMENT: compiled 0 shipped python scripts -- a gate over nothing "
          "passes everything")
    sys.exit(2)

if findings:
    print(f"FAIL: {len(findings)} problem(s) in shipped python")
    for origin, name, msg in findings:
        print(f"  {origin}/{name}: {msg}")
    print()
    print("  A SyntaxWarning today is an error in a later Python. These run inside containers")
    print("  at install time, where nobody reads warnings. Use a raw string for regexes.")
    sys.exit(1)

print(f"OK: {checked} shipped python scripts compile without warnings "
      f"({rendered_charts} charts rendered)")
