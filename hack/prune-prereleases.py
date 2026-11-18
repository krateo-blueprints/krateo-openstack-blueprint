#!/usr/bin/env python3
"""Delete old pre-release versions of every chart this repository publishes from GHCR, keeping the
newest N pre-releases. A release (plain X.Y.Z) is never deleted.

Why. prerelease.yaml pushes every chart (about 39) at X.Y.Z-main.g<sha7> for each commit verified on
GKE, and nothing else removes them. Releases are immutable and installed by explicit version, so
they stay forever. Pre-releases are needed only until their GKE pair has run.

Which versions. A package version is DELETABLE only when it has at least one tag, every tag has
the pre-release shape (`hack/release-version.sh check prerelease`, the one definition of that
shape), which a release tag never has. An untagged version, a version tagged with anything else,
and a version that ALSO carries a release tag are never touched. `delete_version` re-checks this
itself, immediately before the DELETE, so no planning bug can delete a release.
hack/validate-prerelease-channel.py tests that table and mutates this file to prove it.

Which pre-releases are kept. The N newest pre-release VERSIONS, counted across all packages (the
newest push of any chart at that version dates it). Every package keeps the same N, so a kept
pre-release is still installable as a whole.

Usage: hack/prune-prereleases.py --keep N [--delete]
  Without --delete it prints the plan and changes nothing (the dry run).
Environment: PRUNE_TOKEN, a token with read:packages and delete:packages on the owner's packages;
GITHUB_REPOSITORY_OWNER (the package owner, an organisation).
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_SH = os.path.join(ROOT, "hack", "release-version.sh")
API = "https://api.github.com"


def shape(channel, tag):
    """True when TAG has the CHANNEL's shape, as hack/release-version.sh defines it."""
    return subprocess.run(["bash", VERSION_SH, "check", channel, tag],
                          capture_output=True).returncode == 0


def deletable(tags):
    """A version may be deleted only if it is tagged, and only with pre-release tags. A release tag
    fails the pre-release shape, so a version carrying one is never deletable."""
    if not tags:
        return False
    return all(shape("prerelease", t) for t in tags)


def chart_names(root=ROOT):
    """Every chart this repository publishes: the Chart.yaml files hack/publish-charts.sh packages
    (all of them outside a vendored charts/ directory)."""
    names = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("charts", ".git", ".claude", "node_modules")]
        if "Chart.yaml" in filenames:
            for line in open(os.path.join(dirpath, "Chart.yaml")):
                if line.startswith("name:"):
                    names.add(line.split(":", 1)[1].strip())
                    break
    return sorted(names)


class GitHub:
    def __init__(self, token, owner):
        self.token, self.owner = token, owner

    def _req(self, method, url):
        req = urllib.request.Request(url, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return json.loads(body) if body else None, r.headers.get("Link", "")

    def _pkg(self, name):
        return f"{API}/orgs/{self.owner}/packages/container/{urllib.parse.quote('charts/' + name, safe='')}"

    def versions(self, name):
        """Every version of charts/<name>, or None when the package does not exist."""
        out, url = [], self._pkg(name) + "/versions?per_page=100"
        while url:
            try:
                page, link = self._req("GET", url)
            except urllib.error.HTTPError as e:
                if e.code == 404 and not out:
                    return None
                raise
            out += page
            url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part[part.index("<") + 1:part.index(">")]
        return out

    def delete(self, name, version_id):
        self._req("DELETE", f"{self._pkg(name)}/versions/{version_id}")


def tags_of(v):
    return list(((v.get("metadata") or {}).get("container") or {}).get("tags") or [])


def plan(packages, keep):
    """packages: {name: [version, ...]}. Returns (kept pre-release tags, [(name, version), ...] to
    delete). Only deletable versions are ever candidates."""
    newest = {}
    for name, versions in packages.items():
        for v in versions:
            if deletable(tags_of(v)):
                for t in tags_of(v):
                    newest[t] = max(newest.get(t, ""), v.get("created_at") or "")
    kept = set(sorted(newest, key=lambda t: (newest[t], t), reverse=True)[:keep])
    doomed = []
    for name, versions in sorted(packages.items()):
        for v in versions:
            tags = tags_of(v)
            if deletable(tags) and not (set(tags) & kept):
                doomed.append((name, v))
    return kept, doomed


def delete_version(gh, name, v):
    """The only path to a DELETE. Refuses anything that is not deletable, whatever the plan said."""
    tags = tags_of(v)
    if not deletable(tags):
        raise RuntimeError(f"refusing to delete charts/{name} version {v.get('id')} tagged {tags}: "
                           "not a pre-release-only version")
    gh.delete(name, v["id"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, required=True)
    ap.add_argument("--delete", action="store_true", help="delete; without it, only print the plan")
    a = ap.parse_args()
    if a.keep < 1:
        sys.exit("--keep must be at least 1: the newest pre-release may be the one on GKE right now")
    token = os.environ.get("PRUNE_TOKEN", "")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if not token or not owner:
        sys.exit("PRUNE_TOKEN and GITHUB_REPOSITORY_OWNER are required")
    gh = GitHub(token, owner.lower())
    names = chart_names()
    if not names:
        sys.exit(f"no Chart.yaml under {ROOT}: nothing to prune is not the same as nothing published")
    packages, missing = {}, []
    for n in names:
        vs = gh.versions(n)
        if vs is None:
            missing.append(n)
        else:
            packages[n] = vs
    if not packages:
        sys.exit("none of this repository's charts exists as a package; check the owner and the token")
    kept, doomed = plan(packages, a.keep)
    total = sum(len(v) for v in packages.values())
    print(f"{len(packages)} package(s), {total} version(s); keeping pre-releases: "
          f"{', '.join(sorted(kept)) or 'none'}")
    if missing:
        print(f"not in GHCR (never published, skipped): {', '.join(missing)}")
    for name, v in doomed:
        print(f"  {'delete' if a.delete else 'would delete'} charts/{name} {', '.join(tags_of(v))} "
              f"(id {v['id']}, {v.get('created_at')})")
    if not a.delete:
        print(f"DRY RUN: {len(doomed)} version(s) would be deleted; nothing was changed")
        return 0
    for name, v in doomed:
        delete_version(gh, name, v)
    print(f"deleted {len(doomed)} pre-release version(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
