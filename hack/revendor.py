#!/usr/bin/env python3
"""Three-way merge a vendored openstack-helm chart's values.yaml onto a newer upstream.

    hack/revendor.py <component> --base <version> --theirs <version> [--apply]

Why a tool rather than doing it by hand: the identity pilot (#66) was done ad hoc, so its
method survived only in a PR description. Twenty-nine charts remain, each with the same
decision to make on every leaf, and "carried 31 overrides" is not a reproducible claim unless
something re-derives it.

The merge rule, per leaf path:

    ours == base,  theirs != base   -> take THEIRS   (upstream moved; we had not overridden)
    ours != base,  theirs == base   -> keep OURS     (our override; upstream did not move)
    ours != base,  theirs != base,
                   ours != theirs   -> CONFLICT      (both moved; a human decides)
    ours == theirs                  -> nothing to do

The middle-left case is what protects deliberate Krateo policy. horizon_secret_key is the
example that matters: upstream ships a published Django SECRET_KEY as its default and still
does in 2026.2, we replaced it (#78), and a merge that silently took THEIRS would reintroduce
a live signing key that anyone can read. That case is asserted in --selftest.

ruamel round-trip, not yaml.safe_dump: safe_dump destroys block scalars (the identity pilot
lost horizon's 1457-line values.yaml down to 659 that way) and reorders keys, which turns a
30-line review into an unreviewable one.
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("ruamel.yaml is required: pip install ruamel.yaml")

REPO = "openstack-helm"


def yaml_rt():
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # never re-wrap; a rewrap is a diff with no content
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def pull(component, version, dest):
    """Fetch an upstream chart and return its parsed values.yaml."""
    subprocess.run(
        ["helm", "pull", f"{REPO}/{component}", "--version", version,
         "--untar", "--untardir", str(dest)],
        check=True, capture_output=True,
    )
    return yaml_rt().load((dest / component / "values.yaml").read_text())


def walk(node, prefix=()):
    """Yield (path, value) for every leaf. Lists are leaves: a positional merge of
    an upstream list is not meaningful, and comparing them element-wise produced a
    false 'override' on pod.tolerations during the pilot."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, prefix + (k,))
    else:
        yield prefix, node


def get(node, path):
    for p in path:
        if not isinstance(node, dict) or p not in node:
            return KeyError
        node = node[p]
    return node


def put(node, path, value):
    for p in path[:-1]:
        if p not in node or not isinstance(node[p], dict):
            node[p] = {}
        node = node[p]
    node[path[-1]] = value


def merge(base, ours, theirs):
    """Return (merged, taken, kept, conflicts). `merged` is OURS, mutated in place, so
    comments and formatting survive."""
    taken, kept, conflicts = [], [], []
    paths = {p for p, _ in walk(base)} | {p for p, _ in walk(ours)} | {p for p, _ in walk(theirs)}
    for path in sorted(paths):
        b, o, t = get(base, path), get(ours, path), get(theirs, path)
        if o == t:
            continue
        if o == b and t is not KeyError:
            put(ours, path, t)
            taken.append(path)
        elif t == b:
            kept.append(path)
        else:
            conflicts.append((path, b, o, t))
    return ours, taken, kept, conflicts


def selftest():
    """The override case must hold, because getting it wrong reintroduces a live secret."""
    y = yaml_rt()
    base = y.load("conf:\n  horizon:\n    horizon_secret_key: 9aee62c0-uuid\n")
    ours = y.load("conf:\n  horizon:\n    horizon_secret_key: changeme-set-me\n")
    theirs = y.load("conf:\n  horizon:\n    horizon_secret_key: 9aee62c0-uuid\n")
    merged, taken, kept, conflicts = merge(base, ours, theirs)
    got = merged["conf"]["horizon"]["horizon_secret_key"]
    assert got == "changeme-set-me", f"override NOT preserved: {got!r}"
    assert ("conf", "horizon", "horizon_secret_key") in kept
    assert not conflicts

    # And the inverse: where we never overrode, upstream's change must land.
    base = y.load("images:\n  tags:\n    glance: old\n")
    ours = y.load("images:\n  tags:\n    glance: old\n")
    theirs = y.load("images:\n  tags:\n    glance: new\n")
    merged, taken, kept, conflicts = merge(base, ours, theirs)
    assert merged["images"]["tags"]["glance"] == "new", "upstream change not taken"
    assert ("images", "tags", "glance") in taken

    # And a genuine conflict must be reported, not silently resolved either way.
    base = y.load("a: 1\n")
    ours = y.load("a: 2\n")
    theirs = y.load("a: 3\n")
    _, _, _, conflicts = merge(base, ours, theirs)
    assert len(conflicts) == 1, "both-moved case was not reported as a conflict"
    print("selftest: override preserved, upstream change taken, conflict reported")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("component", nargs="?")
    ap.add_argument("--base", help="upstream version this chart was vendored from")
    ap.add_argument("--theirs", help="upstream version to move to")
    ap.add_argument("--apply", action="store_true", help="write the merge; otherwise report only")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.component and a.base and a.theirs):
        ap.error("component, --base and --theirs are required")

    ours_path = Path("blueprints") / a.component / "chart" / "values.yaml"
    if not ours_path.is_file():
        sys.exit(f"no vendored chart at {ours_path}")

    y = yaml_rt()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        base = pull(a.component, a.base, td / "base")
        theirs = pull(a.component, a.theirs, td / "theirs")
        ours = y.load(ours_path.read_text())
        merged, taken, kept, conflicts = merge(base, ours, theirs)

        print(f"{a.component}: {a.base} -> {a.theirs}")
        print(f"  upstream changes taken : {len(taken)}")
        print(f"  krateo overrides kept  : {len(kept)}")
        print(f"  conflicts (both moved) : {len(conflicts)}")
        for path, b, o, t in conflicts:
            print(f"    {'.'.join(map(str, path))}")
            print(f"      base={b!r}")
            print(f"      ours={o!r}")
            print(f"      theirs={t!r}")

        if a.apply:
            if conflicts:
                sys.exit("refusing to write with unresolved conflicts; resolve them by hand")
            buf = ours_path.open("w")
            y.dump(merged, buf)
            buf.close()
            print(f"  wrote {ours_path}")
        else:
            print("  (report only; pass --apply to write)")
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
