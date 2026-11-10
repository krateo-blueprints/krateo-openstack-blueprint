#!/usr/bin/env python3
"""Keystone's OIDC secrets come from Kubernetes Secrets, never from values.

mod_auth_openidc has two secrets: OIDCClientSecret, the client's credential at the identity
provider, and OIDCCryptoPassphrase, the key it encrypts its state and session cookies with. The
keystone chart's Apache config is free-form (conf.wsgi_keystone), so before this gate a deployer
configuring OIDC federation wrote both into values, and values reach the Composition CR in
plaintext: anyone who can read the CR could read Keystone's client secret, and decrypt or forge
its cookies.

The chart now owns both directives. oidc.clientSecretRef and oidc.cryptoPassphraseRef name
Secrets; keystone-api gets OIDC_CLIENT_SECRET and OIDC_CRYPTO_PASSPHRASE from them, and the chart
renders `OIDCClientSecret ${OIDC_CLIENT_SECRET}` and `OIDCCryptoPassphrase
${OIDC_CRYPTO_PASSPHRASE}` in its own file under conf.software.apache2.conf_dir. Apache resolves
them from the process environment at startup. Measured in
ghcr.io/krateo-blueprints/keystone-oidc:2025.1-ubuntu_jammy: a real code flow's token request
carried the client secret's value, and a state cookie made under one passphrase was rejected under
another. Either directive in the free-form config is refused at render time, and so is OIDC without
a passphrase: mod_auth_openidc 2.4.11 refuses to start without one, and `apache2 -t` does not
notice.

This gate renders the keystone chart and checks:
  - every values source a deployer gets from this repo (the chart defaults, the Keystone
    Compositions under examples/, the umbrella's componentValues.keystone) renders, and every
    such directive it renders is the env reference;
  - every hack/tests/keystone_oidc/enabled-*.yaml renders one chart-owned directive per reference
    it sets, mounts the chart-owned file under the Apache conf dir, and wires each env var from the
    Secret the reference names (non-optional, no literal value); together they cover both secrets;
  - every literal-*.yaml (a secret written into the free-form config) is refused for that reason,
    and both directives have such a fixture;
  - every missing-passphrase-*.yaml (OIDC without oidc.cryptoPassphraseRef) is refused;
  - half a reference (a name without a key) is refused, for each reference;
  - the refusals an upgrading install can hit (a literal secret, OIDC without a passphrase) point
    at docs/configuration.md's "Upgrading an install that already uses OIDC", and that section
    exists.

It fails when a fixture class is missing or no directive is rendered at all: a gate that looked at
nothing has measured nothing.

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

OWNED_KEY = "oidc-secrets.conf"
# The upgrade path for installs that wrote the secrets into values before the chart owned them.
# Every refusal an upgrading install can hit must point here, and the heading must exist.
UPGRADE_DOC = "docs/configuration.md"
UPGRADE_HEADING = "Upgrading an install that already uses OIDC"
UPGRADE_POINTER = f'See {UPGRADE_DOC}, section "{UPGRADE_HEADING}"'
# directive -> (values reference under oidc, env var)
SECRETS = {
    "OIDCClientSecret": ("clientSecretRef", "OIDC_CLIENT_SECRET"),
    "OIDCCryptoPassphrase": ("cryptoPassphraseRef", "OIDC_CRYPTO_PASSPHRASE"),
}


def env_form(directive):
    return "${%s}" % SECRETS[directive][1]


def pattern(directive):
    return re.compile(r"^[ \t]*%s(?:[ \t]+(.*?))?[ \t]*$" % directive, re.IGNORECASE | re.MULTILINE)


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
    """(directive, where, value) for every secret directive in rendered config."""
    found = []
    for where, text in config_texts(docs):
        for directive in SECRETS:
            for m in pattern(directive).finditer(text):
                found.append((directive, where, (m.group(1) or "").strip().strip('"').strip("'")))
    return found


def literal_findings(source, found):
    """Every directive whose value is anything but its env reference (value redacted)."""
    return [f"{source}: {where}: {d} has a literal value ({len(v)} chars, not printed)"
            for d, where, v in found if v != env_form(d)]


def api_container(docs):
    for d in docs:
        if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("keystone-api"):
            for c in d["spec"]["template"]["spec"]["containers"]:
                if c["name"] == "keystone-api":
                    return c
    return None


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

    enabled = sorted(tests.glob("enabled-*.yaml"))
    literal = sorted(tests.glob("literal-*.yaml"))
    missing = sorted(tests.glob("missing-passphrase-*.yaml"))
    rel = tests.relative_to(root) if tests.is_relative_to(root) else tests
    for files, what in ((enabled, "enabled-*.yaml: the chart-owned directives were never rendered"),
                        (literal, "literal-*.yaml: the refusal was never exercised"),
                        (missing, "missing-passphrase-*.yaml: OIDC without a passphrase was never tried")):
        if not files:
            print(f"FAIL: no {rel}/{what} -- nothing was measured")
            return 1

    failures, rendered_directives = [], 0
    covered_enabled, covered_literal = set(), set()
    doc = root / UPGRADE_DOC
    if not doc.is_file() or f"\n## {UPGRADE_HEADING}\n" not in doc.read_text():
        failures.append(f"{UPGRADE_DOC} has no '## {UPGRADE_HEADING}' section, which every refusal "
                        "an upgrading install can hit points at")

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)

        def render(extra):
            return job_hooks.render(chart, toolkit, "keystone", work / "r", extra)

        def values_file(name, data):
            p = work / name
            p.write_text(yaml.safe_dump(data, sort_keys=False))
            return p

        def refused(source, extra, must_mention):
            """Expect the render to fail with a message naming every string in must_mention."""
            nonlocal rendered_directives
            try:
                docs = render(extra)
            except Exception as e:
                missing_words = [w for w in must_mention if w not in str(e)]
                if missing_words:
                    failures.append(f"{source}: failed to render, but not for the expected reason "
                                    f"(no {missing_words} in): {str(e)[-300:]}")
                    print(f"FAIL {source}: refused for another reason")
                else:
                    print(f"OK   {source}: refused at render")
                return
            found = directives(docs)
            rendered_directives += len(found)
            failures.append(f"{source}: rendered instead of being refused")
            failures.extend(literal_findings(source, found))
            print(f"FAIL {source}: rendered")

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
            rendered_directives += len(found)
            bad = literal_findings(source, found)
            failures += bad
            print(f"{'FAIL' if bad else 'OK  '} {source}: {len(found)} secret directive(s)")

        # The chart-owned directives, rendered from Secret references.
        for f in enabled:
            source = str(f.relative_to(root))
            oidc = (yaml.safe_load(f.read_text()) or {}).get("oidc") or {}
            try:
                docs = render(["-f", str(f)])
            except Exception as e:
                failures.append(f"{source}: did not render: {str(e)[-300:]}")
                continue
            found = directives(docs)
            rendered_directives += len(found)
            bad = literal_findings(source, found)
            outside = [f"{d} in {w}" for d, w, _ in found if not w.endswith(f" {OWNED_KEY}")]
            if outside:
                bad.append(f"{source}: secret directive outside the chart-owned file: {outside}")
            c = api_container(docs)
            if c is None:
                bad.append(f"{source}: no keystone-api container rendered")
            set_refs = []
            for directive, (refkey, env) in SECRETS.items():
                ref = oidc.get(refkey) or {}
                if not (ref.get("name") and ref.get("key")):
                    continue
                set_refs.append(directive)
                owned = [w for d, w, v in found
                         if d == directive and w.endswith(f" {OWNED_KEY}") and v == env_form(directive)]
                if len(owned) != 1:
                    bad.append(f"{source}: expected exactly one chart-owned `{directive} "
                               f"{env_form(directive)}` in {OWNED_KEY}, found {len(owned)} -- the "
                               f"chart did not render it from oidc.{refkey}")
                if c is not None:
                    envs = [e for e in (c.get("env") or []) if e.get("name") == env]
                    if len(envs) != 1:
                        bad.append(f"{source}: keystone-api has {len(envs)} {env} env entries, want 1")
                    else:
                        e = envs[0]
                        sk = (e.get("valueFrom") or {}).get("secretKeyRef") or {}
                        if "value" in e:
                            bad.append(f"{source}: {env} carries a literal value")
                        if sk.get("name") != ref.get("name") or sk.get("key") != ref.get("key"):
                            bad.append(f"{source}: {env} is not read from Secret {ref.get('name')!r} "
                                       f"key {ref.get('key')!r} (got {sk})")
                        if sk.get("optional"):
                            bad.append(f"{source}: {env}'s secretKeyRef is optional: without the "
                                       f"Secret Apache would use the literal text {env_form(directive)}")
                if not [x for x in bad if source in x]:
                    covered_enabled.add(directive)
            if c is not None and set_refs:
                mounts = [m.get("mountPath") for m in c.get("volumeMounts") or []
                          if m.get("subPath") == OWNED_KEY]
                want = f"{conf_dir}/{OWNED_KEY}"
                if mounts != [want]:
                    bad.append(f"{source}: {OWNED_KEY} is not mounted at {want} (got {mounts})")
            if not set_refs:
                bad.append(f"{source}: sets neither oidc.clientSecretRef nor oidc.cryptoPassphraseRef")
            failures += bad
            print(f"{'FAIL' if bad else 'OK  '} {source}: chart-owned {', '.join(set_refs) or 'nothing'}")

        # A secret written into the free-form config must be refused, and for that reason.
        for f in literal:
            text = f.read_text()
            named = [d for d in SECRETS if pattern(d).search(text)]
            if len(named) != 1:
                failures.append(f"{f.relative_to(root)}: must write exactly one secret directive, "
                                f"found {named}")
                continue
            covered_literal.add(named[0])
            refused(str(f.relative_to(root)), ["-f", str(f)], [f"carries an {named[0]} directive", UPGRADE_POINTER])

        # OIDC without a passphrase would crash-loop keystone-api, so it must not render.
        for f in missing:
            refused(str(f.relative_to(root)), ["-f", str(f)], ["oidc.cryptoPassphraseRef is not set", UPGRADE_POINTER])

        # Half a reference configures nothing; it must not render silently.
        for refkey in ("clientSecretRef", "cryptoPassphraseRef"):
            refused(f"half oidc.{refkey} (name, no key)",
                    ["--set", f"oidc.{refkey}.name=some-secret"], [f"oidc.{refkey} needs both name and key"])

    for directive in SECRETS:
        if directive not in covered_enabled:
            failures.append(f"no enabled-*.yaml fixture rendered a correct chart-owned {directive}")
        if directive not in covered_literal:
            failures.append(f"no literal-*.yaml fixture exercises a literal {directive}")
    if rendered_directives == 0:
        print("FAIL: no OIDCClientSecret or OIDCCryptoPassphrase rendered anywhere -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures:
        print(f"FAIL: {len(failures)} problem(s); Keystone's OIDC secrets must come from "
              "oidc.clientSecretRef and oidc.cryptoPassphraseRef (Secrets), never from values")
        return 1
    print(f"OK: {rendered_directives} secret directive(s) rendered, each an env reference read from a "
          "Secret; literal secrets in values, OIDC without a passphrase and half references are refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
