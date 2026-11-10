#!/usr/bin/env python3
"""Keystone's OIDC client secret comes from a Kubernetes Secret, never from values.

mod_auth_openidc reads its client secret from the OIDCClientSecret directive. The keystone chart's
Apache config is free-form (conf.wsgi_keystone), so before this gate a deployer configuring OIDC
federation wrote the secret into values, and values reach the Composition CR in plaintext: anyone
who can read the CR could read Keystone's client secret.

The chart now owns the directive. With oidc.clientSecretRef set, keystone-api gets the env var
OIDC_CLIENT_SECRET from that Secret and the chart renders `OIDCClientSecret ${OIDC_CLIENT_SECRET}`
in its own file under conf.software.apache2.conf_dir; Apache resolves it from the process
environment at startup (measured in ghcr.io/krateo-blueprints/keystone-oidc:2025.1-ubuntu_jammy:
the token request of a real code flow carried the Secret's value). Any OIDCClientSecret in the
free-form config is refused at render time.

This gate renders the keystone chart and checks:
  - every values source a deployer gets from this repo (the chart defaults, the Keystone
    Compositions under examples/, the umbrella's componentValues.keystone) renders, and every
    OIDCClientSecret it renders is exactly `${OIDC_CLIENT_SECRET}`;
  - every hack/tests/keystone_oidc/enabled-*.yaml renders the chart-owned directive, mounts it
    under the Apache conf dir, and wires OIDC_CLIENT_SECRET from the Secret it names, with no
    literal value;
  - every hack/tests/keystone_oidc/literal-*.yaml (a client secret written into the free-form
    config) is refused, and refused for that reason;
  - half a clientSecretRef (a name without a key) is refused.

It fails when it finds no enabled fixture or renders no OIDCClientSecret directive at all: a gate
that looked at nothing has measured nothing.

Usage: hack/validate-keystone-oidc-secret.py [repo-root]
"""
import argparse
import base64
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("job_hooks", HERE / "validate-job-hooks.py")
job_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job_hooks)
yaml = job_hooks.yaml

ENV_FORM = "${OIDC_CLIENT_SECRET}"
ENV_NAME = "OIDC_CLIENT_SECRET"
OWNED_KEY = "oidc-client-secret.conf"
DIRECTIVE = re.compile(r"^[ \t]*OIDCClientSecret(?:[ \t]+(.*?))?[ \t]*$", re.IGNORECASE | re.MULTILINE)


def config_texts(docs):
    """(where, text) for every ConfigMap and Secret data entry, Secrets base64-decoded."""
    out = []
    for d in docs:
        kind = d.get("kind")
        if kind not in ("ConfigMap", "Secret"):
            continue
        name = (d.get("metadata") or {}).get("name", "?")
        for key, val in (d.get("data") or {}).items():
            text = str(val)
            if kind == "Secret":
                try:
                    text = base64.b64decode(text).decode("utf-8", "replace")
                except Exception:
                    pass
            out.append((f"{kind}/{name} {key}", text))
        for key, val in (d.get("stringData") or {}).items():
            out.append((f"{kind}/{name} {key}", str(val)))
    return out


def directives(docs):
    """(where, value) for every OIDCClientSecret directive in rendered config."""
    found = []
    for where, text in config_texts(docs):
        for m in DIRECTIVE.finditer(text):
            found.append((where, (m.group(1) or "").strip().strip('"').strip("'")))
    return found


def api_container(docs):
    for d in docs:
        if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("keystone-api"):
            for c in d["spec"]["template"]["spec"]["containers"]:
                if c["name"] == "keystone-api":
                    return c
    return None


