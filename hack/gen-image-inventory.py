#!/usr/bin/env python3
"""The inventory of every container image a chart in this repository can run (#316).

    hack/gen-image-inventory.py [repo-root]                    check, offline (per pull request)
    hack/gen-image-inventory.py [repo-root] --resolve          check, and re-ask every registry
    hack/gen-image-inventory.py [repo-root] --write            regenerate the two files
    hack/gen-image-inventory.py [repo-root] --write --resolve  regenerate, re-asking every registry
        --max-load N   wait before each render until the 1-minute load average is below N

Writes two files, both generated, both checked:

  hack/images.yaml   the machine-readable inventory; .github/workflows/image-scan.yaml scans what it lists
  docs/images.md     the same facts as a table, with each image's publisher and why any image is unpullable

WHAT COUNTS AS "AN IMAGE A CHART CAN RUN". Every published chart (blueprints/*/chart and the per-cluster
capabilities chart) is rendered twice: at its defaults, and with every `manifests.<name>` it ships turned off turned
on (each one alone when all of them together do not render, e.g. two backends that exclude each other). Every
container, init container and ephemeral container image those renders contain is recorded, with the condition that
renders it. Then each chart's `images.tags` is added: an image a chart declares but neither render reaches (an
image_repo_sync job behind images.local_registry.active, a rally test) is still one an override can start, so it is
recorded as `declared-only`. The union is the population; a render alone would miss the declared ones, and the
declarations alone would miss an image a template writes literally.

TWO KINDS OF FACT, KEPT APART ON PURPOSE.

  * What the charts reference is derivable offline from this tree, so the default mode re-derives it and fails on any
    difference from hack/images.yaml. A pull request that changes an image fails until the inventory is regenerated,
    and the regenerated inventory is what the image scan reads, so a changed image is scanned on that pull request.
  * What a registry says about an image (does it answer anonymously, in a manifest format docker can pull, for
    linux/amd64; its digest) is not in this tree. It is recorded under `registry`, with the date it was observed, and
    re-measured only with --resolve: on the weekly image-scan run, and when --write meets an image it has no record of.
    A pull request is never failed by a registry being slow. --resolve fails when pullability differs from the record,
    and REPORTS a moved digest without failing: the series tags these charts use today (2026.1-ubuntu_noble) are
    rebuilt upstream, so a moved digest is the expected state until the pinning decision in docs/supply-chain.md.

"Pullable anonymously" means: with no credentials, as the kubelet pulls here, the registry serves the manifest in a
type a current containerd accepts (not Docker Registry v1, #115), an index has a linux/amd64 entry, that entry's
manifest resolves, and its config blob downloads. Layers are not fetched; that would be a pull.

Every image's registry namespace must have a publisher in hack/image-sources.yaml, and every entry there must match an
image: where an image comes from is part of the inventory, so a new source is a decision someone writes down.

Fails when helm or PyYAML is missing, when no chart renders, and when no image is found.
"""
import datetime
import difflib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:
    print("FAILED MEASUREMENT: PyYAML unavailable, so nothing was rendered or compared")
    sys.exit(2)

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
FLAGS = [a for a in sys.argv[1:] if a.startswith("--")]
WRITE = "--write" in FLAGS
RESOLVE = "--resolve" in FLAGS
MAX_LOAD = None
for i, a in enumerate(sys.argv[1:]):
    if a == "--max-load":
        MAX_LOAD = float(sys.argv[i + 2])
        ARGS.remove(sys.argv[i + 2])
ROOT = os.path.abspath(ARGS[0] if ARGS else ".")
BP = os.path.join(ROOT, "blueprints")
TOOLKIT = os.path.join(ROOT, "charts", "helm-toolkit")
INVENTORY = os.path.join(ROOT, "hack", "images.yaml")
SOURCES = os.path.join(ROOT, "hack", "image-sources.yaml")
DOC = os.path.join(ROOT, "docs", "images.md")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# One source of truth for each half that already exists: how a rendered manifest is read for images is the kind
# pre-loader's (tools/collect-images.py), and how a registry is asked anonymously, and which manifest types are
# pullable, is preflight-refs' (hack/preflight-refs.py). A second copy of either could disagree with the first.
COLLECT = load_module("collect_images", os.path.join(ROOT, "tools", "collect-images.py"))
PREFLIGHT = load_module("preflight_refs", os.path.join(ROOT, "hack", "preflight-refs.py"))

