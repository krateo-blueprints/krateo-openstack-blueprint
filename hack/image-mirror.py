#!/usr/bin/env python3
"""Mirror images that cannot be pinned where they are published into ghcr.io/krateo-blueprints (#316, #334).

    hack/image-mirror.py [repo-root] --check    offline: hack/image-mirror.yaml agrees with hack/images.yaml (lint.yaml)
    hack/image-mirror.py [repo-root] --apply    copy each entry with crane, then verify it (image-mirror.yaml, on main)
    hack/image-mirror.py --self-test            the --check rules against built-in cases

--check, for every entry in hack/image-mirror.yaml:
  * the target is under ghcr.io/krateo-blueprints/images/ and its tag is dated (...-YYYYMMDD), so it names one build;
  * no two entries share a target;
  * the digest is the one hack/images.yaml recorded for the image the chart runs, the source while the chart still
    points upstream, or the target once it points at the mirror. A digest that matches neither would mirror a build
    image-scan never measured. If the chart runs neither, the entry is stale and fails.

--apply, for every entry: if the target tag exists it must already serve the digest (a mirror tag is never moved);
otherwise `crane copy <source>@<digest> <target>`, which copies the manifest bytes and so keeps the digest. Then the
target must serve the digest, and serve it ANONYMOUSLY: the kubelet pulls without credentials, and a package GHCR
created as private is as unpullable as a missing one. A new package must be made public once, by an org owner.
"""
import importlib.util
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")
    sys.exit(2)

