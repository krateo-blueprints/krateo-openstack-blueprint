#!/usr/bin/env python3
"""An image can do what its chart asks of it: encrypted token caching needs the cryptography package (#463).

    hack/check-image-packages.py [repo-root] <results-dir>   judge Trivy's JSON reports (--list-all-pkgs)
    hack/check-image-packages.py --self-test                 the rules, against built-in cases (offline)

Measured on GKE 2026-11-23 (0.3.74): masakari answered 500 and cloudkitty dropped the connection for every request with a
valid token. Their charts set `memcache_security_strategy: ENCRYPT`, and their images have keystonemiddleware without
cryptography, so the first token to cache raised CryptoUnavailableError. Nothing in a render shows that; the image's package
list does.

For every chart with ENCRYPT under any keystone_authtoken of its conf, for every image hack/images.yaml says it runs
(`rendered: declared-only` is skipped: no default render starts it): an image whose package list has keystonemiddleware must
have cryptography too.
  * Every such image must have a report that lists packages: a report without them measured nothing.
  * Every such chart must have at least one image with keystonemiddleware, or the rule looked at the wrong images.
  * No chart with ENCRYPT at all fails too: the rule would pass on no input.
"""
import glob
import json
import os
import re
import sys

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was judged")
    sys.exit(2)

NEEDS, CARRIER = "cryptography", "keystonemiddleware"


def encrypts(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).endswith("keystone_authtoken") and isinstance(v, dict) and str(v.get("memcache_security_strategy")).upper() == "ENCRYPT":
                return True
            if encrypts(v):
                return True
    return False


def packages(report):
    """The Python package names a report lists, or None if it lists no package of any kind."""
    names, any_pkg = set(), False
    for r in report.get("Results") or []:
        for p in r.get("Packages") or []:
            any_pkg = True
            if r.get("Type") == "python-pkg":
                names.add(re.sub(r"[-_.]+", "-", str(p.get("Name") or "")).lower())
    return names if any_pkg else None


def judge(charts, inventory, reports):
    """(failures, summary). charts: those with ENCRYPT; reports: {image: parsed json}."""
    bad, judged = [], 0
    if not charts:
        return ["no chart sets memcache_security_strategy ENCRYPT, so the rule judged nothing"], ""
    for c in sorted(charts):
        images = sorted(e["image"] for e in inventory if any(u.get("chart") == c and u.get("rendered") != "declared-only"
                                                             for u in e.get("used_by") or []))
        carriers = 0
        for i in images:
            if i not in reports:
                bad.append("%s: no scan report for %s" % (c, i))
                continue
            p = packages(reports[i])
            if p is None:
                bad.append("%s: the report for %s lists no package (is --list-all-pkgs set?)" % (c, i))
                continue
            judged += 1
            if CARRIER in p:
                carriers += 1
                if NEEDS not in p:
                    bad.append("%s sets memcache_security_strategy ENCRYPT, and %s has %s without %s: every request with a "
                               "valid token fails there" % (c, i, CARRIER, NEEDS))
        if not carriers and not any(x.startswith(c + ":") for x in bad):
            bad.append("%s: none of its %d image(s) has %s, so the rule looked at the wrong images" % (c, len(images), CARRIER))
    return bad, "%d chart(s) with ENCRYPT, %d image report(s) judged" % (len(charts), judged)


def self_test():
    def rep(*names, **kw):
        return {"Results": [{"Type": kw.get("type", "python-pkg"), "Packages": [{"Name": n} for n in names]}]}
    inv = [{"image": "i/api", "used_by": [{"chart": "a", "rendered": "defaults"}]},
           {"image": "i/db", "used_by": [{"chart": "a", "rendered": "defaults"}]},
           {"image": "i/sync", "used_by": [{"chart": "a", "rendered": "declared-only"}]}]
    db = rep("mariadb", type="debian")
    cases = [
        ("both packages", judge({"a"}, inv, {"i/api": rep("keystonemiddleware", "cryptography"), "i/db": db}), 0),
        ("0.3.74's masakari", judge({"a"}, inv, {"i/api": rep("keystonemiddleware", "oslo.cache"), "i/db": db}), 1),
        ("a Debian cryptography does not count", judge({"a"}, inv, {"i/api": {"Results": rep("keystonemiddleware")["Results"] + rep("cryptography", type="debian")["Results"]}, "i/db": db}), 1),
        ("no report", judge({"a"}, inv, {"i/db": db}), 1),
        ("a report without packages", judge({"a"}, inv, {"i/api": {"Results": []}, "i/db": db}), 1),
        ("no image carries keystonemiddleware", judge({"a"}, inv, {"i/api": rep("x"), "i/db": db}), 1),
        ("no chart", judge(set(), inv, {}), 1),
        ("names are normalised", judge({"a"}, inv, {"i/api": rep("KeystoneMiddleware", "Cryptography"), "i/db": db}), 0),
        ("encrypts: nested", [[]] if encrypts({"conf": {"a": {"keystone_authtoken": {"memcache_security_strategy": "ENCRYPT"}}}}) else [["x"]], 0),
        ("encrypts: paste filter", [[]] if not encrypts({"conf": {"a": {"keystone_authtoken": {"memcache_security_strategy": "MAC"}}}}) else [["x"]], 0),
    ]
    wrong = [(n, r[0], w) for n, r, w in cases if len(r[0]) != w]
    for n, r, w in wrong:
        print("SELF-TEST FAIL: %s: %d finding(s), want %d: %s" % (n, len(r), w, r))
    print("self-test: %d case(s), %d wrong" % (len(cases), len(wrong)))
    return 1 if wrong else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        print("usage: hack/check-image-packages.py <repo-root> <results-dir>")
        return 2
    root, results = args
    charts = set()
    for p in sorted(glob.glob(os.path.join(root, "blueprints", "*", "chart", "values.yaml"))):
        if encrypts((yaml.safe_load(open(p)) or {}).get("conf")):
            charts.add(p.split(os.sep)[-3])
    inventory = (yaml.safe_load(open(os.path.join(root, "hack", "images.yaml"))) or {}).get("images") or []
    reports = {}
    for p in glob.glob(os.path.join(results, "**", "*.json"), recursive=True):
        d = json.load(open(p))
        reports[d.get("ArtifactName")] = d
    bad, summary = judge(charts, inventory, reports)
    for b in bad:
        print("FAIL: " + b)
    if bad:
        return 1
    print("OK: " + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