HEADER = """\
# GENERATED by hack/gen-image-inventory.py -- do not edit. Regenerate with:
#   python3 hack/gen-image-inventory.py . --write
# Every image a published chart can run (#316). `used_by[].rendered` is the condition that renders it:
#   defaults            the chart's own defaults render it
#   every-manifest-on   only a render with the chart's off-by-default manifests turned on does
#   declared-only       the chart declares it in images.tags and no render above reaches it
# `registry_facts` is measured, not derived from this tree: refreshed by --resolve, dated by `observed`.
"""


# ---------------------------------------------------------------------------------------------------------- render
def wait_for_load():
    if MAX_LOAD is None:
        return
    while os.getloadavg()[0] >= MAX_LOAD:
        time.sleep(15)


def published_charts():
    """(name, dir) of every chart a release publishes: each blueprint's chart, and the capabilities chart."""
    out = []
    for d in sorted(os.listdir(BP)):
        c = os.path.join(BP, d, "chart")
        if os.path.isfile(os.path.join(c, "Chart.yaml")):
            out.append((d, c))
    cap = os.path.join(BP, "openstack", "capabilities")
    if os.path.isfile(os.path.join(cap, "Chart.yaml")):
        out.append(("openstack-capabilities", cap))
    return out


def stage(chart_dir, tmp, name):
    w = os.path.join(tmp, name)
    shutil.copytree(chart_dir, w)
    cy = os.path.join(w, "Chart.yaml")
    txt = open(cy).read()
    open(cy, "w").write(txt.replace("CHART_VERSION", "0.0.0-inventory"))
    if "helm-toolkit" in txt:
        os.makedirs(os.path.join(w, "charts"), exist_ok=True)
        shutil.copytree(TOOLKIT, os.path.join(w, "charts", "helm-toolkit"))
    return w


def render(chart, values):
    wait_for_load()
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        vf = f.name
    try:
        r = subprocess.run(["helm", "template", "inventory", chart, "--namespace", "openstack",
                            "--kube-version", "1.36.0", "-f", vf], capture_output=True, text=True)
    finally:
        os.unlink(vf)
    if r.returncode != 0:
        return None, r.stderr.strip()
    found = set()
    for doc in yaml.safe_load_all(r.stdout):
        COLLECT.walk(doc, found)
    return found, ""


def off_manifests(values):
    m = (values or {}).get("manifests") or {}
    return sorted(k for k, v in m.items() if v is False)


