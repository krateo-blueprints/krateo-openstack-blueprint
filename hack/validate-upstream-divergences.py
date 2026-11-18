#!/usr/bin/env python3
"""Assert every declared divergence from upstream openstack-helm still holds.

hack/revendor.py three-way merges values.yaml, so a values override survives a re-vendor automatically: `ours !=
base, theirs == base` keeps ours. It never writes templates. A template divergence is therefore protected by nothing at
all -- anyone who refreshes a chart's templates from upstream by hand silently reverts it, and the revert is invisible
because the values half of the same fix stays in place. That combination is worse than either state alone. So every
intentional divergence is declared, with the reason it exists, and this asserts it still holds. A gate, not a memory.

Each divergence is one file, hack/upstream-divergences.d/<component>-<slug>.yaml (one file per entry since
2026-11-18: every pull request appended to one hack/upstream-divergences.yaml, so branches in flight re-conflicted on
each merge). Its keys, with comments above them saying why:
  component   the blueprint whose chart diverges
  issue       where the reasoning lives, so a future reader can judge whether it still applies
  reason      why upstream is wrong for this repo, in one sentence
  assertions  checks against a file, relative to blueprints/<component>/chart/
                absent  - this string must NOT appear outside comments
                present - this string MUST appear outside comments

Refused, before anything is checked: a file that is not one mapping with exactly those keys (two entries in a file, a
list, a second YAML document); a duplicate key inside a file; a file whose name does not start with its component; an
assertion (component, file, absent/present, string) declared in two files; the old hack/upstream-divergences.yaml
coming back; and an empty or missing directory -- a gate that silently measures nothing is the defect this repo has
already shipped once (four gates printed "SKIP: PyYAML unavailable" and exited 0 for weeks, so a missing dependency
here exits non-zero rather than skipping).
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
    drops whole comment lines and never rewrites code. Shell and YAML share '#'; Go templates
    additionally use {{/* ... */}}, which is handled by dropping those blocks whole.

    Go-template comments are stripped from .yaml as well as .tpl, because a helm template IS a
    .yaml file -- templates/deployment-api.yaml and friends. Handling only .tpl left exactly the
    files a divergence is most likely to be explained in: issue #110's entries name
    `pod.probes.<svc>.liveness.port` in the {{/* */}} block above the probe they removed it from,
    which under the old rule satisfied its own `absent` assertion and could never fail.
    """
    if suffix in (".tpl", ".yaml", ".yml"):
        text = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", text, flags=re.S)
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(line.split(" #")[0] if suffix in (".yaml", ".yml") else line)
    return "\n".join(out)


KEYS = {"component", "issue", "reason", "assertions"}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _no_duplicates(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep)


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def load(root):
    """The declared divergences, one per file, and the problems that refuse them."""
    problems = []
    old = root / "hack" / "upstream-divergences.yaml"
    if old.exists():
        problems.append(f"{old.relative_to(root)} is back; each divergence is a file in hack/upstream-divergences.d/")
    d = root / "hack" / "upstream-divergences.d"
    files = sorted(d.glob("*")) if d.is_dir() else []
    if not files:
        return [], problems + ["FAILED MEASUREMENT: no divergence declared in hack/upstream-divergences.d; refusing "
                               "to report success"]
    entries, seen = [], {}
    for f in files:
        rel = f"hack/upstream-divergences.d/{f.name}"
        if f.suffix != ".yaml":
            problems.append(f"{rel}: not a .yaml file")
            continue
        try:
            docs = list(yaml.load_all(f.read_text(), Loader=UniqueKeyLoader))
        except yaml.YAMLError as e:
            problems.append(f"{rel}: {str(e).splitlines()[0]}")
            continue
        if len(docs) != 1 or not isinstance(docs[0], dict) or set(docs[0]) != KEYS:
            problems.append(f"{rel}: must hold exactly one divergence, a mapping with keys {sorted(KEYS)}")
            continue
        e = docs[0]
        if not re.fullmatch(r"%s-[a-z0-9]+(?:-[a-z0-9]+)*\.yaml" % re.escape(str(e["component"])), f.name):
            problems.append(f"{rel}: its component is {e['component']!r}, so it must be named "
                            f"{e['component']}-<slug>.yaml")
        if not isinstance(e["assertions"], list) or not e["assertions"]:
            problems.append(f"{rel}: no assertions")
            continue
        for a in e["assertions"]:
            for kind in ("absent", "present"):
                if kind in a:
                    k = (e["component"], a.get("file"), kind, a[kind])
                    if k in seen:
                        problems.append(f"{rel}: {e['component']} {a.get('file')} {kind} {a[kind]!r} is already "
                                        f"declared in {seen[k]}")
                    seen[k] = rel
        e["_file"] = rel
        entries.append(e)
    return entries, problems


def main(root):
    root = pathlib.Path(root)
    entries, problems = load(root)
    if problems:
        print("The divergence declarations are refused:\n")
        for p in problems:
            print(f"  - {p}")
        return 1

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
                    f"      Divergence rationale: see issue #{entry['issue']} ({entry['_file']})."
                )
            if "present" in a and a["present"] not in body:
                failures.append(
                    f"{component}: {a['file']} is missing {a['present']!r}, which must be present.\n"
                    f"      {' '.join(a['why'].split())}\n"
                    f"      Divergence rationale: see issue #{entry['issue']} ({entry['_file']})."
                )

    if failures:
        print("A deliberate divergence from upstream has been reverted:\n")
        for f in failures:
            print(f"  - {f}")
        print(
            "\nIf upstream has genuinely fixed the underlying problem, remove the entry's file from"
            "\nhack/upstream-divergences.d/ in the same change, with the evidence."
        )
        return 1
    print(f"OK: {checked} assertions across {len(entries)} declared divergences (one file each in "
          f"hack/upstream-divergences.d) still hold")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
