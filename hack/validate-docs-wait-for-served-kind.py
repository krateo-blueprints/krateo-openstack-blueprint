#!/usr/bin/env python3
"""Every documented Composition apply first waits until the API server accepts that very file.

core-provider creates a blueprint's kind from its CompositionDefinition some seconds after the
CompositionDefinition is applied (28 s on a fresh GKE cluster, 0.3.47, core-provider 2.13.82).
README's install steps applied the Composition straight after, which failed:

    Error from server (ServiceUnavailable): ... Resource kind
    composition.krateo.io/v0-3-47, Kind=Openstack not found.

The first fix (#227) waited until DISCOVERY listed the kind at this version. That is not the
predicate either (#244): on a fresh cluster (osh-s2, 0.3.49) discovery listed Openstack at
07:40:24 and the apply one second later still got the same 503 from the API server -- the CRD was
in discovery, its handler not serving yet. `kubectl wait compositiondefinition` is version-blind
(during an upgrade the previous version is still served). The predicate that is exactly right is
the apply itself, without its effect: a server dry-run of the SAME file, which goes through the
same handler, admission and schema, and needs no plural (core-provider's are not mechanical).

    kubectl create namespace openstack            # a namespaced dry-run needs the namespace
    OS=<the release's example-openstack.yaml>
    i=0; until kubectl apply --dry-run=server -f "$OS" >/dev/null 2>&1; do
      i=$((i+1)); [ $i -lt 60 ] || { ...print the server's error...; break; }; sleep 5; done
    kubectl apply -f "$OS"

For every fenced sh/bash block in the repo's own Markdown, this gate checks:
  * each Composition apply (`kubectl apply -f <...>/example-*.yaml`, or `-f "$VAR"` in a block
    that applies an example) is preceded in the block by a bounded
    `until kubectl apply --dry-run=server -f <the same target>` loop, and by
    `kubectl create namespace openstack` before that loop;
  * no block waits on discovery (`until kubectl get --raw "/apis/composition.krateo.io/...`):
    that is the #244 race, refused rather than merely not required;
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
DISCOVERY_POLL = re.compile(r'until kubectl get --raw "/apis/composition\.krateo\.io')
DRY_RUN = re.compile(r'until kubectl apply --dry-run=server -f "?([^"\s]+)"?')
BOUND = re.compile(r"\[ \$i -lt \d+ \]")
APPLY = re.compile(r'kubectl apply -f "?([^"\s]+)"?(?!\S)')
NAMESPACE = re.compile(r"kubectl create namespace openstack\b(?!-)")
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
            for line in lines:
                code = "" if line.lstrip().startswith("#") else line
                if DISCOVERY_POLL.search(code):
                    failures.append(f"{rel}:{start}: waits on discovery (`kubectl get --raw /apis/...`): discovery "
                                    "can list the kind a second before the API server serves it (#244); wait with a "
                                    "server dry-run of the file instead")
            mentions_example = "example-" in body
            for i, line in enumerate(lines):
                code = "" if line.lstrip().startswith("#") else line
                if "--dry-run" in code:
                    continue
                m = APPLY.search(code)
                if not m:
                    continue
                target = m.group(1)
                is_example = re.search(r"/example-[\w-]+\.yaml$", target) is not None
                is_var = target.startswith("$") and mentions_example
                if not (is_example or is_var):
                    continue
                applies += 1
                where = f"{rel}:{start} (`{code.strip()[:80]}`)"
                problems = []
                waits = [j for j in range(i) if (d := DRY_RUN.search(lines[j])) and d.group(1) == target]
                if not waits:
                    problems.append(f"no `until kubectl apply --dry-run=server -f {target}` wait before it (the "
                                    "same target: a wait on anything else can pass while this file is refused)")
                else:
                    w = waits[-1]
                    if not BOUND.search("\n".join(lines[w:w + 2])):
                        problems.append("the wait has no bound (`[ $i -lt N ]` in its own loop), so a refused file "
                                        "hangs it")
                    if not any(NAMESPACE.search(lines[j]) for j in range(w)):
                        problems.append("no `kubectl create namespace openstack` before the wait: a namespaced "
                                        "dry-run is refused forever without its namespace")
                if problems:
                    failures.append(f"{where}: " + "; ".join(problems))
    if applies == 0:
        print("FAIL: no documented Composition apply found -- nothing was measured")
        return 1
    for f in failures:
        print(f"FAIL {f}")
    if failures:
        print(f"FAIL: {len(failures)} problem(s) in {len(docs)} documents")
        return 1
    print(f"OK: {applies} documented Composition apply(ies) each wait until the API server accepts the same "
          f"file (server dry-run); no discovery wait, no version-blind wait and no unstamped template in "
          f"{len(docs)} documents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
