{{/* Cron schedule from a contract interval ("30s" | "5m" | "2h"). Sub-minute
     intervals round up to every minute (cron's floor); minute intervals >= 60
     clamp to hourly. Keeps the serviceContract usage/health interval the single
     source of truth for the emitter CronJobs. */}}
{{- define "osh.cronFromInterval" -}}
{{- $i := . | toString -}}
{{- if hasSuffix "h" $i -}}
{{- $n := trimSuffix "h" $i | int -}}
{{- if le $n 1 -}}0 * * * *{{- else -}}0 */{{ $n }} * * *{{- end -}}
{{- else if hasSuffix "m" $i -}}
{{- $n := trimSuffix "m" $i | int -}}
{{- if le $n 1 -}}* * * * *{{- else if ge $n 60 -}}0 * * * *{{- else -}}*/{{ $n }} * * * *{{- end -}}
{{- else -}}
* * * * *
{{- end -}}
{{- end -}}

{{/* The bare composition.krateo.io version (e.g. "v0-3-1") derived from chartVersion
     (crdgen maps version 0.1.1 -> apiVersion v0-1-1). Single source of truth so
     osh.apiVersion and osh.crdExists can never drift on which version they mean. */}}
{{- define "osh.version" -}}
{{- $top := index . 0 -}}
{{- printf "v%s" ($top.Values.chartVersion | toString | replace "." "-") -}}
{{- end -}}

{{/* The composition.krateo.io apiVersion served by the component CRDs, derived
     from chartVersion (crdgen maps version 0.1.1 -> apiVersion v0-1-1). Keeping
     this in one place means a version bump never strands a hardcoded v0-1-0. */}}
{{- define "osh.apiVersion" -}}
{{- $top := index . 0 -}}
{{- printf "composition.krateo.io/%s" (include "osh.version" (list $top)) -}}
{{- end -}}

{{/* Returns "true" if a Composition of <kind>/<name> reports Ready=True. Takes the cluster's CRD
     list as its fourth argument; compositions.yaml looks that up ONCE per render and threads it
     through everything (see osh.crdExists for the measurement that forced this).

     Guarded by osh.crdExists: a `lookup` of an unregistered group/version *errors* (not nil),
     which would fail the whole render on first install (before the component CRDs exist). The
     guard makes the umbrella self-bootstrapping - on early reconciles deps simply read as
     not-ready until their CRDs appear. */}}
{{- define "osh.ready" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $crds := index . 3 -}}
{{- $r := "" -}}
{{- if eq (include "osh.crdExists" (list $kind (include "osh.version" (list $top)) $crds)) "true" -}}
{{- $o := lookup (include "osh.apiVersion" (list $top)) $kind $top.Release.Namespace $name -}}
{{- if $o -}}{{- range ($o.status.conditions | default list) -}}
{{- /* Reason, not just status. Two of the five conditions this can see carry a status that
       is not False, and they mean opposite things:

         Available                  children healthy              True
         ReconcileGracefullyPaused  an operator stopped it        True     <- not ready
         ZeroEvidence               never evaluated, no inventory Unknown  <- not ready

       An operator pausing reconciliation is saying "do not act on this", and emitting a
       dependent against it acts on it -- the dependent then reconciles against a component
       nobody is maintaining, which is the situation the pause exists to prevent.

       ZeroEvidence was already refused, but by luck: Unknown is not True, and a truthiness
       test happens to reject it. The gate asked "is this true" rather than reasoning about
       which conditions mean ready, and one of the five needed a distinction it did not make. */ -}}
{{- if and (eq .type "Ready") (eq (.status | toString) "True") (ne (.reason | toString) "ReconcileGracefullyPaused") -}}{{- $r = "true" -}}{{- end -}}
{{- end -}}{{- end -}}
{{- end -}}
{{- $r -}}
{{- end -}}