def literal_findings(source, found):
    """Report every directive whose value is anything but the env reference (redacted)."""
    return [f"{source}: {where}: OIDCClientSecret has a literal value ({len(v)} chars, not printed)"
            for where, v in found if v != ENV_FORM]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    chart = root / "blueprints" / "keystone" / "chart"
    toolkit = root / "charts" / "helm-toolkit"
    tests = root / "hack" / "tests" / "keystone_oidc"
    if not (chart / "Chart.yaml").is_file():
        print(f"FAIL: no keystone chart at {chart} -- nothing was measured")
        return 1
    values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
    conf_dir = (((values.get("conf") or {}).get("software") or {}).get("apache2") or {}).get("conf_dir")

    failures, directives_seen = [], 0
    enabled = sorted(tests.glob("enabled-*.yaml"))
    literal = sorted(tests.glob("literal-*.yaml"))
    if not enabled:
        print(f"FAIL: no {tests.relative_to(root)}/enabled-*.yaml -- the chart-owned directive "
              "was never rendered, so nothing was measured")
        return 1
    if not literal:
        print(f"FAIL: no {tests.relative_to(root)}/literal-*.yaml -- the refusal was never exercised")
        return 1

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)

        def render(extra):
            return job_hooks.render(chart, toolkit, "keystone", work / "r", extra)

        def values_file(name, data):
            p = work / name
            p.write_text(yaml.safe_dump(data, sort_keys=False))
            return p

        # Values sources a deployer gets from this repo.
        sources = [("chart defaults", [])]
        for ex in sorted((root / "examples").glob("*.yaml")):
            for i, d in enumerate(yaml.safe_load_all(ex.read_text())):
                if isinstance(d, dict) and d.get("kind") == "Keystone" and d.get("spec"):
                    p = values_file(f"example-{ex.stem}-{i}.yaml", d["spec"])
                    sources.append((f"{ex.relative_to(root)} (Keystone #{i})", ["-f", str(p)]))
        umbrella = root / "blueprints" / "openstack" / "chart" / "values.yaml"
        if umbrella.is_file():
            cv = ((yaml.safe_load(umbrella.read_text()) or {}).get("componentValues") or {}).get("keystone")
            if cv:
                p = values_file("umbrella-componentValues-keystone.yaml", cv)
                sources.append(("umbrella componentValues.keystone", ["-f", str(p)]))
        for source, extra in sources:
            try:
                docs = render(extra)
            except Exception as e:
                failures.append(f"{source}: did not render: {str(e)[-300:]}")
                continue
            found = directives(docs)
            directives_seen += len(found)
            failures += literal_findings(source, found)
            print(f"{'FAIL' if literal_findings(source, found) else 'OK  '} {source}: "
                  f"{len(found)} OIDCClientSecret directive(s)")

        # The chart-owned directive, rendered from a Secret reference.
        for f in enabled:
            source = str(f.relative_to(root))
            ref = ((yaml.safe_load(f.read_text()) or {}).get("oidc") or {}).get("clientSecretRef") or {}
            try:
                docs = render(["-f", str(f)])
            except Exception as e:
                failures.append(f"{source}: did not render: {str(e)[-300:]}")
                continue
            bad = []
            found = directives(docs)
            directives_seen += len(found)
            bad += literal_findings(source, found)
            owned = [w for w, v in found if w.endswith(f" {OWNED_KEY}") and v == ENV_FORM]
            if len(owned) != 1:
                bad.append(f"{source}: expected exactly one chart-owned `OIDCClientSecret {ENV_FORM}` "
                           f"in {OWNED_KEY}, found {len(owned)} -- the chart did not render the "
                           "directive from oidc.clientSecretRef")
            others = [w for w, _ in found if not w.endswith(f" {OWNED_KEY}")]
            if others:
                bad.append(f"{source}: OIDCClientSecret outside the chart-owned file: {others}")
            c = api_container(docs)
            if c is None:
                bad.append(f"{source}: no keystone-api container rendered")
            else:
                envs = [e for e in (c.get("env") or []) if e.get("name") == ENV_NAME]
                if len(envs) != 1:
                    bad.append(f"{source}: keystone-api has {len(envs)} {ENV_NAME} env entries, want 1")
                else:
                    e = envs[0]
                    sk = (e.get("valueFrom") or {}).get("secretKeyRef") or {}
                    if "value" in e:
                        bad.append(f"{source}: {ENV_NAME} carries a literal value")
                    if sk.get("name") != ref.get("name") or sk.get("key") != ref.get("key"):
                        bad.append(f"{source}: {ENV_NAME} is not read from Secret "
                                   f"{ref.get('name')!r} key {ref.get('key')!r} (got {sk})")
                    if sk.get("optional"):
                        bad.append(f"{source}: {ENV_NAME}'s secretKeyRef is optional: without the "
                                   "Secret Apache would send the literal text ${OIDC_CLIENT_SECRET}")
                mounts = [m for m in c.get("volumeMounts") or [] if m.get("subPath") == OWNED_KEY]
                want = f"{conf_dir}/{OWNED_KEY}"
                if [m.get("mountPath") for m in mounts] != [want]:
                    bad.append(f"{source}: {OWNED_KEY} is not mounted at {want} "
                               f"(got {[m.get('mountPath') for m in mounts]})")
            failures += bad
            print(f"{'FAIL' if bad else 'OK  '} {source}: chart-owned directive from "
                  f"Secret {ref.get('name')!r}/{ref.get('key')!r}")

        # A secret written into the free-form config must be refused, and for that reason.
        for f in literal:
            source = str(f.relative_to(root))
            try:
                docs = render(["-f", str(f)])
            except Exception as e:
                if "OIDCClientSecret" in str(e) and "clientSecretRef" in str(e):
                    print(f"OK   {source}: refused at render")
                else:
                    failures.append(f"{source}: failed to render, but not with the OIDCClientSecret "
                                    f"refusal: {str(e)[-300:]}")
                continue
            found = directives(docs)
            directives_seen += len(found)
            failures.append(f"{source}: rendered instead of being refused; "
                            f"{len(literal_findings(source, found))} literal OIDCClientSecret line(s)")
            failures += literal_findings(source, found)
            print(f"FAIL {source}: rendered a client secret written into values")

        # Half a reference configures nothing; it must not render silently.
        try:
            render(["--set", "oidc.clientSecretRef.name=keystone-oidc-client"])
            failures.append("oidc.clientSecretRef with a name and no key rendered silently")
            print("FAIL half reference: rendered")
        except Exception as e:
            if "clientSecretRef" in str(e):
                print("OK   half reference: refused at render")
            else:
                failures.append(f"half reference failed for another reason: {str(e)[-300:]}")

    if directives_seen == 0:
        print("FAIL: no OIDCClientSecret directive rendered anywhere -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures:
        print(f"FAIL: {len(failures)} problem(s); Keystone's OIDC client secret must come from "
              "oidc.clientSecretRef (a Secret), never from values")
        return 1
    print(f"OK: {directives_seen} OIDCClientSecret directive(s) rendered, every one `{ENV_FORM}` "
          "from a Secret; literal secrets in values are refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
