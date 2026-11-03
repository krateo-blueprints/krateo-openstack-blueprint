#!/usr/bin/env python3
"""No component may be unable to become ready because of something it is itself holding back.

hack/validate-component-dag.py proves the DECLARED graph is closed, acyclic and renderable. That
is not enough, and #110 is why. The declared DAG stayed acyclic the whole time the compute tier
was deadlocked on a 4-node GKE install of profile=full:

    neutron  deps = [keystone, rabbitmq, openvswitch]                        <- nova not declared
    nova     deps = [keystone, placement, rabbitmq, glance, neutron, libvirt]

while neutron's dhcp, l3 and metadata agents sat in Init:0/2 logging "Resolving dependency
Service nova-api", because openstack-helm's own charts order their startup with
kubernetes-entrypoint and those waits do not follow this umbrella's DAG. With nova's creation
gated on neutron's READINESS, neutron could not become Ready until nova-api existed and nova was
not created until neutron was Ready. Neither moved, and nothing reported an error: 3 of neutron's
73 managed children were simply not ready, forever.

So the cycle was real but it was not in the declared graph. It ran through two dimensions at
once -- a chart's runtime wait for a Service, and the umbrella's wait for a Composition -- and a
gate that looks at either dimension alone cannot see it. This one builds the graph of everything
that waits on everything else and looks for a cycle in the whole of it:

    composition/X is Ready        <- every managed child of X is ready
    child workload of X           <- X's Composition has been created
                                  <- every Service in its DEPENDENCY_SERVICE has endpoints,
                                     which means the workloads backing that Service are ready
                                  <- every pod in its DEPENDENCY_POD_JSON is ready
                                  <- every job in its DEPENDENCY_JOBS has completed
    creation of X                 <- for each dependency D, per compositions.yaml's per-edge
                                     gate: composition/D is READY, or, if the edge is declared
                                     in .Values.dependencyRelaxations, only that D EXISTS

The relaxed edges are the subject. Readiness is the default and the right default: it is what
sequences a dependent behind a dependency that actually works. It is only unreachable on an edge
whose endpoints lie inside a cycle of this graph, and it is perfectly reachable everywhere else
-- placement does not wait on nova, rabbitmq does not wait on freezer. So this gate asserts the
relax-set is exactly the set that is needed, in both directions:

  * every cycle must be broken by some declared relaxation. A new chart that introduces a cycle
    fails CI rather than silently needing another relaxation, which is the state #110 shipped in.
  * every declared relaxation must be inside a cycle, tested by tightening it back to readiness
    and requiring that a cycle reappears. A relaxation that is no longer load-bearing fails, so
    the set cannot accrete, and relaxing the whole DAG -- throwing away ordering on 79 edges that
    never had the problem -- fails here as 79 unnecessary relaxations.
  * and because both of those reason about the set as DECLARED, the partition that
    compositions.yaml actually computes is checked against the declarations too, by lifting its
    statements verbatim out of the template and running them. See check_partition.

A relaxation is "necessary" iff tightening it ALONE, with the rest of the set still relaxed,
produces a cycle. Two relaxations that break only the same single cycle therefore both report as
unnecessary, which is the correct complaint: neither is individually needed and the set is not
minimal.

Ground truth comes from rendering every component chart, not from reading values.yaml. Three of
the four resolutions involved cannot be done from values alone:
  * a dependency's `service: identity` is resolved through `endpoints.identity.hosts.<endpoint>`
    where <endpoint> is the dependency's own `endpoint:` field -- `internal` gives keystone-api
    while `default` gives keystone, and only one of the two is a Service anyone creates;
  * which component OWNS a Service is only visible as a Service object in its rendered output;
  * which workloads BACK that Service is a label-selector match against rendered pod templates.
Rendering also means a chart that stops emitting a wait, or renames a Service, changes the
measurement rather than slipping past a text match.

Zero input is a failure, never a pass. Every service name any workload waits on must resolve to
an owning component or be declared in EXTERNAL_SERVICES below with a reason -- so renaming a
Service cannot quietly empty this graph out -- and a render failure, an unreadable chart, a
missing dependency, a malformed relaxation, an unrecognised creation gate or a graph with no
edges all exit non-zero. Four gates in this repo printed "SKIP: PyYAML unavailable" and exited 0
for weeks (#120).

What this gate does NOT claim. It judges the shipped chart defaults; a workload that only the
umbrella's componentValues enables is not rendered and not measured. It says nothing about waits
that resolve to nothing -- an unmatched DEPENDENCY_POD_JSON selector or a DEPENDENCY_JOBS name no
manifest renders is a forever-wait of a different kind, counted and printed here but left to its
own gate. And it does not require a declared dependency to be backed by an entrypoint wait: it
reports that number, because an edge with neither a readiness gate nor an entrypoint wait is
sequenced by nothing at all, but which edges deserve a wait is a separate change.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Service names that are waited on but are deliberately not components of this blueprint. Any
# OTHER unresolved name is a failure: the usual cause is a renamed Service, which would silently
# delete edges from the graph and let a real cycle through.
EXTERNAL_SERVICES = {
    "postgresql": "gnocchi's alternative indexer backend; this blueprint ships the mariadb one",
}

WORKLOAD_KINDS = ("Deployment", "DaemonSet", "StatefulSet", "Job")

try:
    import yaml
except ImportError:
    sys.exit("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")


def fail_measurement(msg):
    print(f"FAILED MEASUREMENT: {msg}")
    sys.exit(2)


def read_umbrella(root):
    """The declared component DAG and the declared dependency relaxations."""
    values = root / "blueprints/openstack/chart/values.yaml"
    if not values.is_file():
        fail_measurement(f"no umbrella values at {values}")
    doc = yaml.safe_load(values.read_text()) or {}
    comps = {c["name"]: sorted(c.get("deps") or []) for c in doc.get("components") or []}
    if not comps:
        fail_measurement("the umbrella declares no components")
    for name, deps in comps.items():
        for d in deps:
            if d not in comps:
                fail_measurement(f"{name} declares dependency {d!r}, which is not a component "
                                 f"(hack/validate-component-dag.py reports this properly)")
    comps.pop("openstack", None)  # the umbrella is not one of its own components

    relaxed = {}
    for i, r in enumerate(doc.get("dependencyRelaxations") or []):
        if not isinstance(r, dict):
            fail_measurement(f"dependencyRelaxations[{i}] is not a mapping")
        missing = [k for k in ("component", "dependency", "issue", "cycle") if not r.get(k)]
        if missing:
            fail_measurement(f"dependencyRelaxations[{i}] is missing {', '.join(missing)}. Each "
                             f"entry names the dependent, the dependency, the issue, and the "
                             f"cycle that makes readiness unreachable on that edge -- an "
                             f"undocumented relaxation is indistinguishable from an accident")
        edge = (r["component"], r["dependency"])
        if edge in relaxed:
            fail_measurement(f"dependencyRelaxations declares {edge[0]} -> {edge[1]} twice")
        if edge[0] not in comps or edge[1] not in comps.get(edge[0], []):
            fail_measurement(f"dependencyRelaxations declares {edge[0]} -> {edge[1]}, which is "
                             f"not a declared dependency edge; relaxing an edge that does not "
                             f"exist hides whatever the real edge now is")
        relaxed[edge] = " ".join(str(r["cycle"]).split())
    return comps, relaxed


def read_gate(root):
    """Which creation gate compositions.yaml is in, read from the umbrella's own templates.

    Returns (description, mode). The mode decides which creation edges the graph gets, so it is
    read rather than assumed: a revert to uniform readiness must put the readiness edges back and
    fail here, and a blanket switch to existence must relax every edge and fail on minimality.
    """
    tpl = root / "blueprints/openstack/chart/templates"
    comp_yaml, helpers = tpl / "compositions.yaml", tpl / "_helpers.tpl"
    for f in (comp_yaml, helpers):
        if not f.is_file():
            fail_measurement(f"{f} is missing, so the creation gate cannot be read")
    body = comp_yaml.read_text()
    source = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", helpers.read_text(), flags=re.S)

    called = set(re.findall(r'include\s+"(osh\.deps[A-Za-z]*)"', body))
    unknown = called - {"osh.depsReady", "osh.depsExist", "osh.depsRelaxed"}
    if unknown:
        fail_measurement(f"compositions.yaml gates creation on {sorted(unknown)}, whose semantics "
                         f"this gate does not know. Teach it in the same change that introduces "
                         f"it -- a creation gate it cannot model is a gate it cannot check")
    if not called:
        fail_measurement("compositions.yaml calls no dependency predicate at all, so nothing "
                         "orders creation")

    # What each predicate actually resolves a dependency with, so renaming one to the other
    # cannot change this gate's verdict without changing this gate. Comments go first: this repo
    # documents a helper by explaining the one it replaced, and a name in prose must neither
    # satisfy nor trip a check (the same reason validate-upstream-divergences.py strips them).
    def resolves_with(name):
        bodies = [b for b in re.split(r'(?=\{\{-?\s*define\s+")', source)
                  if re.match(r'\{\{-?\s*define\s+"%s"' % re.escape(name), b)]
        if len(bodies) != 1:
            fail_measurement(f"_helpers.tpl defines {name!r} {len(bodies)} times; expected once")
        return ('include "osh.ready"' in bodies[0], 'include "osh.exists"' in bodies[0])

    ready_called = "osh.depsReady" in called
    exist_called = "osh.depsExist" in called
    if ready_called:
        by_ready, by_exists = resolves_with("osh.depsReady")
        if not by_ready:
            fail_measurement("osh.depsReady does not resolve dependencies with osh.ready, so it "
                             "is not the readiness gate its name claims")
    if exist_called:
        by_ready, by_exists = resolves_with("osh.depsExist")
        if by_ready:
            # Redefined under the other name. Model what it does, not what it is called.
            return "readiness (osh.depsExist, redefined in terms of osh.ready)", "readiness"
        if not by_exists:
            fail_measurement("osh.depsExist resolves dependencies with neither osh.exists nor "
                             "osh.ready, so this gate cannot model it")

    if "osh.depsRelaxed" in called and not (ready_called and exist_called):
        fail_measurement("compositions.yaml partitions its dependencies with osh.depsRelaxed but "
                         "calls only one of the two gates, so one half of every component's "
                         "dependency list is gated by nothing at all")
    if ready_called and exist_called:
        if "osh.depsRelaxed" not in called:
            fail_measurement("compositions.yaml uses both creation gates but never calls "
                             "osh.depsRelaxed, so which edge is in which gate cannot be read "
                             "from the declared relaxations")
        for var in ("$strictReady", "$relaxedExist"):
            if not re.search(r"if\s+and\b[^\n]*" + re.escape(var), body):
                fail_measurement(f"compositions.yaml computes {var} but the emission condition "
                                 f"does not test it; the creation gate is not what it appears "
                                 f"to be")
        return "per edge: readiness by default, existence where declared", "per-edge"
    if ready_called:
        return "readiness, uniformly (osh.depsReady for every edge)", "readiness"
    return "existence, uniformly (osh.depsExist for every edge)", "existence"


def check_partition(root, comps, declared):
    """Render compositions.yaml's OWN partition expression and check it matches the declarations.

    Everything else here reasons about the relax-set as declared in values.yaml. That leaves one
    gap: the templates could fail to implement the declaration -- drop the partition loop and
    every dependency lands in the strict set with none left to gate, or land in neither. So this
    lifts the partition statements verbatim out of compositions.yaml, runs THEM over
    .Values.components in a throwaway copy of the chart, and compares. Lifting rather than
    reimplementing is the point: a check that reimplements the logic it is checking only proves
    the two copies agree.

    This is also the one part of the creation gate that `helm template` can exercise at all.
    osh.depsRelaxed reads values and never calls `lookup`, so it answers the same offline as it
    does against a live cluster -- unlike osh.ready, osh.exists and osh.paused, which are empty
    under `helm template` and cannot be tested without an API server.
    """
    tpl = root / "blueprints/openstack/chart/templates/compositions.yaml"
    body = tpl.read_text()
    block = re.search(r"(\{\{-\s*\$relaxed\s*:=.*?)\{\{-\s*\$strictReady\s*:=", body, re.S)
    if not block:
        fail_measurement("cannot find the dependency partition in compositions.yaml (the "
                         "statements from `$relaxed :=` to `$strictReady :=`), so what the "
                         "template computes per edge cannot be checked against the declarations")
    debug = ("{{- range $c := .Values.components }}\n"
             + block.group(1)
             + '#PARTITION {{ $c.name }} strict={{ join "," $strict }} '
               'relaxed={{ join "," $relaxed }}\n'
               "{{- end }}\n")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "chart"
        shutil.copytree(root / "blueprints/openstack/chart", work)
        cy = work / "Chart.yaml"
        cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
        toolkit = root / "charts/helm-toolkit"
        if toolkit.is_dir():
            (work / "charts").mkdir(exist_ok=True)
            shutil.copytree(toolkit, work / "charts/helm-toolkit", dirs_exist_ok=True)
        probe = "templates/zz-gate-partition.yaml"
        (work / probe).write_text(debug)
        out = subprocess.run(
            ["helm", "template", "openstack", str(work), "--kube-version", "1.36.0",
             "--show-only", probe],
            capture_output=True, text=True,
        )
    if out.returncode != 0:
        tail = out.stderr.strip().splitlines()
        fail_measurement(f"the umbrella's own partition expression does not render -- "
                         f"{tail[-1][:200] if tail else 'no stderr'}")

    def split_set(field):
        # Compared as sets: the template keeps each component's declared dependency order while
        # this gate reads them sorted, and the gate is about WHICH edge is in which half.
        return tuple(sorted(x for x in field.split(",") if x))

    rendered = {}
    for line in out.stdout.splitlines():
        if line.startswith("#PARTITION "):
            parts = line.split()
            rendered[parts[1]] = (split_set(parts[2].split("=", 1)[1]),
                                  split_set(parts[3].split("=", 1)[1]))
    if not rendered:
        fail_measurement("the umbrella's partition expression rendered no component at all, so "
                         "nothing was compared")

    wrong = []
    for name, deps in sorted(comps.items()):
        want = (tuple(sorted(d for d in deps if (name, d) not in declared)),
                tuple(sorted(d for d in deps if (name, d) in declared)))
        got = rendered.get(name)
        if got is None:
            wrong.append(f"{name}: the template partitioned no dependencies for it")
        elif got != want:
            wrong.append(f"{name}: template gates strict={list(got[0])} relaxed={list(got[1])}, "
                         f"but the declarations say strict={list(want[0])} "
                         f"relaxed={list(want[1])}")
    if wrong:
        fail_measurement("compositions.yaml does not gate the edges the declarations say it "
                         "does, so every check below is about a gate that is not the one that "
                         "will run:\n  " + "\n  ".join(wrong))
    return len(rendered)


def render(args):
    """helm template one component chart, exactly as validate-component-dag.py does."""
    root, name = args
    chart = root / "blueprints" / name / "chart"
    if not (chart / "Chart.yaml").is_file():
        return name, None, f"no chart at blueprints/{name}/chart"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "chart"
        shutil.copytree(chart, work)
        cy = work / "Chart.yaml"
        cy.write_text(cy.read_text().replace("CHART_VERSION", "0.0.0-gate"))
        toolkit = root / "charts/helm-toolkit"
        if toolkit.is_dir():
            (work / "charts").mkdir(exist_ok=True)
            shutil.copytree(toolkit, work / "charts/helm-toolkit", dirs_exist_ok=True)
        out = subprocess.run(
            # --kube-version for the same reason the DAG gate passes it: a kubeVersion-constrained
            # chart renders nothing without it, and a gate that accepts an empty render accepts
            # anything.
            ["helm", "template", name, str(work), "--kube-version", "1.36.0"],
            capture_output=True, text=True,
        )
    if out.returncode != 0:
        tail = out.stderr.strip().splitlines()
        return name, None, f"helm template failed -- {tail[-1][:160] if tail else 'no stderr'}"
    try:
        docs = [d for d in yaml.safe_load_all(out.stdout) if isinstance(d, dict)]
    except yaml.YAMLError as e:
        return name, None, f"rendered output does not parse -- {str(e)[:160]}"
    if not docs:
        return name, None, "rendered 0 objects"
    return name, docs, None


def entrypoint_waits(pod_spec):
    """The Services, pod label sets and jobs a workload's init containers wait for."""
    services, pods, jobs = [], [], []
    for c in (pod_spec.get("initContainers") or []):
        for e in (c.get("env") or []):
            name, value = e.get("name"), e.get("value")
            if not value:
                continue
            if name == "DEPENDENCY_SERVICE":
                # "namespace:service-name", comma separated.
                services += [t.strip().split(":")[-1] for t in str(value).split(",") if t.strip()]
            elif name == "DEPENDENCY_JOBS":
                jobs += [t.strip() for t in str(value).split(",") if t.strip()]
            elif name == "DEPENDENCY_POD_JSON":
                try:
                    for entry in json.loads(value):
                        if entry.get("labels"):
                            pods.append(entry["labels"])
                except (ValueError, AttributeError):
                    pass  # not a measurement this gate rests on; see the module docstring
    return sorted(set(services)), pods, sorted(set(jobs))


