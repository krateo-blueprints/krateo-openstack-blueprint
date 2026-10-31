#!/usr/bin/env python3
"""Resolve every chart and image reference in a repository. Fail if any does not exist.

Four separate outages in this platform were the same defect: a pinned chart or image that
resolved to nothing. Each surfaced 30-45 minutes into a live cluster rebuild, as an opaque
ImagePullBackOff or a composition that reported Ready=True while deploying nothing. Each
would have taken seconds to catch here.

Deliberately regex-driven, not YAML-parsed: most of these files are Helm templates, so they
are not valid YAML, and the references live in printf blocks and doc comments as often as in
values. Matching text finds them all.

Exit 1 if any reference is unresolvable.
"""
import base64, json, os, re, sys, urllib.error, urllib.request

TIMEOUT = 25

# oci://host/path/chart  — the chart's version is looked for nearby (see pair_version)
OCI = re.compile(r'oci://([a-z0-9.-]+)/([a-zA-Z0-9._/-]+)')
# host/path:tag — an image. Requires a dot in the host so `foo/bar:baz` in prose is skipped.
IMG = re.compile(r'\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)/([a-zA-Z0-9._/-]+):([a-zA-Z0-9._-]+)\b')
VERSION_NEAR = re.compile(r'version:\s*"?([0-9]+\.[0-9]+\.[0-9]+[^"\s]*)"?')
# host/path@sha256:...  — a digest-pinned image. IMG cannot match one, because '@' is not in
# its path character class, so without this the most deterministic way to write a reference
# would also be the only way to make it unverified.
DIGEST = re.compile(r'\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)/([a-zA-Z0-9._/-]+)@(sha256:[a-f0-9]{64})\b')

# Anything templated, placeholder, or obviously not a literal.
#   \.\.\.  -- `ghcr.io/.../name:1.2.3` is the usual way docs and comments elide an org.
#             It parses as a perfectly good reference and is never a real one.
SKIP = re.compile(r'\{\{|\$\{|%[sqvd]|CHART_VERSION|VERSION|<[a-zA-Z]|x\.y\.z|\.\.\.')

# A tag that can be rebuilt under the same name. These resolve forever and mean something
# different each time, so a resolution check can never catch one -- see #69, where 49 were
# live while this script reported "all references resolve".
# Applied to the tag of an already-extracted reference, never to raw text: `pipeline:main:`
# and `filter:maintenance:` are PasteDeploy keys in these charts, not image tags.
MUTABLE = re.compile(r'latest', re.I)


def ghcr_token(repo):
    url = "https://%s/token?scope=repository:%s:pull" % ("ghcr.io", repo)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return json.load(r).get("token", "")
    except Exception:
        return ""