TARGET = re.compile(r"^ghcr\.io/krateo-blueprints/images/[a-z0-9][a-z0-9._-]*:[A-Za-z0-9._-]*[-_]20[0-9]{6}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def check(mirrors, recorded):
    """Failures for the --check rules. `recorded` maps every image the charts run to its recorded digest."""
    bad = []
    if not mirrors:
        return bad
    seen = {}
    for i, m in enumerate(mirrors):
        where = "hack/image-mirror.yaml entry %d (%s)" % (i + 1, (m or {}).get("target"))
        missing = [k for k in ("source", "digest", "target", "issue") if not str((m or {}).get(k) or "").strip()]
        if missing:
            bad.append("%s: missing %s" % (where, ", ".join(missing)))
            continue
        if not TARGET.match(m["target"]):
            bad.append("%s: target must be ghcr.io/krateo-blueprints/images/<name>:<tag ending -YYYYMMDD>" % where)
        if not DIGEST.match(m["digest"]):
            bad.append("%s: digest %r is not sha256:<64 hex>" % (where, m["digest"]))
        if m["target"] in seen:
            bad.append("%s: target already used by entry %d" % (where, seen[m["target"]]))
        seen[m["target"]] = i + 1
        runs = [img for img in (m["target"], m["source"]) if img in recorded]
        if not runs:
            bad.append("%s: the charts run neither %s nor %s; remove the entry" % (where, m["source"], m["target"]))
        elif recorded[runs[0]] != m["digest"]:
            bad.append("%s: digest %s is not the one hack/images.yaml recorded for %s (%s), so it is not the build "
                       "image-scan measured" % (where, m["digest"], runs[0], recorded[runs[0]]))
    return bad


def load(root):
    mirrors = (yaml.safe_load(open(os.path.join(root, "hack", "image-mirror.yaml"))) or {}).get("mirrors") or []
    inv = yaml.safe_load(open(os.path.join(root, "hack", "images.yaml"))) or {}
    recorded = {e["image"]: (e.get("registry_facts") or {}).get("digest") for e in inv.get("images") or []}
    return mirrors, recorded


def crane(*args):
    r = subprocess.run(["crane"] + list(args), capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def anonymous_digest(root, ref):
    """The digest the registry serves for `ref` to a client with no credentials, or None with the HTTP status."""
    spec = importlib.util.spec_from_file_location("preflight_refs", os.path.join(root, "hack", "preflight-refs.py"))
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    import urllib.error
    import urllib.request
    host, rest = ref.split("/", 1)
    repo, tag = rest.rsplit(":", 1)
    base, path, auth = pf.v2_endpoint(host, repo)
    hdrs = dict(auth)
    hdrs["Accept"] = ",".join(pf.V2_MANIFEST)
    req = urllib.request.Request("%s/%s/manifests/%s" % (base, path, tag), headers=hdrs, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=pf.TIMEOUT) as r:
            return r.headers.get("Docker-Content-Digest"), r.status
    except urllib.error.HTTPError as e:
        return None, e.code


def apply(root, mirrors):
    bad = []
    for m in mirrors:
        src, dig, tgt = m["source"], m["digest"], m["target"]
        rc, have, _ = crane("digest", tgt)
        if rc == 0 and have != dig:
            bad.append("%s already serves %s, not %s: a mirror tag is never moved; give the new build a new tag"
                       % (tgt, have, dig))
            continue
        if rc != 0:
            rc, out, err = crane("copy", "%s@%s" % (src.rsplit(":", 1)[0], dig), tgt)
            if rc != 0:
                bad.append("crane copy %s@%s -> %s failed: %s" % (src, dig, tgt, err or out))
                continue
            print("copied %s@%s -> %s" % (src, dig, tgt))
        else:
            print("present %s (%s)" % (tgt, dig))
        rc, have, err = crane("digest", tgt)
        if rc != 0 or have != dig:
            bad.append("%s serves %s after the copy, want %s %s" % (tgt, have, dig, err))
            continue
        anon, status = anonymous_digest(root, tgt)
        if anon != dig:
            bad.append("%s is not pullable anonymously (HTTP %s, digest %s): an org owner must make the GHCR package "
                       "public once (package settings, Change visibility)" % (tgt, status, anon))
    return bad


def self_test():
    D1, D2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    S = "quay.io/x/a:2026.1-ubuntu-noble"
    T = "ghcr.io/krateo-blueprints/images/a:2026.1-ubuntu-noble-20261117"

    def m(**kw):
        e = {"source": S, "digest": D1, "target": T, "issue": 1}
        e.update(kw)
        return e

    cases = [
        ("source run, digest recorded", [m()], {S: D1}, 0),
        ("target run, digest recorded", [m()], {T: D1}, 0),
        ("digest not the recorded one", [m(digest=D2)], {S: D1}, 1),
        ("chart runs neither (stale)", [m()], {"quay.io/x/b:1.0.0": D1}, 1),
        ("target outside the org", [m(target="ghcr.io/someone/a:x-20261117")], {S: D1}, 1),
        ("target tag not dated", [m(target="ghcr.io/krateo-blueprints/images/a:2026.1-ubuntu-noble")], {S: D1}, 1),
        ("digest malformed", [m(digest="sha256:abc")], {S: "sha256:abc"}, 1),
        ("two entries, one target", [m(), m(source="quay.io/x/b:2026.1-ubuntu-noble")],
         {S: D1, "quay.io/x/b:2026.1-ubuntu-noble": D1}, 1),
        ("missing issue", [m(issue="")], {S: D1}, 1),
        ("empty list", [], {S: D1}, 0),
    ]
    fails = []
    for name, mirrors, recorded, want in cases:
        got = check(mirrors, recorded)
        if len(got) != want:
            fails.append("%s: %d failure(s), want %d: %s" % (name, len(got), want, got))
    for f in fails:
        print("FAIL: self-test " + f)
    if fails:
        sys.exit(1)
    print("OK: %d mirror-list cases behave as specified" % len(cases))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--self-test" in sys.argv:
        self_test()
        return
    root = os.path.abspath(args[0] if args else ".")
    mirrors, recorded = load(root)
    if not recorded:
        print("FAILED MEASUREMENT: hack/images.yaml lists no image")
        sys.exit(2)
    bad = check(mirrors, recorded)
    if not bad and "--apply" in sys.argv:
        bad = apply(root, mirrors)
    for b in bad:
        print("FAIL: " + b)
    if bad:
        sys.exit(1)
    print("OK: %d mirror entr%s consistent with hack/images.yaml%s" % (
        len(mirrors), "y" if len(mirrors) == 1 else "ies", ", copied and pullable anonymously" if "--apply" in sys.argv else ""))


main()