def collect(root, names):
    """Render every component chart and reduce it to Services, workloads and their waits."""
    charts, errors = {}, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, docs, err in pool.map(render, [(root, n) for n in names]):
            if err:
                errors.append(f"{name}: {err}")
                continue
            services, workloads = [], []
            for d in docs:
                kind = d.get("kind")
                if kind == "Service":
                    services.append({
                        "name": d["metadata"]["name"],
                        "selector": (d.get("spec") or {}).get("selector") or {},
                    })
                elif kind in WORKLOAD_KINDS:
                    tpl = (d.get("spec") or {}).get("template") or {}
                    svc, pods, jobs = entrypoint_waits((tpl.get("spec") or {}))
                    workloads.append({
                        "kind": kind,
                        "name": d["metadata"]["name"],
                        "labels": (tpl.get("metadata") or {}).get("labels") or {},
                        "services": svc,
                        "pods": pods,
                        "jobs": jobs,
                    })
            charts[name] = {"services": services, "workloads": workloads, "children": len(docs)}
    if errors:
        fail_measurement("a component chart could not be measured, so its waits are invisible:\n  "
                         + "\n  ".join(errors))
    return charts


def resolve(charts):
    """Service name -> (owning component, backing workloads), and the unresolved waits."""
    owners, duplicates = {}, []
    for comp, data in sorted(charts.items()):
        for svc in data["services"]:
            backing = [w for w in data["workloads"]
                       if svc["selector"]
                       and all(w["labels"].get(k) == v for k, v in svc["selector"].items())]
            if svc["name"] in owners and owners[svc["name"]][0] != comp:
                duplicates.append(f"{svc['name']} is created by both {owners[svc['name']][0]} "
                                  f"and {comp}")
            owners.setdefault(svc["name"], (comp, backing))
    if duplicates:
        fail_measurement("a Service name has no single owner, so its waits cannot be attributed:"
                         "\n  " + "\n  ".join(duplicates))

    unresolved = {}
    for comp, data in sorted(charts.items()):
        for w in data["workloads"]:
            for svc in w["services"]:
                owner = owners.get(svc)
                if owner is None:
                    unresolved.setdefault(svc, set()).add(f"{comp}/{w['name']} (no such Service)")
                elif not owner[1]:
                    unresolved.setdefault(svc, set()).add(
                        f"{comp}/{w['name']} ({owner[0]}'s Service {svc} selects no workload "
                        f"this chart renders)")
    undeclared = {s: v for s, v in unresolved.items() if s not in EXTERNAL_SERVICES}
    if undeclared:
        print("FAILED MEASUREMENT: a Service that is waited on resolves to nothing, so this "
              "gate cannot see what waits on it:\n")
        for svc, who in sorted(undeclared.items()):
            print(f"  Service {svc}, waited on by:")
            for w in sorted(who):
                print(f"    {w}")
        print("\nIf it is genuinely not a component of this blueprint, declare it in "
              "EXTERNAL_SERVICES\nwith the reason. If a Service was renamed, the waits that name "
              "the old one are broken.")
        sys.exit(2)
    return owners, unresolved