def derive():
    """{image: {chart: {"rendered": condition, "keys": [...]}}}, the charts that run no image, and render errors."""
    if shutil.which("helm") is None:
        print("FAILED MEASUREMENT: helm not on PATH, so nothing was rendered")
        sys.exit(2)
    rank = {"defaults": 0, "every-manifest-on": 1, "declared-only": 2}
    usage, errors, renders = {}, [], 0

    def note(image, chart, cond, key=None):
        u = usage.setdefault(image, {}).setdefault(chart, {"rendered": cond, "keys": set()})
        if rank[cond] < rank[u["rendered"]]:
            u["rendered"] = cond
        if key:
            u["keys"].add(key)

    with tempfile.TemporaryDirectory() as tmp:
        for name, cdir in published_charts():
            values = yaml.safe_load(open(os.path.join(cdir, "values.yaml"))) or {}
            w = stage(cdir, tmp, name)
            found, err = render(w, {})
            if found is None:
                errors.append("%s does not render at its defaults: %s" % (name, err[:300]))
                continue
            renders += 1
            for img in found:
                note(img, name, "defaults")
            toggles = off_manifests(values)
            if toggles:
                found, _ = render(w, {"manifests": {k: True for k in toggles}})
                sets = [found] if found is not None else []
                if found is None:
                    # All together do not render (a backend another one excludes, values only an operator can
                    # supply). Each alone, so one incompatible pair costs only the pair. A manifest that renders
                    # under no setting here is still covered by its images.tags declaration below.
                    for k in toggles:
                        one, _ = render(w, {"manifests": {k: True}})
                        if one is not None:
                            sets.append(one)
                renders += len(sets)
                for s in sets:
                    for img in s:
                        note(img, name, "every-manifest-on")
            tags = (values.get("images") or {}).get("tags") or {}
            for key, img in sorted(tags.items()):
                if isinstance(img, str) and img.strip():
                    note(img.strip(), name, "declared-only", key)
            if not any(name in u for u in usage.values()):
                usage.setdefault(None, {})[name] = None
    if renders == 0:
        print("FAILED MEASUREMENT: no chart rendered")
        for e in errors:
            print("  " + e)
        sys.exit(2)
    without = sorted((usage.pop(None, None) or {}).keys())
    if not usage:
        print("FAILED MEASUREMENT: %d render(s) and no image found; the reader did not look where it meant to" % renders)
        sys.exit(2)
    return usage, without, errors, renders


