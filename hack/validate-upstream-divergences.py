#!/usr/bin/env python3
"""Assert every declared divergence from upstream openstack-helm still holds.

hack/revendor.py merges values.yaml and never touches templates, so a template divergence has no
protection. This supplies it. See hack/upstream-divergences.yaml for the reasoning per entry.

Refuses to pass on zero input: an empty or unreadable declaration file is a failure, not a pass.
A gate that silently measures nothing is the defect this repo has already shipped once -- four
gates printed "SKIP: PyYAML unavailable" and exited 0 for weeks, so a missing dependency here
exits non-zero rather than skipping.
"""
import pathlib
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")


def strip_comments(text, suffix):
    """Remove comment lines so a divergence described IN a comment cannot satisfy or trip a check.

    This repo has produced false findings from careless text transformation before, so this only
    drops whole comment lines and never rewrites code. Shell and YAML share '#'; Go templates in
    .tpl files additionally use {{/* ... */}}, which is handled by dropping those blocks whole.
    """
    if suffix == ".tpl":
        text = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", text, flags=re.S)
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(line.split(" #")[0] if suffix in (".yaml", ".yml") else line)
    return "\n".join(out)


def main(root):
    root = pathlib.Path(root)
    decl = root / "hack" / "upstream-divergences.yaml"
    if not decl.is_file():
        sys.exit(f"FAILED MEASUREMENT: {decl} is missing")
    doc = yaml.safe_load(decl.read_text()) or {}
    entries = doc.get("divergences") or []
    if not entries:
        sys.exit("FAILED MEASUREMENT: no divergences declared; refusing to report success")

    failures, checked = [], 0
    for entry in entries:
        component = entry["component"]
        chart = root / "blueprints" / component / "chart"
        for a in entry.get("assertions") or []:
            target = chart / a["file"]
            if not target.is_file():
                failures.append(
                    f"{component}: {a['file']} does not exist, so the divergence cannot hold"
                )
                continue
            body = strip_comments(target.read_text(), target.suffix)
            checked += 1
            if "absent" in a and a["absent"] in body:
                failures.append(
                    f"{component}: {a['file']} contains {a['absent']!r}, which must be absent.\n"
                    f"      {' '.join(a['why'].split())}\n"
                    f"      Divergence rationale: see issue #{entry['issue']}."
                )
            if "present" in a and a["present"] not in body:
                failures.append(
                    f"{component}: {a['file']} is missing {a['present']!r}, which must be present.\n"
                    f"      {' '.join(a['why'].split())}\n"
                    f"      Divergence rationale: see issue #{entry['issue']}."
                )

    if failures:
        print("A deliberate divergence from upstream has been reverted:\n")
        for f in failures:
            print(f"  - {f}")
        print(
            "\nIf upstream has genuinely fixed the underlying problem, remove the entry from"
            "\nhack/upstream-divergences.yaml in the same change, with the evidence."
        )
        return 1
    print(f"OK: {checked} assertions across {len(entries)} declared divergences still hold")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
