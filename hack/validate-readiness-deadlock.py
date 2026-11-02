#!/usr/bin/env python3
"""No component may be unable to become ready because of something it is itself holding back.

hack/validate-component-dag.py proves the DECLARED graph is closed, acyclic and renderable. That
is not enough, and #110 is why. The declared DAG stayed acyclic the whole time the compute tier
was deadlocked on a 4-node GKE install of profile=full:

    neutron  deps = [keystone, rabbitmq, openvswitch]                        <- nova not declared
    nova     deps = [keystone, placement, rabbitmq, glance, neutron, libvirt]

while neutron's dhcp, l3 and metadata agents sat in Init:0/2 logging "Resolving dependency
Service nova-api", because openstack-helm's own charts order their startup with
kubernetes-entrypoint and those waits do not follow this umbrella's DAG. With Composition
creation gated on dependency READINESS, neutron could not become Ready until nova-api existed
and nova was not created until neutron was Ready. Neither moved, and nothing reported an error:
3 of neutron's 73 managed children were simply not ready, forever.

So the cycle was real but it was not in the declared graph. It ran through two dimensions at
once -- a chart's runtime wait for a Service, and the umbrella's wait for a Composition -- and
a gate that looks at either dimension alone cannot see it. This one builds the graph of
everything that waits on everything else and looks for a cycle in the whole of it:

    composition/X is Ready        <- every managed child of X is ready
    child workload of X           <- X's Composition has been created
                                  <- every Service in its DEPENDENCY_SERVICE has endpoints,
                                     which means the workloads backing that Service are ready
                                  <- every pod in its DEPENDENCY_POD_JSON is ready
                                  <- every job in its DEPENDENCY_JOBS has completed
    creation of X                 <- per compositions.yaml's gate, each dependency either
                                     EXISTS or is READY -- which one is read from the chart

That last line is the point. Under readiness gating "creation of X" waits on
"composition/dep is Ready" and the neutron/nova cycle closes; under existence gating it waits
on "creation of dep", which still orders creation but can never be closed by anything a chart
does at runtime. The gate therefore reads the umbrella's own templates to decide which edges
to draw, so it fires on the tree that deadlocks and passes on the tree that does not. Reverting
the fix, in either file, puts the readiness edges back and this gate fails.

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
missing dependency, an unrecognised creation gate or a graph with no edges all exit non-zero.
Four gates in this repo printed "SKIP: PyYAML unavailable" and exited 0 for weeks (#120).

What this gate does NOT claim. It judges the shipped chart defaults; a workload that only the
umbrella's componentValues enables is not rendered and not measured. It says nothing about
waits that resolve to nothing -- an unmatched DEPENDENCY_POD_JSON selector or a DEPENDENCY_JOBS
name no manifest renders is a forever-wait of a different kind, counted and printed here but
left to its own gate. And a declared dependency with no matching entrypoint wait (nova ->
placement) is now sequenced by nothing at all; that is a deliberate consequence of existence
gating, not a defect this gate reports.
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

# The creation gates this gate knows how to model, by the helper compositions.yaml calls.
#   readiness -> creation of a component waits for each dependency's Composition to be Ready
#   existence -> creation of a component waits only for each dependency's Composition to exist
GATE_SEMANTICS = {
    "osh.depsReady": "readiness",
    "osh.depsExist": "existence",
}

WORKLOAD_KINDS = ("Deployment", "DaemonSet", "StatefulSet", "Job")

try:
    import yaml
except ImportError:
    sys.exit("FAILED MEASUREMENT: PyYAML unavailable, so nothing was checked")


def fail_measurement(msg):
    print(f"FAILED MEASUREMENT: {msg}")
    sys.exit(2)


def read_components(root):
    values = root / "blueprints/openstack/chart/values.yaml"
    if not values.is_file():
        fail_measurement(f"no umbrella values at {values}")
    comps = {}
    for c in (yaml.safe_load(values.read_text()) or {}).get("components") or []:
        comps[c["name"]] = sorted(c.get("deps") or [])
    if not comps:
        fail_measurement("the umbrella declares no components")
    for name, deps in comps.items():
        for d in deps:
            if d not in comps:
                fail_measurement(f"{name} declares dependency {d!r}, which is not a component "
                                 f"(hack/validate-component-dag.py reports this properly)")
    comps.pop("openstack", None)  # the umbrella is not one of its own components
    return comps


def read_gate_semantics(root):
    """Which creation gate compositions.yaml is in, read from the umbrella's own templates."""
    tpl = root / "blueprints/openstack/chart/templates"
    comp_yaml, helpers = tpl / "compositions.yaml", tpl / "_helpers.tpl"
    for f in (comp_yaml, helpers):
        if not f.is_file():
            fail_measurement(f"{f} is missing, so the creation gate cannot be read")
    body = comp_yaml.read_text()

    # The predicate whose result the emission condition tests. Both the assignment and the use
    # have to be found: a predicate that is computed and not tested gates nothing.
    called = sorted(set(re.findall(r'include\s+"(osh\.deps[A-Za-z]*)"', body)))
    if len(called) != 1:
        fail_measurement(f"compositions.yaml calls {called or 'no'} dependency predicate(s); "
                         f"expected exactly one of {sorted(GATE_SEMANTICS)}")
    name = called[0]
    if name not in GATE_SEMANTICS:
        fail_measurement(f"compositions.yaml gates creation on {name!r}, whose semantics this "
                         f"gate does not know. Teach it: add {name!r} to GATE_SEMANTICS as "
                         f"'readiness' or 'existence' in the same change that introduces it")
    if not re.search(r"\$deps\s*:=\s*include\s+\"%s\"" % re.escape(name), body) or \
            not re.search(r"if\s+and\b[^\n]*\$deps", body):
        fail_measurement(f"compositions.yaml computes {name!r} but the emission condition does "
                         f"not test it; the creation gate is not what it appears to be")

    # And what that predicate actually resolves a dependency with, so renaming the readiness
    # predicate to the existence one cannot change the gate's verdict without changing the gate.
    # Comments go first: this repo documents a helper by explaining the one it replaced, and a
    # name in prose must neither satisfy nor trip a check (the same reason
    # hack/validate-upstream-divergences.py strips them).
    source = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", helpers.read_text(), flags=re.S)
    bodies = re.split(r'(?=\{\{-?\s*define\s+")', source)
    body_of = [b for b in bodies if re.match(r'\{\{-?\s*define\s+"%s"' % re.escape(name), b)]
    if len(body_of) != 1:
        fail_measurement(f"_helpers.tpl defines {name!r} {len(body_of)} times; expected once")
    uses_ready = 'include "osh.ready"' in body_of[0]
    uses_exists = 'include "osh.exists"' in body_of[0]
    semantics = GATE_SEMANTICS[name]
    if uses_ready and semantics != "readiness":
        semantics = "readiness"  # redefined under the other name; model what it does, not its name
    elif semantics == "existence" and not uses_exists:
        fail_measurement(f"{name!r} claims existence semantics but resolves dependencies with "
                         f"neither osh.exists nor osh.ready; this gate cannot model it")
    return name, semantics


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