def split_ref(ref):
    """registry, repository (normalised: Docker Hub official images under library/), tag, digest."""
    name, digest = (ref.split("@", 1) + [None])[:2]
    tag = None
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name.rsplit(":", 1)
    first = name.split("/", 1)[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        host, repo = name.split("/", 1)
    else:
        host, repo = "docker.io", name
    if host in ("docker.io", "registry-1.docker.io", "index.docker.io") and "/" not in repo:
        repo = "library/" + repo
    if host in ("registry-1.docker.io", "index.docker.io"):
        host = "docker.io"
    return host, repo, tag, digest


def load_sources():
    data = yaml.safe_load(open(SOURCES)) or {}
    return data.get("sources") or []


def source_of(image, sources):
    host, repo, _, _ = split_ref(image)
    full = host + "/" + repo
    best = None
    for s in sources:
        p = s["prefix"]
        if full.startswith(p) and (best is None or len(p) > len(best["prefix"])):
            best = s
    return best


def offline_entries(usage):
    entries = []
    for image in sorted(usage):
        host, repo, tag, digest = split_ref(image)
        e = {"image": image, "registry": host, "repository": repo}
        if tag:
            e["tag"] = tag
        if digest:
            e["pinned_digest"] = digest
        e["used_by"] = []
        for chart in sorted(usage[image]):
            u = usage[image][chart]
            row = {"chart": chart, "rendered": u["rendered"]}
            if u["keys"]:
                row["keys"] = sorted(u["keys"])
            e["used_by"].append(row)
        entries.append(e)
    return entries


# ---------------------------------------------------------------------------------------------------------- registry
class MeasurementError(Exception):
    """The registry could not be asked. Never a verdict about the image."""


class _DropAuthAcrossHosts(urllib.request.HTTPRedirectHandler):
    """Blob downloads redirect to a CDN or object store, which rejects a bearer token meant for the registry."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


OPENER = urllib.request.build_opener(_DropAuthAcrossHosts)


def fetch(url, headers):
    """(status, headers, body). An HTTP status is an answer; a network failure after retries is no answer."""
    last = None
    for attempt in range(4):
        try:
            with OPENER.open(urllib.request.Request(url, headers=headers), timeout=PREFLIGHT.TIMEOUT) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last = "HTTP %d" % e.code
            else:
                return e.code, e.headers, b""
        except Exception as e:  # DNS, TLS, timeout: the registry said nothing
            last = repr(e)
        time.sleep(2 ** attempt)
    raise MeasurementError("%s: %s" % (url, last))


INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")


def probe(image):
    host, repo, tag, digest = split_ref(image)
    base, path, auth = PREFLIGHT.v2_endpoint(host, repo)
    accept = dict(auth)
    accept["Accept"] = ",".join(PREFLIGHT.V2_MANIFEST + PREFLIGHT.V1_MANIFEST)
    status, hdrs, body = fetch("%s/%s/manifests/%s" % (base, path, digest or tag), accept)
    facts = {"observed": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")}

    def refuse(reason, **extra):
        facts.update({"anonymous_pull": False, "reason": reason})
        facts.update(extra)
        return facts

    if status in (401, 403):
        # A registry answers an absent repository and a private one alike, deliberately, so this says both.
        return refuse("the registry answers HTTP %d to an anonymous request: the repository is private or does not "
                      "exist (a registry does not say which), and either way the kubelet, pulling without "
                      "credentials, cannot pull it" % status)
    if status == 404:
        return refuse("the registry has no manifest under this reference (HTTP 404)")
    if status != 200:
        return refuse("the manifest request answered HTTP %d" % status)
    mt = (hdrs.get("Content-Type") or "").split(";")[0].strip().lower()
    facts["media_type"] = mt
    facts["digest"] = hdrs.get("Docker-Content-Digest") or "sha256:" + hashlib.sha256(body).hexdigest()
    if mt in PREFLIGHT.V1_MANIFEST:
        return refuse("Docker Registry v1 (schema 1) manifest: no current docker or containerd pulls this "
                      "format (\"unsupported manifest media type\"), however the tag resolves (#115)")
    if mt not in PREFLIGHT.V2_MANIFEST:
        return refuse("manifest media type %r is not one a current containerd pulls" % mt)
    doc = json.loads(body)
    manifest_ref = None
    if mt in INDEX_TYPES:
        plats = set()
        for m in doc.get("manifests") or []:
            p = m.get("platform") or {}
            if p.get("os") in (None, "unknown"):
                continue  # an attestation, not a platform
            plat = "%s/%s" % (p.get("os"), p.get("architecture"))
            if p.get("variant"):
                plat += "/" + p["variant"]
            plats.add(plat)
            if plat == "linux/amd64" and manifest_ref is None:
                manifest_ref = m["digest"]
        facts["platforms"] = sorted(plats)
        if manifest_ref is None:
            return refuse("the index has no linux/amd64 image")
        child = dict(auth)
        child["Accept"] = ",".join(t for t in PREFLIGHT.V2_MANIFEST if t not in INDEX_TYPES)
        status, _, body = fetch("%s/%s/manifests/%s" % (base, path, manifest_ref), child)
        if status != 200:
            return refuse("the index's linux/amd64 manifest answered HTTP %d" % status)
        doc = json.loads(body)
    cfg = (doc.get("config") or {}).get("digest")
    if not cfg:
        return refuse("the image manifest names no config blob")
    status, _, body = fetch("%s/%s/blobs/%s" % (base, path, cfg), dict(auth))
    if status != 200:
        return refuse("the config blob answered HTTP %d to an anonymous request" % status)
    if mt not in INDEX_TYPES:
        c = json.loads(body)
        plat = "%s/%s" % (c.get("os"), c.get("architecture"))
        if c.get("variant"):
            plat += "/" + c["variant"]
        facts["platforms"] = [plat]
        if plat != "linux/amd64":
            return refuse("a single-platform image for %s, not linux/amd64" % plat)
    facts["anonymous_pull"] = True
    return facts


PULL_FACTS = ("anonymous_pull", "reason", "media_type", "platforms")
FACT_ORDER = ("observed", "anonymous_pull", "reason", "media_type", "platforms", "digest")


# ---------------------------------------------------------------------------------------------------------- markdown
def markdown(inv, sources):
    images = inv["images"]
    by_registry = {}
    for e in images:
        by_registry[e["registry"]] = by_registry.get(e["registry"], 0) + 1
    count = {"defaults": 0, "every-manifest-on": 0, "declared-only": 0}
    for e in images:
        best = min((u["rendered"] for u in e["used_by"]), key=list(count).index)
        count[best] += 1
    bad = [e for e in images if (e.get("registry_facts") or {}).get("anonymous_pull") is not True]
    out = [
        "---",
        "type: Configuration",
        "title: krateo-openstack-blueprint — container images",
        "description: Every container image a published chart can run, generated from the charts by "
        "hack/gen-image-inventory.py, with its publisher and whether its registry serves it anonymously.",
        "resource: oci://ghcr.io/krateo-blueprints/charts/openstack",
        "tags:",
        "  - openstack",
        "  - images",
        "  - supply-chain",
        "---",
        "",
        "# Container images",
        "",
        "Generated by `hack/gen-image-inventory.py` from the charts and `hack/image-sources.yaml`; do not edit.",
        "The machine-readable copy is `hack/images.yaml`, which `.github/workflows/image-scan.yaml` scans for CVEs.",
        "The pinning policy and the scan's allowlist are in [supply-chain.md](supply-chain.md).",
        "",
        "## Summary",
        "",
        "- **%d distinct images** across %d published charts." % (len(images), len(inv["charts"])),
        "- **By registry:** %s." % ", ".join("%s %d" % (r, n) for r, n in sorted(by_registry.items())),
        "- **Rendered at a chart's defaults:** %d. **Only with off-by-default manifests on:** %d. "
        "**Declared in `images.tags`, rendered by neither:** %d." % (
            count["defaults"], count["every-manifest-on"], count["declared-only"]),
        "- **Not pullable anonymously:** %d." % len(bad),
        "- **Charts that run no image:** %s." % (", ".join(inv["charts_without_images"]) or "none"),
        "",
        "\"Pullable anonymously\" was measured without credentials, as the kubelet pulls here: the manifest is served",
        "in a type containerd accepts (not Docker Registry v1), an index has a linux/amd64 entry, that manifest resolves",
        "and its config blob downloads. Layers were not fetched. Each row's measurement date is in `hack/images.yaml`.",
        "",
        "## Not pullable anonymously",
        "",
    ]
    if bad:
        for e in bad:
            r = e.get("registry_facts") or {}
            out.append("- `%s` (%s): %s" % (e["image"], ", ".join(u["chart"] for u in e["used_by"]),
                                            r.get("reason", "no registry measurement recorded")))
    else:
        out.append("None, as measured.")
    out += [
        "",
        "**#115, gnocchi:** upstream OpenStack-Helm points gnocchi at `quay.io/attcomdev/ubuntu-source-gnocchi-*:3.0.3`,",
        "which are Docker Registry v1 manifests: the tags resolve and no current docker can pull them. This repo",
        "repointed gnocchi at the OpenStack Kolla images (#117, kept by `hack/upstream-divergences.yaml`); their rows",
        "below are measured like every other. The registry check above fails on a v1 manifest, so a return of that",
        "format is a finding here, not a surprise at install time.",
        "",
        "## Images",
        "",
        "Platforms are the ones the registry lists for the reference. `d` = rendered at defaults, `m` = only with",
        "off-by-default manifests on, `x` = declared only.",
        "",
        "| Image | Publisher | Anonymous pull | Platforms | Charts |",
        "| --- | --- | --- | --- | --- |",
    ]
    short = {"defaults": "d", "every-manifest-on": "m", "declared-only": "x"}
    for e in images:
        r = e.get("registry_facts") or {}
        s = source_of(e["image"], sources)
        pull = "yes" if r.get("anonymous_pull") is True else "**no**"
        charts = ", ".join("%s (%s)" % (u["chart"], short[u["rendered"]]) for u in e["used_by"])
        out.append("| `%s` | %s | %s | %s | %s |" % (e["image"], s["publisher"] if s else "?", pull,
                                                   ", ".join(r.get("platforms") or []) or "-", charts))
    out += ["", "## Publishers", "", "| Prefix | Publisher | Built from |", "| --- | --- | --- |"]
    for s in sorted(sources, key=lambda s: s["prefix"]):
        out.append("| `%s` | %s | %s |" % (s["prefix"], s["publisher"], s["source"]))
    out.append("")
    return "\n".join(out)


def dump(inv):
    return HEADER + yaml.safe_dump(inv, sort_keys=False, default_flow_style=False, width=200)


# ---------------------------------------------------------------------------------------------------------- main
def main():
    failures = []
    usage, without, render_errors, renders = derive()
    for e in render_errors:
        failures.append(e)
    entries = offline_entries(usage)
    charts = [n for n, _ in published_charts()]

    sources = load_sources()
    if not sources:
        failures.append("hack/image-sources.yaml lists no sources")
    for e in entries:
        if source_of(e["image"], sources) is None:
            failures.append("%s: no publisher for %s/%s in hack/image-sources.yaml; say where it comes from"
                            % (e["image"], e["registry"], e["repository"]))
    for s in sources:
        if not any(source_of(e["image"], sources) is s for e in entries):
            failures.append("hack/image-sources.yaml: %s matches no image any chart runs; remove it" % s["prefix"])

    old = {}
    if os.path.exists(INVENTORY):
        prev = yaml.safe_load(open(INVENTORY)) or {}
        old = {e["image"]: e.get("registry_facts") for e in prev.get("images") or [] if e.get("registry_facts")}

    measured, moved, changed = 0, [], []
    for e in entries:
        rec = old.get(e["image"])
        if RESOLVE or (WRITE and rec is None):
            try:
                facts = probe(e["image"])
                facts = {k: facts[k] for k in FACT_ORDER if k in facts}
            except MeasurementError as err:
                print("FAILED MEASUREMENT: %s could not be asked: %s" % (e["image"], err))
                sys.exit(2)
            measured += 1
            if rec is not None:
                if rec.get("digest") and facts.get("digest") and rec["digest"] != facts["digest"]:
                    moved.append((e["image"], rec["digest"], facts["digest"]))
                diff = [k for k in PULL_FACTS if rec.get(k) != facts.get(k)]
                if diff:
                    changed.append((e["image"], {k: rec.get(k) for k in diff}, {k: facts.get(k) for k in diff}))
            # A check compares the files against what was recorded; the fresh answer is compared to the record
            # above. Only --write replaces the record.
            e["registry_facts"] = facts if WRITE or rec is None else rec
        elif rec is not None:
            e["registry_facts"] = rec
        else:
            failures.append("%s: no registry measurement recorded; run hack/gen-image-inventory.py . --write"
                            % e["image"])

    inv = {"charts": charts, "charts_without_images": without, "images": entries}
    text = dump(inv)
    doc = markdown(inv, sources)

    if WRITE:
        open(INVENTORY, "w").write(text)
        open(DOC, "w").write(doc)
        print("wrote hack/images.yaml and docs/images.md: %d images from %d charts (%d renders), %d registry "
              "measurement(s)" % (len(entries), len(charts), renders, measured))
    else:
        for path, want in ((INVENTORY, text), (DOC, doc)):
            have = open(path).read() if os.path.exists(path) else ""
            if have != want:
                rel = os.path.relpath(path, ROOT)
                d = list(difflib.unified_diff(have.splitlines(), want.splitlines(), rel + " (checked in)",
                                              rel + " (from the charts)", lineterm="", n=1))
                failures.append("%s is not what the charts generate (%d diff lines); run "
                                "hack/gen-image-inventory.py . --write\n%s" % (rel, len(d), "\n".join(d[:80])))

    for img, a, b in moved:
        print("DIGEST MOVED (reported, not failed; see docs/supply-chain.md): %s %s -> %s" % (img, a, b))
    for img, a, b in changed:
        failures.append("%s: the registry's answer changed: recorded %s, now %s" % (img, a, b))

    if failures:
        for f in failures:
            print("FAIL: " + f)
        sys.exit(1)
    pullable = sum(1 for e in entries if e["registry_facts"].get("anonymous_pull") is True)
    print("OK: %d images from %d charts (%d renders) match hack/images.yaml and docs/images.md; %d pullable "
          "anonymously as recorded%s" % (len(entries), len(charts), renders, pullable,
                                         ", %d re-measured, %d digest(s) moved" % (measured, len(moved))
                                         if RESOLVE else ""))


main()
