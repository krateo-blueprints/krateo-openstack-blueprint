#!/usr/bin/env python3
"""No image the charts run carries a CRITICAL vulnerability without an exception that is still in date (#316).

    hack/check-image-cves.py [repo-root] --matrix          the images to scan, as JSON, for the workflow's matrix
    hack/check-image-cves.py [repo-root] <results-dir>     judge Trivy's JSON reports against the allowlist
    hack/check-image-cves.py [repo-root] --self-test       the judging rules, against built-in cases (offline)
        --today YYYY-MM-DD   the date expiries are judged against (default: today, UTC)

The images are hack/images.yaml's, the ones its registry measurement found pullable anonymously: an image no one can
pull cannot be scanned, and the inventory gate already reports it. --matrix prints that list, so the workflow scans
exactly what this judges and the two cannot drift apart.

Judging, in <results-dir> (one Trivy `--format json` report per image, any file name, found recursively):

  * Every image to scan must have exactly one report whose ArtifactName is that image. A scan job that died, an
    artifact that did not upload, a report for some other image: each would otherwise leave an image unjudged and
    the run green.
  * Every CRITICAL vulnerability must be covered by an entry in hack/image-cve-allowlist.yaml with the same `id`,
    `image` and `package`, whose `expires` is today or later. The key is all three because each alone is too wide:
    an id alone would excuse that CVE in every image, and an image alone every CVE in it.
  * Every entry must carry a `reason` and an `expires` no more than MAX_DAYS ahead, so an exception is a dated
    decision that comes back, never a permanent mute.
  * An entry that covers no finding fails: the image was fixed or replaced, and an exception left behind would
    silently cover the same CVE if it came back.
"""
import datetime
import glob
import json
import os
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was judged")
    sys.exit(2)

MAX_DAYS = 120
SEVERITY = "CRITICAL"
REQUIRED = ("id", "image", "package", "reason", "expires")


def to_scan(inventory):
    return sorted(e["image"] for e in inventory.get("images") or []
                  if (e.get("registry_facts") or {}).get("anonymous_pull") is True)


def parse_date(v):
    if isinstance(v, datetime.date):
        return v
    try:
        return datetime.date.fromisoformat(str(v))
    except ValueError:
        return None


def judge(images, reports, allowlist, today):
    """(failures, notes). `reports` is [(path, parsed json)]; `allowlist` the parsed allowlist file."""
    failures, notes = [], []
    if not images:
        failures.append("hack/images.yaml lists no pullable image, so nothing would be scanned")
    by_image = {}
    for path, rep in reports:
        name = (rep or {}).get("ArtifactName")
        by_image.setdefault(name, []).append((path, rep))
    findings = []  # (image, id, package, installed, fixed, status)
    for img in images:
        got = by_image.get(img) or []
        if len(got) != 1:
            failures.append("%s: %d scan report(s), want exactly 1; an image without one was not judged"
                            % (img, len(got)))
            continue
        for res in got[0][1].get("Results") or []:
            for v in res.get("Vulnerabilities") or []:
                if v.get("Severity") == SEVERITY:
                    findings.append((img, v.get("VulnerabilityID"), v.get("PkgName"), v.get("InstalledVersion"),
                                     v.get("FixedVersion") or "", v.get("Status") or ""))
    for name in sorted(n for n in by_image if n not in images):
        notes.append("a report for %r, which is not an image to scan; ignored" % name)

    entries = (allowlist or {}).get("entries") or []
    valid = []
    for i, e in enumerate(entries):
        where = "hack/image-cve-allowlist.yaml entry %d (%s)" % (i + 1, (e or {}).get("id"))
        missing = [k for k in REQUIRED if not str((e or {}).get(k) or "").strip()]
        if missing:
            failures.append("%s: missing %s" % (where, ", ".join(missing)))
            continue
        exp = parse_date(e["expires"])
        if exp is None:
            failures.append("%s: expires %r is not a YYYY-MM-DD date" % (where, e["expires"]))
            continue
        if exp > today + datetime.timedelta(days=MAX_DAYS):
            failures.append("%s: expires %s, more than %d days after %s; an exception must come back for review"
                            % (where, exp, MAX_DAYS, today))
            continue
        valid.append((e, exp, where))

    used = set()
    uncovered = []
    for f in findings:
        img, vid, pkg = f[0], f[1], f[2]
        match = [(e, exp, w) for e, exp, w in valid if (e["id"], e["image"], e["package"]) == (vid, img, pkg)]
        live = [m for m in match if m[1] >= today]
        for m in match:
            used.add(id(m[0]))
        if live:
            continue
        uncovered.append(f + (("expired %s" % max(m[1] for m in match)) if match else "",))
    for f in sorted(uncovered):
        img, vid, pkg, inst, fixed, status, expired = f
        failures.append("%s: %s %s in %s %s (fixed in: %s; status: %s)%s" % (
            img, SEVERITY, vid, pkg, inst, fixed or "none", status or "?",
            "; its exception " + expired if expired else "; no exception"))
    for e, exp, where in valid:
        if id(e) not in used:
            failures.append("%s: covers no %s finding for %s; remove it" % (where, SEVERITY, e["image"]))
    return failures, notes, len(findings)