{{/* Returns "true" if an operator has paused reconciliation of the <kind>/<name> Composition.

     osh.ready above already refuses a paused dependency, because Ready=True with reason
     ReconcileGracefullyPaused is not ready (#95, and see its condition table). This exists for
     the edges whose creation gate is EXISTENCE rather than readiness: those edges deliberately
     accept a dependency that is not working, but accepting a dependency an operator has
     switched off is a different thing. "Do not act on this" has to keep meaning "do not build
     on this" -- creating a dependent runs its db_init and db_sync against a component nobody
     is maintaining.

     It cannot reintroduce the deadlock of #110, and that is the whole reason an existence-gated
     edge may still consult it: a pause is driven by an operator and can never be caused by the
     absence of the dependent. Existence and pausing are conditions only the operator and this
     umbrella can change; readiness is a condition the dependency's own charts can hold hostage,
     which is exactly how neutron's agents held nova's creation hostage.

     ZeroEvidence needs no special case here: it is not a pause, and a dependency that exists
     but has never been evaluated is exactly the "exists, not yet ready" case an existence-gated
     edge is meant to let through.

     This never withdraws anything. compositions.yaml consults it only through osh.depsExist,
     whose result is or'd with osh.exists, so pausing a component cannot uninstall the
     components already built on it (#68).

     Guarded by osh.crdExists for the same reason osh.exists is: a `lookup` of an unregistered
     group/version *errors* (not nil), which would fail the whole render on first install,
     before the component CRDs exist. Nothing can be paused before its CRD exists, so the
     guard's "" is also the honest answer. */}}
{{- define "osh.paused" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $crds := index . 3 -}}
{{- $r := "" -}}
{{- if eq (include "osh.crdExists" (list $kind (include "osh.version" (list $top)) $crds)) "true" -}}
{{- $o := lookup (include "osh.apiVersion" (list $top)) $kind $top.Release.Namespace $name -}}
{{- if $o -}}{{- range ($o.status.conditions | default list) -}}
{{- if and (eq .type "Ready") (eq (.reason | toString) "ReconcileGracefullyPaused") -}}{{- $r = "true" -}}{{- end -}}
{{- end -}}{{- end -}}
{{- end -}}
{{- $r -}}
{{- end -}}

{{/* Returns "true" if a generated CRD for <kind> in composition.krateo.io exists AND
     serves the target <version> (e.g. "v0-3-1"). Matched by Kind (not a guessed
     plural), so pluralization can never bite. Version-aware so that during a
     chart-version bump - when the CRD still only serves the OLD version - the guard
     stays false instead of green-lighting a CR at a version nothing serves yet
     (which would 500 the render: "no matches for kind ... ensure CRDs are installed
     first").

     The CRD list is a PARAMETER and this function must never look it up. compositions.yaml
     fetches it once, above the component range, and threads it through every helper that needs
     it. That is the whole reason osh.ready, osh.paused, osh.exists, osh.depsReady and
     osh.depsExist all carry an extra argument they otherwise would not need.

     Why it matters: this is called once per component for the component's own kind, once more
     through osh.exists, and once per dependency edge through osh.ready / osh.exists /
     osh.paused. For profile=full that is 154 calls in a single render pass -- 34 components x 2,
     plus 84 readiness-gated edges, plus 2 for the one existence-gated edge -- and it grows with
     the DAG, so every component or edge added made it worse. When the fetch lived in here, each
     of those pulled the cluster's ENTIRE CRD list: a 0.3.28 upgrade on a cluster with 257 CRDs,
     several carrying megabyte-scale OpenAPI schemas, spent 13 minutes in its render phase.

     Slow is not the whole cost. A `helm template` from a laptop can simply wait, but
     composition-dynamic-controller renders through chart-inspector under a timeout, so a render
     this slow WEDGES the umbrella instead of merely delaying it. And the cost is paid on every
     release, not just on fresh installs: the chart version is encoded in the composition
     apiVersion, so each bump regenerates every CRD at the new version, the existing CRs stay at
     the old one, and the DAG walk restarts at level zero -- depth x per-pass render, every time.

     The krateo installer chart fixed the identical problem the identical way: on a cluster with
     121 CRDs and 42 large widget schemas, hoisting the lookup took its render from ~55s, which
     blew that same timeout, to a few seconds (D9, 2026-07-08). This cluster has twice the CRDs.

     The per-object lookups in osh.ready / osh.paused / osh.exists are deliberately left alone:
     each is a single GET of one named object, which is cheap. Only the full-list fetch was the
     problem. */}}
{{- define "osh.crdExists" -}}
{{- $kind := index . 0 -}}{{- $version := index . 1 -}}{{- $crds := index . 2 -}}
{{- $found := "" -}}
{{- range ($crds | default list) -}}
{{- if and (eq .spec.group "composition.krateo.io") (eq .spec.names.kind $kind) -}}
{{- range (.spec.versions | default list) -}}
{{- if and (eq .name $version) .served -}}{{- $found = "true" -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $found -}}
{{- end -}}