def comp_node(c):
    return ("composition", c)


def create_node(c):
    return ("creation", c)


def wl_node(c, w):
    return ("workload", c, f"{w['kind']}/{w['name']}")


def label(node):
    if node[0] == "composition":
        return f"composition/{node[1]} is Ready"
    if node[0] == "creation":
        return f"composition/{node[1]} has been created"
    return f"{node[1]}: {node[2]} is ready"


def build_graph(charts, comps, relaxed_edges, workloads, owners):
    """The wait graph. An edge A -> B reads "A cannot become ready until B is".

    `relaxed_edges` is the set of (component, dependency) pairs gated on existence; every other
    declared edge is gated on readiness.
    """
    graph, why, stats = {}, {}, {"service": 0, "pod": 0, "job": 0,
                                 "pod_unmatched": 0, "job_unmatched": 0}

    def edge(a, b, reason):
        graph.setdefault(a, set()).add(b)
        why.setdefault((a, b), reason)

    for comp, data in sorted(charts.items()):
        for w in data["workloads"]:
            node = wl_node(comp, w)
            edge(comp_node(comp), node,
                 f"{comp}'s Composition reports Ready only when every managed child is ready, "
                 f"and {w['kind']}/{w['name']} is one of its {data['children']}")
            edge(node, create_node(comp),
                 f"{w['name']} cannot run before {comp}'s Composition has been created")
            for svc in w["services"]:
                owner, backing = owners.get(svc, (None, []))
                for b in backing:
                    edge(node, wl_node(owner, b),
                         f"{w['name']}'s kubernetes-entrypoint init container waits for Service "
                         f"{svc} to have endpoints, which {owner}'s {b['kind']}/{b['name']} backs")
                    stats["service"] += 1
            for labels in w["pods"]:
                matched = [(c, x) for c in sorted(workloads) for x in workloads[c].values()
                           if all(x["labels"].get(k) == v for k, v in labels.items())]
                if not matched:
                    stats["pod_unmatched"] += 1
                for c, x in matched:
                    edge(node, wl_node(c, x),
                         f"{w['name']} waits for a ready pod labelled "
                         f"{','.join(f'{k}={v}' for k, v in sorted(labels.items()))}, which "
                         f"{c}'s {x['kind']}/{x['name']} provides")
                    stats["pod"] += 1
            for job in w["jobs"]:
                target = workloads.get(comp, {}).get(job)
                if target is None or target["kind"] != "Job":
                    stats["job_unmatched"] += 1
                    continue
                edge(node, wl_node(comp, target), f"{w['name']} waits for job {job} to complete")
                stats["job"] += 1
        for dep in comps[comp]:
            if (comp, dep) in relaxed_edges:
                edge(create_node(comp), create_node(dep),
                     f"creation of {comp}'s Composition is gated on {dep}'s Composition merely "
                     f"EXISTING -- a declared relaxation (osh.depsExist)")
            else:
                edge(create_node(comp), comp_node(dep),
                     f"creation of {comp}'s Composition is gated on {dep} reporting Ready=True "
                     f"(osh.depsReady)")
    return graph, why, stats


