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

{{/* Returns "true" if a Composition of <kind>/<name> reports Ready=True. Guarded by
     osh.crdExists: a `lookup` of an unregistered group/version *errors* (not nil), which
     would fail the whole render on first install (before the component CRDs exist). The
     guard makes the umbrella self-bootstrapping - on early reconciles deps simply read as
     not-ready until their CRDs appear. */}}
{{- define "osh.ready" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}
{{- $r := "" -}}
{{- if eq (include "osh.crdExists" (list $kind (include "osh.version" (list $top)))) "true" -}}
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

{{/* Returns "true" if a generated CRD for <kind> in composition.krateo.io exists AND
     serves the target <version> (e.g. "v0-3-1"). Matched by Kind (not a guessed
     plural), so pluralization can never bite. Version-aware so that during a
     chart-version bump - when the CRD still only serves the OLD version - the guard
     stays false instead of green-lighting a CR at a version nothing serves yet
     (which would 500 the render: "no matches for kind ... ensure CRDs are installed
     first"). */}}
{{- define "osh.crdExists" -}}
{{- $kind := index . 0 -}}{{- $version := index . 1 -}}
{{- $found := "" -}}
{{- range (lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "").items -}}
{{- if and (eq .spec.group "composition.krateo.io") (eq .spec.names.kind $kind) -}}
{{- range (.spec.versions | default list) -}}
{{- if and (eq .name $version) .served -}}{{- $found = "true" -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $found -}}
{{- end -}}

{{/* "true" if every dependency Composition (by component name) is Ready. */}}
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
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}
{{- $r := "" -}}
{{- if eq (include "osh.crdExists" (list $kind (include "osh.version" (list $top)))) "true" -}}
{{- if lookup (include "osh.apiVersion" (list $top)) $kind $top.Release.Namespace $name -}}{{- $r = "true" -}}{{- end -}}
{{- end -}}
{{- $r -}}
{{- end -}}

{{- define "osh.depsReady" -}}
{{- $top := index . 0 -}}{{- $deps := index . 1 -}}{{- $comps := index . 2 -}}
{{- $all := "true" -}}
{{- range $d := $deps -}}
  {{- $kind := "" -}}{{- range $c := $comps -}}{{- if eq $c.name $d -}}{{- $kind = $c.kind -}}{{- end -}}{{- end -}}
  {{- if ne (include "osh.ready" (list $top $kind $d)) "true" -}}{{- $all = "" -}}{{- end -}}
{{- end -}}
{{- $all -}}
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