{{/* Returns "true" if the component's own CR exists at all -- Ready or not.

     This is what makes the dependency gate a precondition for CREATION rather than an
     invariant for CONTINUED EXISTENCE. The gate is a lookup re-evaluated every reconcile,
     so without this a dependency that merely goes unready stops the dependent rendering,
     and a Helm release that stops rendering is a Helm release that gets uninstalled. A
     two-minute MariaDB readiness blip deleted a working Keystone -- Composition, release
     and Deployment -- which is #68.

     Deselection is unaffected: `has $c.name $sel` is checked before this in
     compositions.yaml, so a component removed from the profile is never emitted and cannot
     be resurrected by the fact that it still exists. Removing it stays the way to remove it.

     The CRD check is deliberately NOT latched. If the CRD does not serve the target version
     the CR cannot be rendered at all (the render 500s), so that one must stay a hard gate. */}}
{{- define "osh.exists" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $crds := index . 3 -}}
{{- $r := "" -}}
{{- if eq (include "osh.crdExists" (list $kind (include "osh.version" (list $top)) $crds)) "true" -}}
{{- if lookup (include "osh.apiVersion" (list $top)) $kind $top.Release.Namespace $name -}}{{- $r = "true" -}}{{- end -}}
{{- end -}}
{{- $r -}}
{{- end -}}

{{/* "true" if every dependency Composition (by component name) is Ready. The DEFAULT creation
     gate: it is what sequences a dependent behind a dependency that actually works, and 79 of
     the 80 declared dependency edges are gated this way.

     The fourth argument is the cluster's CRD list, passed down to osh.ready so the whole loop
     reads it from memory instead of fetching it once per dependency. See osh.crdExists. */}}
{{- define "osh.depsReady" -}}
{{- $top := index . 0 -}}{{- $deps := index . 1 -}}{{- $comps := index . 2 -}}{{- $crds := index . 3 -}}
{{- $all := "true" -}}
{{- range $d := $deps -}}
  {{- $kind := "" -}}{{- range $c := $comps -}}{{- if eq $c.name $d -}}{{- $kind = $c.kind -}}{{- end -}}{{- end -}}
  {{- if ne (include "osh.ready" (list $top $kind $d $crds)) "true" -}}{{- $all = "" -}}{{- end -}}
{{- end -}}
{{- $all -}}
{{- end -}}

