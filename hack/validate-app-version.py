#!/usr/bin/env python3
"""Every chart's appVersion is the version of the software it runs (#235).

Why this exists. From 0.3.46 every OpenStack image was the 2026.1 series while all 37 blueprint charts and the
capabilities chart still said appVersion "2025.1". The Marketplace index and every Helm consumer read appVersion
as the release inside; a sibling project pinned its documentation to Keystone's retired 2025.1 branch because of
it. Nothing could fail when the two disagreed.

hack/app-versions.yaml says where each chart's version comes from (series / series-of / tag / measured /
library / excluded). This gate:
  - fails on any Chart.yaml in the repo that the map does not list, and on any map entry with no Chart.yaml;
  - series:    every <series>-ubuntu_<distro>[-YYYYMMDD] tag in the chart's values.yaml is one series, and appVersion
               is it;
  - series-of: appVersion is the series of the named chart;
  - tag:       appVersion is the version in images.tags.<image> (leading "v" and any "-suffix" dropped);
  - measured:  appVersion is the recorded version AND images.tags.<image> still has the recorded tag (a new image
               needs a new measurement);
and fails when it compared nothing, or fewer charts than the map lists as checked.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
# A series tag, or the dated build of one (2026.1-ubuntu_noble-20261117): images are pinned to dated tags (#316).
SERIES_TAG = re.compile(r":(\d{4}\.\d)-ubuntu_[a-z]+(?:-20\d{6})?$")
failures = []


def fail(msg):
    failures.append(msg)


def chart_yaml(d):
    return yaml.safe_load((ROOT / d / "Chart.yaml").read_text()) or {}


def values(d):
    p = ROOT / d / "values.yaml"
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def tags(d):
    return ((values(d).get("images") or {}).get("tags") or {})


def series_of(d):
    found = {m.group(1) for t in tags(d).values() if isinstance(t, str) for m in [SERIES_TAG.search(t)] if m}
    if len(found) != 1:
        fail("%s: its images carry %s OpenStack series, want exactly one" % (d, sorted(found) or "no"))
        return None
    return found.pop()


def main():
    entries = (yaml.safe_load((ROOT / "hack/app-versions.yaml").read_text()) or {}).get("charts") or {}
    on_disk = {str(p.parent.relative_to(ROOT)) for p in ROOT.glob("**/Chart.yaml")
               if not any(part in (".git", "node_modules") for part in p.parts)
               and "/charts/helm-toolkit/" not in "/" + str(p.relative_to(ROOT)) or str(p.parent.relative_to(ROOT)) == "charts/helm-toolkit"}
    # vendored copies of helm-toolkit inside a blueprint (blueprints/*/helm-toolkit, */charts/helm-toolkit) are
    # not charts of their own
    on_disk = {d for d in on_disk if d == "charts/helm-toolkit" or not d.endswith("helm-toolkit")}
    for d in sorted(on_disk - set(entries)):
        fail("%s/Chart.yaml is not listed in hack/app-versions.yaml: say where its appVersion comes from" % d)
    for d in sorted(set(entries) - on_disk):
        fail("hack/app-versions.yaml lists %s, which has no Chart.yaml" % d)
    compared, checked = 0, 0
    for d, e in sorted(entries.items()):
        src = e.get("source")
        if src in ("library", "excluded") or d not in on_disk:
            continue
        checked += 1
        have = str(chart_yaml(d).get("appVersion"))
        if src == "series":
            want = series_of(d)
        elif src == "series-of":
            want = series_of(e["chart"])
        elif src == "tag":
            t = tags(d).get(e["image"], "")
            m = re.search(r":v?([0-9][0-9A-Za-z.]*)(?:-[^:]*)?$", t)
            want = m.group(1) if m else None
            if not want:
                fail("%s: images.tags.%s is %r, which carries no version" % (d, e["image"], t))
        elif src == "measured":
            t = tags(d).get(e["image"], "")
            if not t.endswith(":" + e["tag"]):
                fail("%s: images.tags.%s is %r, not the measured %r: measure the new image and record it"
                     % (d, e["image"], t, e["tag"]))
            want = str(e["version"])
        else:
            fail("%s: unknown source %r" % (d, src))
            continue
        if want is None:
            continue
        compared += 1
        if have != want:
            fail("%s: appVersion is %r, want %r (%s)" % (d, have, want, src))
    if compared == 0 or compared < checked:
        fail("compared %d of %d checked charts: a gate that compares less than it lists proves nothing" % (compared, checked))
    if failures:
        for f in failures:
            print("FAIL: " + f)
        return 1
    print("OK: %d charts' appVersion is the software they run (series, tag or measured), %d listed in all" % (compared, len(entries)))
    return 0


sys.exit(main())