def main(argv):
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    comps = read_components(root)
    gate_name, semantics = read_gate_semantics(root)
    charts = collect(root, sorted(comps))

    # --- resolution: Service name -> the component that owns it and the workloads behind it.
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

    workloads = {c: {w["name"]: w for w in d["workloads"]} for c, d in charts.items()}
    total_workloads = sum(len(w) for w in workloads.values())
    if not total_workloads or not owners:
        fail_measurement(f"rendered {total_workloads} workloads and {len(owners)} Services; "
                         f"there is nothing here to find a deadlock in")

    # Every service name anyone waits on must resolve to a component's workloads, or be declared
    # external. Otherwise the edge disappears and so does any cycle through it.
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

    # --- the graph. An edge A -> B reads "A cannot become ready until B is".
    graph, why = {}, {}

    def edge(a, b, reason):
        graph.setdefault(a, set()).add(b)
        why.setdefault((a, b), reason)

    def comp_node(c):
        return ("composition", c)

    def create_node(c):
        return ("creation", c)

    def wl_node(c, w):
        return ("workload", c, f"{w['kind']}/{w['name']}")

    service_edges = 0
    pod_edges, pod_unmatched = 0, 0
    job_edges, job_unmatched = 0, 0
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
                    service_edges += 1
            for labels in w["pods"]:
                matched = [(c, x) for c in sorted(workloads) for x in workloads[c].values()
                           if all(x["labels"].get(k) == v for k, v in labels.items())]
                if not matched:
                    pod_unmatched += 1
                for c, x in matched:
                    edge(node, wl_node(c, x),
                         f"{w['name']} waits for a ready pod labelled "
                         f"{','.join(f'{k}={v}' for k, v in sorted(labels.items()))}, which "
                         f"{c}'s {x['kind']}/{x['name']} provides")
                    pod_edges += 1
            for job in w["jobs"]:
                target = workloads.get(comp, {}).get(job)
                if target is None or target["kind"] != "Job":
                    job_unmatched += 1
                    continue
                edge(node, wl_node(comp, target),
                     f"{w['name']} waits for job {job} to complete")
                job_edges += 1
        for dep in comps[comp]:
            if semantics == "readiness":
                edge(create_node(comp), comp_node(dep),
                     f"creation of {comp}'s Composition is gated on {dep} reporting Ready=True "
                     f"({gate_name})")
            else:
                edge(create_node(comp), create_node(dep),
                     f"creation of {comp}'s Composition is gated on {dep}'s Composition "
                     f"existing ({gate_name})")

    if not service_edges:
        fail_measurement("not one workload waits on a Service owned by a component; either the "
                         "charts stopped declaring their dependencies or this gate stopped "
                         "reading them")

    # --- cycles. Tarjan's SCCs, then one concrete witness path per cycle, because a list of
    # nodes is not a readable explanation of a deadlock and a path is.
    index, low, on_stack, stack, order = {}, {}, set(), [], []
    sccs = []

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
                sccs.append(scc)

    for node in sorted(graph) + sorted({m for v in graph.values() for m in v}):
        if node not in index:
            order.append(node)
            strongconnect(node)

    def witness(scc):
        """Shortest cycle back to scc[0] inside the component, as a list of nodes."""
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

    def label(node):
        if node[0] == "composition":
            return f"composition/{node[1]} is Ready"
        if node[0] == "creation":
            return f"composition/{node[1]} has been created"
        return f"{node[1]}: {node[2]} is ready"

    cycles = [scc for scc in sccs
              if len(scc) > 1 or (scc and scc[0] in graph.get(scc[0], ()))]
    if cycles:
        print(f"FAIL: {len(cycles)} readiness deadlock(s). Nothing in the cycle can become ready "
              f"until something\nthat is waiting on it does, and no component reports an error "
              f"while it happens -- it just\nstays partly installed, which is also what a "
              f"healthy install looks like for its first minutes.\n")
        print(f"The umbrella gates Composition creation on dependency {semantics.upper()} "
              f"({gate_name}).\n")
        for scc in sorted(cycles, key=lambda s: sorted(s)):
            path = witness(scc)
            print("  " + " -> ".join(label(n) for n in path))
            for a, b in zip(path, path[1:]):
                print(f"      {label(a)}")
                print(f"        waits for {label(b)}: {why[(a, b)]}")
            members = sorted({n[1] for n in scc})
            print(f"      components involved: {', '.join(members)}")
            print(f"      {len(scc)} nodes are stuck in this cycle, not just the "
                  f"{len(path) - 1} on the path above\n")
        if semantics == "readiness":
            print("Gating creation on readiness is what closes these cycles: it turns a chart's "
                  "own soft\nstartup wait into a hard block on a Composition that does not exist "
                  "yet. See #110 and the\nheader of blueprints/openstack/chart/templates/"
                  "compositions.yaml.")
        return 1

    external = ", ".join(sorted(unresolved)) or "none"
    print(f"OK: no readiness deadlock. {len(graph)} nodes, "
          f"{sum(len(v) for v in graph.values())} wait edges across {len(charts)} components "
          f"({total_workloads} workloads, {len(owners)} Services).")
    print(f"    creation gate: dependency {semantics} ({gate_name}); "
          f"{service_edges} Service waits, {pod_edges} pod waits, {job_edges} job waits.")
    print(f"    declared external Services: {external}. "
          f"Unmatched pod selectors: {pod_unmatched}, unmatched job names: {job_unmatched} "
          f"(not this gate's subject; see its docstring).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