{{/* "true" if every dependency Composition (by component name) EXISTS and none of them is
     paused. The RELAXED creation gate, used only for the edges listed in
     .Values.dependencyRelaxations; compositions.yaml's header says why those edges cannot be
     gated on readiness (#110).

     Readiness is unreachable on such an edge, not merely slow: the dependency's own charts wait
     at startup for a workload that this umbrella will not create until the dependency is Ready,
     so the two conditions each hold the other open. Existence is the strongest condition left
     that only the operator and this umbrella can satisfy, which is what makes it safe -- no
     chart can hold it hostage.

     Deliberately a separate name from osh.depsReady rather than a redefinition of it. Which
     semantics an edge is in says everything about how the install progresses, so compositions
     .yaml partitions the dependency list and names both predicates at the call site. Reading
     either one in isolation must not be able to mislead.

     The fourth argument is the cluster's CRD list, passed down to both osh.exists and
     osh.paused -- two reads of it per dependency, which is exactly why it is threaded rather
     than fetched. See osh.crdExists. */}}
{{- define "osh.depsExist" -}}
{{- $top := index . 0 -}}{{- $deps := index . 1 -}}{{- $comps := index . 2 -}}{{- $crds := index . 3 -}}
{{- $all := "true" -}}
{{- range $d := $deps -}}
  {{- $kind := "" -}}{{- range $c := $comps -}}{{- if eq $c.name $d -}}{{- $kind = $c.kind -}}{{- end -}}{{- end -}}
  {{- if ne (include "osh.exists" (list $top $kind $d $crds)) "true" -}}{{- $all = "" -}}{{- end -}}
  {{- if eq (include "osh.paused" (list $top $kind $d $crds)) "true" -}}{{- $all = "" -}}{{- end -}}
{{- end -}}
{{- $all -}}
{{- end -}}

{{/* The dependencies of <component> whose creation gate is relaxed to existence, space-joined,
     taken from .Values.dependencyRelaxations. Everything NOT returned here stays gated on
     readiness, so the default is always the strict one: a relaxation has to be declared, with
     the cycle that forces it, to take effect. hack/validate-readiness-deadlock.py checks that
     each declared entry is genuinely inside a cycle and that every cycle has one, so the set
     can neither accrete nor go stale.

     Pure values arithmetic -- no `lookup` -- so unlike every other gate helper this one returns
     the same answer under `helm template` as it does against a live cluster. */}}
{{- define "osh.depsRelaxed" -}}
{{- $top := index . 0 -}}{{- $name := index . 1 -}}{{- $deps := index . 2 -}}
{{- $out := list -}}
{{- range $d := ($deps | default list) -}}
  {{- range $r := ($top.Values.dependencyRelaxations | default list) -}}
    {{- if and (eq $r.component $name) (eq $r.dependency $d) -}}{{- $out = append $out $d -}}{{- end -}}
  {{- end -}}
{{- end -}}
{{- $out | uniq | join " " -}}
{{- end -}}

{{/* The set of component names to deploy, space-joined. If `enabled` is non-empty it
     is an explicit selection expanded to its transitive dependency closure (so
     `enabled: [heat]` pulls in keystone, rabbitmq, mariadb, memcached). Otherwise it
     falls back to the `profile`: identity tier always, plus compute when profile=full. */}}
{{- define "osh.selected" -}}
{{- $top := index . 0 -}}
{{- $comps := $top.Values.components -}}
{{- $enabled := $top.Values.enabled | default list -}}
{{- $sel := dict -}}
{{- if gt (len $enabled) 0 -}}
  {{- range $e := $enabled -}}{{- $_ := set $sel $e true -}}{{- end -}}
  {{/* expand deps; iterate past the max graph depth so the closure is complete */}}
  {{- range $i := until 8 -}}
    {{- range $c := $comps -}}
      {{- if hasKey $sel $c.name -}}
        {{- range $d := ($c.deps | default list) -}}{{- $_ := set $sel $d true -}}{{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- else -}}
  {{- range $c := $comps -}}
    {{- if or (eq $top.Values.profile "full") (eq $c.tier "identity") -}}{{- $_ := set $sel $c.name true -}}{{- end -}}
  {{- end -}}
{{- end -}}
{{- keys $sel | sortAlpha | join " " -}}
{{- end -}}