def sccs_of(graph):
    """Tarjan's strongly connected components, iteratively."""
    index, low, on_stack, stack, out = {}, {}, set(), [], []

    def strongconnect(root_node):
        work = [(root_node, iter(sorted(graph.get(root_node, ()))))]
        index[root_node] = low[root_node] = len(index)
        stack.append(root_node)
        on_stack.add(root_node)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = len(index)
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                scc = []
                while True:
                    m = stack.pop()
                    on_stack.discard(m)
                    scc.append(m)
                    if m == node:
                        break
                out.append(scc)

    for node in sorted(graph) + sorted({m for v in graph.values() for m in v}):
        if node not in index:
            strongconnect(node)
    return out


def cycles_of(graph):
    """The strongly connected components that are actually cycles."""
    return [scc for scc in sccs_of(graph)
            if len(scc) > 1 or (scc and scc[0] in graph.get(scc[0], ()))]


def witness(graph, scc):
    """Shortest cycle back to one member, as a list of nodes: a path reads, a node set does not."""
    members, start = set(scc), sorted(scc)[0]
    prev, queue = {}, [start]
    while queue:
        node = queue.pop(0)
        for child in sorted(graph.get(node, ())):
            if child not in members:
                continue
            if child == start:
                path = [node]
                while path[-1] != start:
                    path.append(prev[path[-1]])
                return list(reversed(path)) + [start]
            if child not in prev:
                prev[child] = node
                queue.append(child)
    return [start, start]