def load_reports(d):
    out = []
    for p in sorted(glob.glob(os.path.join(d, "**", "*.json"), recursive=True)):
        try:
            out.append((p, json.load(open(p))))
        except ValueError as err:
            out.append((p, {"ArtifactName": None, "error": str(err)}))
    return out


def self_test():
    """Each rule, and the cases each one exists for, against built-in reports. Offline."""
    today = datetime.date(2026, 11, 17)
    A, B = "quay.io/x/a:1", "quay.io/x/b:1"

    def rep(img, *vulns):
        return ("r", {"ArtifactName": img, "Results": [{"Target": img, "Vulnerabilities": [
            {"VulnerabilityID": v, "PkgName": p, "InstalledVersion": "1", "Severity": s} for v, p, s in vulns]}]})

    def entry(vid="CVE-1", img=A, pkg="libx", reason="measured", expires="2027-01-01"):
        return {"id": vid, "image": img, "package": pkg, "reason": reason, "expires": expires}

    cases = [
        ("clean", [A], [rep(A)], [], 0),
        ("HIGH is not judged", [A], [rep(A, ("CVE-1", "libx", "HIGH"))], [], 0),
        ("uncovered critical", [A], [rep(A, ("CVE-1", "libx", "CRITICAL"))], [], 1),
        ("covered critical", [A], [rep(A, ("CVE-1", "libx", "CRITICAL"))], [entry()], 0),
        ("covers today", [A], [rep(A, ("CVE-1", "libx", "CRITICAL"))], [entry(expires="2026-11-17")], 0),
        ("expired yesterday", [A], [rep(A, ("CVE-1", "libx", "CRITICAL"))], [entry(expires="2026-11-16")], 1),
        ("other image's entry", [A, B], [rep(A, ("CVE-1", "libx", "CRITICAL")), rep(B)], [entry(img=B)], 2),
        ("other package's entry", [A], [rep(A, ("CVE-1", "libx", "CRITICAL"))], [entry(pkg="liby")], 2),
        ("stale entry", [A], [rep(A)], [entry()], 1),
        ("no reason", [A], [rep(A, ("CVE-1", "libx", "CRITICAL"))], [entry(reason=" ")], 2),
        ("expiry too far", [A], [rep(A, ("CVE-1", "libx", "CRITICAL"))], [entry(expires="2027-06-01")], 2),
        ("expiry not a date", [A], [rep(A, ("CVE-1", "libx", "CRITICAL"))], [entry(expires="soon")], 2),
        ("missing report", [A, B], [rep(A)], [], 1),
        ("report for another image", [A], [rep(B)], [], 1),
        ("two reports", [A], [rep(A), rep(A)], [], 1),
        ("nothing to scan", [], [], [], 1),
    ]
    bad = []
    for name, images, reports, entries, want in cases:
        failures, _, _ = judge(images, reports, {"entries": entries}, today)
        if len(failures) != want:
            bad.append("%s: %d failure(s), want %d: %s" % (name, len(failures), want, failures))
    # load_reports reads nested files, and an unparseable one is a report for no image, not a crash.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "scan-a"))
        json.dump(rep(A)[1], open(os.path.join(d, "scan-a", "trivy.json"), "w"))
        open(os.path.join(d, "broken.json"), "w").write("{")
        failures, _, _ = judge([A], load_reports(d), {}, today)
        if failures:
            bad.append("nested report not found, or a broken one counted: %s" % failures)
    for b in bad:
        print("FAIL: self-test " + b)
    if bad:
        sys.exit(1)
    print("OK: %d judging cases behave as specified" % (len(cases) + 1))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = sys.argv[1:]
    today = datetime.datetime.now(datetime.timezone.utc).date()
    if "--today" in flags:
        today = datetime.date.fromisoformat(flags[flags.index("--today") + 1])
        args.remove(flags[flags.index("--today") + 1])
    if "--self-test" in flags:
        self_test()
        return
    root = os.path.abspath(args[0] if args else ".")
    inventory = yaml.safe_load(open(os.path.join(root, "hack", "images.yaml"))) or {}
    images = to_scan(inventory)
    if "--matrix" in flags:
        if not images:
            print("FAILED MEASUREMENT: hack/images.yaml lists no pullable image", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(images))
        return
    if len(args) < 2:
        sys.exit("usage: check-image-cves.py <repo-root> <results-dir> | --matrix | --self-test")
    reports = load_reports(args[1])
    allowlist = yaml.safe_load(open(os.path.join(root, "hack", "image-cve-allowlist.yaml"))) or {}
    failures, notes, n = judge(images, reports, allowlist, today)
    for m in notes:
        print("NOTE: " + m)
    if failures:
        for f in failures:
            print("FAIL: " + f)
        print("%d image(s) judged, %d %s finding(s), %d failure(s), as of %s"
              % (len(images), n, SEVERITY, len(failures), today))
        sys.exit(1)
    print("OK: %d image(s) scanned; every %s finding (%d) is covered by an exception in date as of %s"
          % (len(images), SEVERITY, n, today))


main()