def registry_get(host, repo, ref):
    """HEAD-ish GET of a manifest, ANONYMOUSLY. Returns HTTP status.

    401 counts as a failure and that is deliberate: the child's kubelet and core-provider
    pull without credentials, so "exists but needs auth" is indistinguishable from "gone"
    at the point where it matters. A private package fails provisioning exactly like a
    missing one.
    """
    if host == "ghcr.io":
        tok = ghcr_token(repo)
        auth = {"Authorization": "Bearer " + tok} if tok else {}
        base = "https://ghcr.io/v2"
    elif host in ("docker.io", "registry-1.docker.io"):
        # Docker Hub official images ("rabbitmq", "nginx") live under library/. Querying
        # the bare name returns 401, which would flag every official image as broken —
        # the exact cry-wolf failure that gets a check disabled.
        if "/" not in repo:
            repo = "library/" + repo
        try:
            u = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:%s:pull" % repo
            with urllib.request.urlopen(u, timeout=TIMEOUT) as r:
                tok = json.load(r)["token"]
        except Exception:
            tok = ""
        auth = {"Authorization": "Bearer " + tok} if tok else {}
        base = "https://registry-1.docker.io/v2"
    elif host == "quay.io" and not ref.startswith("sha256:"):
        # quay's /v2 manifest endpoint returns 401 for an ABSENT tag as often as 404, which is
        # indistinguishable from "needs auth". Its tag API is authoritative and anonymous, so
        # ask it directly: a tag either is in the active-tag list or it is not.
        #
        # A DIGEST is excluded from this branch deliberately. The tag API answers "is this a
        # tag", and a digest never is, so routing one here reports 404 for an image that
        # exists -- which would make digest pinning, the only immutable option for images
        # with no immutable tag, fail this very check. Verified directly: quay's /v2 endpoint
        # returns 200 for a present digest and a clean 404 for an absent one, so the
        # ambiguity that motivates this branch does not arise for digests. Falls through.
        try:
            u = "https://quay.io/api/v1/repository/%s/tag/?specificTag=%s&onlyActiveTags=true" % (repo, ref)
            with urllib.request.urlopen(u, timeout=TIMEOUT) as r:
                return 200 if json.load(r).get("tags") else 404
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return 0
    else:  # other anonymous-friendly registries
        auth = {}
        base = "https://%s/v2" % host

    hdrs = dict(auth)
    hdrs["Accept"] = ",".join([
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ])
    req = urllib.request.Request("%s/%s/manifests/%s" % (base, repo, ref), headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


NAME_NEAR = re.compile(r'^\s*(?:chart|name):\s*"?([a-z0-9][a-z0-9._-]*)"?\s*$')


def pair_name(lines, idx):
    """Find the chart NAME declared near an oci:// line.

    Several repos pin `repo: oci://host/org/charts` with the chart name in a separate
    `chart:` or `name:` key. Querying the repo root instead of the chart is meaningless —
    it fails for every pin, correct or not, which would make this check pure noise.
    """
    for off in (0, 1, 2, -1, -2, 3, -3):
        j = idx + off
        if 0 <= j < len(lines):
            m = NAME_NEAR.match(lines[j])
            if m:
                return m.group(1)
    return None


def pair_version(lines, idx):
    """Find the chart version declared near an oci:// line.

    Charts are pinned in several shapes across these repos — {url, version},
    {repo, chart, version}, and printf blocks where the two are lines apart — so look in a
    small window both ways rather than assuming one layout.
    """
    for off in (0, 1, 2, -1, -2, 3, -3):
        j = idx + off
        if 0 <= j < len(lines):
            m = VERSION_NEAR.search(lines[j])
            if m and not SKIP.search(m.group(1)):
                return m.group(1)
    return None


REPO_LINE = re.compile(r'^\s*repository:\s*["\']?([a-zA-Z0-9][a-zA-Z0-9._/-]*)["\']?\s*(?:#.*)?$')
TAG_LINE = re.compile(r'^\s*tag:\s*["\']?([a-zA-Z0-9][a-zA-Z0-9._-]*)["\']?\s*(?:#.*)?$')
REG_LINE = re.compile(r'^\s*registry:\s*["\']?([a-zA-Z0-9._-]*)["\']?\s*(?:#.*)?$')


def split_image(lines, idx):
    """Pair a `repository:` line with its neighbouring `tag:` (and optional `registry:`).

    This is the DOMINANT way Helm charts declare images:

        image:
          repository: ghcr.io/org/name
          tag: "1.2.3"

    The single-line matcher never sees these, which is not a corner case — it is most
    charts. It cost a real miss: nutanix-v4-proxy referenced
    ghcr.io/krateo-blueprints/nutanix-v4-proxy, an image that does not exist, and the
    check reported "all references resolve" because it found nothing to check.
    """
    m = REPO_LINE.match(lines[idx])
    if not m:
        return None
    repo = m.group(1)
    if repo.startswith("oci://") or SKIP.search(lines[idx]):
        return None            # chart dependency, handled by the OCI matcher
    tag = registry = None
    for off in (1, 2, 3, -1, -2, -3, 4, -4):
        j = idx + off
        if not (0 <= j < len(lines)):
            continue
        if tag is None:
            t = TAG_LINE.match(lines[j])
            if t and not SKIP.search(t.group(1)):
                tag = t.group(1)
        if registry is None:
            r = REG_LINE.match(lines[j])
            if r is not None:
                registry = r.group(1)
    if not tag:
        return None                     # no resolvable tag -> cannot check, skip
    if registry:
        host, path = registry, repo
    elif "/" in repo and "." in repo.split("/")[0]:
        host, path = repo.split("/", 1)  # repository already carries the host
    else:
        host, path = "docker.io", repo   # bare name -> Docker Hub (library/ added later)
    return host, path, tag


def scan(root):
    charts, images = set(), set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "charts")]
        for fn in filenames:
            if not fn.endswith((".yaml", ".yml", ".tpl", ".json")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                lines = open(p, encoding="utf-8", errors="replace").read().split("\n")
            except Exception:
                continue
            rel = os.path.relpath(p, root)
            for i, line in enumerate(lines):
                for m in OCI.finditer(line):
                    host, path = m.group(1), m.group(2).rstrip('"\'/,')
                    if SKIP.search(m.group(0)):
                        continue
                    ver = pair_version(lines, i)
                    # `oci://host/org/charts` is a repo root, not a chart. Append the
                    # chart name from a neighbouring key when the path clearly lacks one.
                    if path.rsplit("/", 1)[-1] in ("charts", "chart"):
                        nm = pair_name(lines, i)
                        if nm:
                            path = path + "/" + nm
                        else:
                            continue  # repo root with no resolvable chart name — not a ref
                    if ver:
                        charts.add((host, path, ver, rel, i + 1))
                for m in DIGEST.finditer(line):
                    host, path, dig = m.group(1), m.group(2), m.group(3)
                    if host.endswith((".krateo.dev", ".svc")):
                        continue
                    images.add((host, path, dig, rel, i + 1))
                sp = split_image(lines, i)
                if sp:
                    images.add((sp[0], sp[1], sp[2], rel, i + 1))
                for m in IMG.finditer(line):
                    host, path, tag = m.group(1), m.group(2), m.group(3)
                    if SKIP.search(m.group(0)) or host.endswith((".krateo.dev", ".svc")):
                        continue
                    # A web link, not an image. `https://wiki.xen.org/wiki/Bus:Device.Function`
                    # in an OpenAPI description matches host/path:tag exactly, and these repos
                    # vendor tens of thousands of lines of upstream specs full of such links.
                    # No image reference is ever written with a scheme, so this discriminates
                    # precisely -- unlike skipping prose keys, which would also have to guess
                    # at block scalars and would start losing real references.
                    if line[:m.start()].rstrip().endswith("//"):
                        continue
                    if "/" not in path and host.count(".") < 1:
                        continue
                    images.add((host, path, tag, rel, i + 1))
    return charts, images


def main():
    # --mutable-only does the tag-immutability half with no network at all. Resolution needs
    # ~450 registry calls, which is why the full run is weekly (cron) rather than per-PR; but
    # a reintroduced floating tag should fail the PR that adds it, not surface up to a week
    # later, and nothing about immutability requires asking a registry.
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mutable_only = "--mutable-only" in sys.argv[1:]
    root = args[0] if args else "."
    charts, images = scan(root)
    bad = []

    if mutable_only:
        mutable = sorted(
            (host, path, tag, rel, ln)
            for host, path, tag, rel, ln in images
            if MUTABLE.search(tag)
        )
        print("image references scanned (%d), no registry calls made" % len(images))
        if not images:
            # A check that examined nothing must not report that nothing is wrong. Run from
            # the wrong directory, or after a path change the scan no longer matches, this
            # printed "every image tag is immutable" and exited 0 -- green CI over an
            # unexamined tree.
            print()
            print("FAILED MEASUREMENT: no image references found at all. This repository has")
            print("hundreds; finding none means the scan did not look where it meant to, which")
            print("is not the same as finding nothing wrong.")
            return 1
        if mutable:
            print()
            print("MUTABLE TAGS: %d" % len(mutable))
            for host, path, tag, rel, ln in mutable:
                print("  %s/%s:%s   %s:%d" % (host, path, tag, rel, ln))
            print()
            print("A rebuildable tag resolves forever and means something different each time,")
            print("so the chart version stops identifying what it installs. Pin to a tag that")
            print("cannot be rebuilt, or to @sha256: when the registry publishes no immutable")
            print("tag for that image.")
            return 1
        print("every image tag is immutable")
        return 0

    print("chart references (%d)" % len(charts))
    for host, path, ver, rel, ln in sorted(charts):
        code = registry_get(host, path, ver)
        ok = code == 200
        print("  %-4s %s/%s:%s   (%s:%d)" % ("OK" if ok else "FAIL", host, path, ver, rel, ln))
        if not ok:
            bad.append(("chart", "%s/%s:%s" % (host, path, ver), rel, ln, code))

    print()
    print("image references (%d)" % len(images))
    for host, path, tag, rel, ln in sorted(images):
        code = registry_get(host, path, tag)
        ok = code == 200
        print("  %-4s %s/%s:%s   (%s:%d)" % ("OK" if ok else "FAIL", host, path, tag, rel, ln))
        if not ok:
            bad.append(("image", "%s/%s:%s" % (host, path, tag), rel, ln, code))

    mutable = sorted(
        (host, path, tag, rel, ln)
        for host, path, tag, rel, ln in images
        if MUTABLE.search(tag)
    )
    if mutable:
        print()
        print("MUTABLE TAGS: %d" % len(mutable))
        for host, path, tag, rel, ln in mutable:
            print("  %s/%s:%s   %s:%d" % (host, path, tag, rel, ln))
        print()
        print("These resolve today and may resolve to different bytes tomorrow, so the chart")
        print("version stops identifying what it installs. Pin to a tag that cannot be rebuilt,")
        print("or to @sha256: when the registry publishes no immutable tag for that image.")

    if bad:
        print()
        print("UNRESOLVABLE REFERENCES: %d" % len(bad))
        for kind, ref, rel, ln, code in bad:
            print("  %-6s %s  -> HTTP %s   %s:%d" % (kind, ref, code, rel, ln))
        print()
        print("A reference that does not resolve here fails at provisioning time instead,")
        print("as an ImagePullBackOff or a composition that reports Ready=True and deploys nothing.")

    print()
    if bad or mutable:
        return 1
    if not charts and not images:
        print("FAILED MEASUREMENT: no references found at all -- the scan did not look where")
        print("it meant to. A clean result from an empty scan is indistinguishable from a")
        print("clean result from a clean tree, and only one of them is true here.")
        return 1
    print("all references resolve, and every tag is immutable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