def main(argv):
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    comps, declared = read_umbrella(root)
    gate_desc, mode = read_gate(root)
    charts = collect(root, sorted(comps))
    owners, unresolved = resolve(charts)
    workloads = {c: {w["name"]: w for w in d["workloads"]} for c, d in charts.items()}
    total_workloads = sum(len(w) for w in workloads.values())
    if not total_workloads or not owners:
        fail_measurement(f"rendered {total_workloads} workloads and {len(owners)} Services; "
                         f"there is nothing here to find a deadlock in")

    all_edges = {(c, d) for c, deps in comps.items() for d in deps}
    # The relax-set the TEMPLATES actually apply, which is not always the declared one: a revert
    # to a uniform gate ignores the declarations, and this gate has to model what will run.
    partitioned = 0
    if mode == "per-edge":
        partitioned = check_partition(root, comps, declared)
        effective = set(declared)
    elif mode == "existence":
        effective = set(all_edges)
    else:
        effective = set()

    graph, why, stats = build_graph(charts, comps, effective, workloads, owners)
    if not stats["service"]:
        fail_measurement("not one workload waits on a Service owned by a component; either the "
                         "charts stopped declaring their dependencies or this gate stopped "
                         "reading them")

    problems = []

    # 1. Every cycle must be broken by a declared relaxation. Any cycle left is a deadlock.
    for scc in sorted(cycles_of(graph), key=lambda s: sorted(s)):
        path = witness(graph, scc)
        members = sorted({n[1] for n in scc})
        candidates = sorted({(a[1], b[1]) for a in scc for b in graph.get(a, ())
                             if a[0] == "creation" and b[0] == "composition" and b in set(scc)})
        lines = ["  " + " -> ".join(label(n) for n in path)]
        for a, b in zip(path, path[1:]):
            lines.append(f"      {label(a)}")
            lines.append(f"        waits for {label(b)}: {why[(a, b)]}")
        lines.append(f"      components involved: {', '.join(members)}")
        lines.append(f"      {len(scc)} nodes are stuck in this cycle, not just "
                     f"{len(path) - 1} on the path above")
        if candidates:
            lines.append("      readiness is unreachable on these creation edges, so relaxing "
                         "one of them to")
            lines.append("      existence breaks the cycle; declare it in "
                         "dependencyRelaxations with this reasoning:")
            for c, d in candidates:
                lines.append(f"        {c} -> {d}")
        else:
            lines.append("      NO creation edge is inside this cycle, so no relaxation can "
                         "break it: the cycle is")
            lines.append("      entirely between chart startup waits and has to be fixed in the "
                         "charts.")
        problems.append(("an unbroken readiness cycle", "\n".join(lines)))

    # 2. Every declared relaxation must still be load-bearing. Tighten it alone and require that
    #    a cycle reappears; if none does, readiness is reachable on that edge and ordering is
    #    being discarded for nothing.
    stale = []
    for edge in sorted(effective):
        tightened = effective - {edge}
        probe, _, _ = build_graph(charts, comps, tightened, workloads, owners)
        if not cycles_of(probe):
            stale.append(edge)
    if stale:
        shown = stale[:12]
        lines = [f"  {len(stale)} relaxation(s) are not inside any cycle, so readiness is "
                 f"reachable on them and",
                 "  gating them on existence discards ordering for nothing:"]
        for c, d in shown:
            lines.append(f"      {c} -> {d}")
        if len(stale) > len(shown):
            lines.append(f"      ... and {len(stale) - len(shown)} more")
        if mode == "existence":
            lines.append("  The gate is uniform existence, so EVERY edge is relaxed. Readiness "
                         "is only unreachable")
            lines.append("  inside a cycle; relax those edges and leave the rest strict.")
        else:
            lines.append("  Remove them from dependencyRelaxations. If one was protecting "
                         "something this gate")
            lines.append("  cannot see, say what in the entry and this gate needs to learn to "
                         "check it.")
        problems.append(("a relaxation that no longer breaks a cycle", "\n".join(lines)))

    if problems:
        print(f"FAIL: {len(problems)} problem(s) with the creation gate. Creation gate in use: "
              f"{gate_desc}.\n")
        for title, detail in problems:
            print(f"{title}:\n{detail}\n")
        print("Nothing in a readiness cycle can become ready until something waiting on it does, "
              "and no\ncomponent reports an error while it happens -- the install just stays "
              "partly finished, which\nis also what a healthy install looks like for its first "
              "minutes. See #110 and the header of\nblueprints/openstack/chart/templates/"
              "compositions.yaml.")
        return 1

    # Informational: an edge gated on existence and backed by no entrypoint wait is sequenced by
    # nothing at all. Not a failure -- a relaxation can be necessary and unbacked at once -- but
    # it is the number that says what the relaxation actually gave up.
    unbacked = []
    for comp, deps in sorted(comps.items()):
        for dep in deps:
            backed = False
            for w in charts[comp]["workloads"]:
                for svc in w["services"]:
                    if owners.get(svc, (None, []))[0] == dep and owners[svc][1]:
                        backed = True
                for labels in w["pods"]:
                    if any(all(x["labels"].get(k) == v for k, v in labels.items())
                           for x in workloads[dep].values()):
                        backed = True
            if not backed:
                unbacked.append((comp, dep))

    print(f"OK: no readiness deadlock, and every relaxation is load-bearing. {len(graph)} nodes, "
          f"{sum(len(v) for v in graph.values())} wait edges across {len(charts)} components "
          f"({total_workloads} workloads, {len(owners)} Services).")
    print(f"    creation gate: {gate_desc}; {stats['service']} Service waits, {stats['pod']} pod "
          f"waits, {stats['job']} job waits.")
    if partitioned:
        print(f"    the templates' own partition expression puts every edge of all "
              f"{partitioned} components in the gate the declarations name.")
    print(f"    {len(all_edges) - len(effective)} of {len(all_edges)} dependency edges gated on "
          f"readiness, {len(effective)} on existence:")
    for c, d in sorted(effective):
        note = "no entrypoint wait backs it either" if (c, d) in unbacked \
            else "the dependent still waits for it at startup"
        print(f"      {c} -> {d} ({note})")
    print(f"    {len(all_edges) - len(unbacked)} of {len(all_edges)} edges are backed by an "
          f"entrypoint wait; {len(unbacked)} are not: "
          f"{', '.join(f'{c}->{d}' for c, d in unbacked) or 'none'}.")
    print(f"    declared external Services: {', '.join(sorted(unresolved)) or 'none'}. "
          f"Unmatched pod selectors: {stats['pod_unmatched']}, unmatched job names: "
          f"{stats['job_unmatched']} (not this gate's subject; see its docstring).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
