#!/usr/bin/env python3
"""Every documented Composition apply first waits until its kind is served at that version.

core-provider creates a blueprint's kind from its CompositionDefinition some seconds after the
CompositionDefinition is applied (28 s on a fresh GKE cluster, 0.3.47, core-provider 2.13.82).
README's install steps applied the Composition straight after, which failed:

    Error from server (ServiceUnavailable): ... Resource kind
    composition.krateo.io/v0-3-47, Kind=Openstack not found.

`kubectl wait compositiondefinition/... --for=condition=Ready` is not the fix: it says
core-provider is done, not which version the API server serves, and during an upgrade the
previous version is still served, so it passes at once. The predicate that holds in both cases
asks the API server for this release's group-version and looks for the kind:

    V=v$(printf %s "$BP_VERSION" | tr . -)
    ... kubectl get --raw "/apis/composition.krateo.io/$V" ... grep -q '"kind":"Openstack"'

Matching the kind, not the plural, needs nothing derived: core-provider's plurals are not
mechanical (Nova's is `nova`, Openvswitch's `openvswitches`).

For every fenced sh/bash block in the repo's own Markdown, this gate checks:
  * each `kubectl apply` of a release Composition (`example-*.yaml`, or `$EX` set from one) is
    preceded in the block by `V=v$(printf %s "$BP_VERSION" | tr . -)` and a bounded
    `until kubectl get --raw "/apis/composition.krateo.io/$V"` poll that greps `"kind":"...";
  * when the apply is of `$EX`, the poll's kinds are read from that same `$EX` (`sed -n
    's/^kind: *//p'`), and when it is a fixed example the poll names a kind, so the wait
    cannot drift from what is applied;
  * no block uses `kubectl wait compositiondefinition` (version-blind), and none applies an
    unstamped template (`blueprints/*/compositiondefinition.yaml`, `examples/*.yaml`,
    `../openstack.yaml`): their `CHART_VERSION` / `COMPOSITION_API_VERSION` placeholders install
    nothing.
It fails when no Composition apply is found in any document: a gate that read nothing has
measured nothing.

Usage: hack/validate-docs-wait-for-served-kind.py [repo-root]
"""
import argparse
import re
import sys
from pathlib import Path

SKIP_PARTS = {"helm-toolkit", "node_modules", ".git", ".claude", "vendor"}
FENCE = re.compile(r"^\s*(?:> ?)?```(\w*)\s*$")
V_LINE = re.compile(r'''V=v\$\(printf %s "\$BP_VERSION" \| tr \. -\)''')
POLL = re.compile(r'''until kubectl get --raw "/apis/composition\.krateo\.io/\$V"[^\n]*grep -q [^\n]*\\?"kind\\?":''')
BOUND = re.compile(r"\[ \$i -lt \d+ \]")
KINDS_FROM_EX = re.compile(r'''for k in \$\(curl -fsSL "\$EX" \| sed -n 's/\^kind: \*//p'\)''')
APPLY_RELEASE = re.compile(r"kubectl apply -f (?:\S*/example-[\w-]+\.yaml|\"\$EX\"|-)")
VERSION_BLIND = re.compile(r"kubectl wait (?:--\S+ )*compositiondefinition")
UNSTAMPED = re.compile(r"kubectl apply -f \S*(?:blueprints/[\w-]+/compositiondefinition\.yaml|examples/[\w-]+\.yaml|\.\./openstack\.yaml)\b")


def blocks(text):
    """(start line, lang, [lines]) for every fenced block; backslash continuations joined."""
    out, cur, lang, start = [], None, None, 0
    for n, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if m and cur is None:
            cur, lang, start = [], m.group(1).lower(), n
        elif m:
            out.append((start, lang, cur))
            cur = None
        elif cur is not None:
            cur.append(re.sub(r"^\s*> ?", "", line) if line.lstrip().startswith(">") else line)
    joined = []
    for start, lang, lines in out:
        logical, buf = [], ""
        for line in lines:
            if line.rstrip().endswith("\\"):
                buf += line.rstrip()[:-1] + " "
            else:
                logical.append(buf + line)
                buf = ""
        if buf:
            logical.append(buf)
        joined.append((start, lang, logical))
    return joined


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    docs = [p for p in sorted(root.rglob("*.md"))
            if not (set(p.relative_to(root).parts) & SKIP_PARTS)]
    failures, applies = [], 0
    for doc in docs:
        rel = doc.relative_to(root)
        for start, lang, lines in blocks(doc.read_text()):
            if lang not in ("sh", "bash", "shell", "console", "zsh"):
                continue
            body = "\n".join(lines)
            for line in lines:
                code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
                if VERSION_BLIND.search(code):
                    failures.append(f"{rel}:{start}: `kubectl wait compositiondefinition` is version-blind "
                                    "(it passes while the previous version is served); poll the API "
                                    "server for this version's group instead")
                if UNSTAMPED.search(code):
                    failures.append(f"{rel}:{start}: applies an unstamped template ({code.strip()[:90]}); "
                                    "apply the release-stamped file for one BP_VERSION")
            seen = []
            for i, line in enumerate(lines):
                code = "" if line.lstrip().startswith("#") else line
                m = APPLY_RELEASE.search(code)
                if not m:
                    continue
                if m.group(0).endswith(" -") and "example-" not in code:
                    continue
                applies += 1
                before = "\n".join(lines[:i])
                where = f"{rel}:{start} (`{code.strip()[:80]}`)"
                problems = []
                if not V_LINE.search(before):
                    problems.append('no `V=v$(printf %s "$BP_VERSION" | tr . -)` before it')
                if not POLL.search(before):
                    problems.append('no `until kubectl get --raw "/apis/composition.krateo.io/$V" ... '
                                    'grep -q \'"kind":...\'` poll before it')
                else:
                    # the bound must be in THIS poll's loop: the poll line and the line after it
                    last = max(j for j in range(i) if POLL.search(lines[j]))
                    if not BOUND.search("\n".join(lines[last:last + 2])):
                        problems.append("the poll has no bound (`[ $i -lt N ]` in its own loop), so a "
                                        "stuck engine hangs it")
                if '"$EX"' in m.group(0) and not KINDS_FROM_EX.search(before):
                    problems.append("it applies $EX but the poll's kinds are not read from $EX")
                if problems:
                    failures.append(f"{where}: " + "; ".join(problems))
                seen.append(i)
    if applies == 0:
        print("FAIL: no documented Composition apply found -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures:
        print(f"FAIL: {len(failures)} problem(s) in {len(docs)} documents")
        return 1
    print(f"OK: {applies} documented Composition apply(ies) each wait until the kind is served at "
          f"their BP_VERSION; no version-blind wait and no unstamped template in {len(docs)} documents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
