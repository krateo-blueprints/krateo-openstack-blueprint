{{- /*
THE CAPABILITY OBJECTS: one ClusterRole per serviceContract capability, plus a
ValidatingAdmissionPolicy and binding per `enforcement: abac` capability.

SOURCE OF TRUTH. This file lives in the umbrella (blueprints/openstack/chart/templates/) and is
copied byte-for-byte into the per-cluster chart (blueprints/openstack/capabilities/templates/)
by hack/gen-capabilities-chart.py, together with the umbrella's serviceContract values and
schema. hack/validate-capability-roles.py fails if the copy differs. Edit it here, then run the
generator.

WHY TWO CHARTS RENDER THE SAME OBJECTS, AND WHY THEY NEVER BOTH OWN ONE
Up to 0.3.46 only the umbrella rendered these, and the umbrella is what an `Openstack`
Composition installs. So they existed only after the first `Openstack` instance did:
  - `openstack-provision` grants `create openstacks`, and did not exist until an `openstacks`
    object did, so it could never authorize the create of the instance;
  - the compute-tier guard matches CREATE and was installed by that create's own release, so it
    never saw the one create that mattered.
From 0.3.47 the per-cluster chart `openstack-capabilities` owns them. An administrator installs
it once per cluster, before any instance, beside the CompositionDefinitions. It renders every
object unconditionally (mode "standalone").

The umbrella keeps this file only to hand the objects over without a gap (mode "handoff"). On a
cluster upgraded from 0.3.46 its release owns them. If 0.3.47 simply stopped rendering them,
Helm would delete them on the upgrade, and the guard would be absent until someone installed
the new chart. So in handoff mode the umbrella re-renders an object only while the live object
is still owned by THIS release (its `meta.helm.sh/release-name`/`-namespace` annotations), and
renders nothing it does not own: nothing on a fresh cluster, nothing once the per-cluster chart
has taken ownership. Two Helm releases therefore never claim the same object.

EVERY COPY CARRIES `helm.sh/resource-policy: keep`. That is what makes the handoff gap-free in
either order:
  - per-cluster chart first (`helm upgrade --install --take-ownership`): ownership moves in
    place, the live objects now carry `keep`, and when the umbrella next reconciles it no longer
    owns them, stops rendering them, and Helm's upgrade skips the delete, because Helm reads
    `keep` from the LIVE object when it prunes;
  - umbrella first: it still owns them, so it re-renders them unchanged except for `keep`.
    The per-cluster chart then takes ownership as above.
It also means that uninstalling either release leaves the guard in place. Removing it is an
explicit `kubectl delete`, never a side effect.

MODES, and why the live state is an argument rather than a lookup in here:
  (list $top "standalone" dict)  render every object
  (list $top "handoff" $live)    render an object only when $live holds it and it is owned by
                                 $top's release. $live comes from capabilities.liveObjects.
Taking $live as an argument makes the handoff decision a pure function of its inputs, so
hack/validate-capability-roles.py can feed it an owned object, a foreign one and an absent one
and check what renders. A `lookup` in here would return nothing under `helm template` and leave
the handoff branch untestable offline.
*/ -}}

{{- define "capabilities.objectName" -}}
{{- printf "%s-%s" (index . 0).Values.serviceContract.service.name (index . 1).name -}}
{{- end -}}

{{- /* "true" when $obj is a live object whose Helm ownership annotations name $top's release
     and namespace; "" for nil, for an object without the annotations, and for an object owned
     by any other release (the per-cluster chart after it took ownership, or a different
     namespace's umbrella). */ -}}
{{- define "capabilities.ownedHere" -}}
{{- $top := index . 0 -}}
{{- $obj := index . 1 -}}
{{- if $obj -}}
{{- $a := (($obj).metadata).annotations | default dict -}}
{{- if and (eq (index $a "meta.helm.sh/release-name" | toString) $top.Release.Name) (eq (index $a "meta.helm.sh/release-namespace" | toString) $top.Release.Namespace) -}}
true
{{- end -}}
{{- end -}}
{{- end -}}

{{- /* The live copies of every capability object, keyed "<Kind>/<name>". Only the umbrella calls
     this. Six named GETs per render on the default contract (four ClusterRoles, one policy, one
     binding), all cluster-scoped and all by name.

     Why these lookups cannot abort the render: a refused `lookup` is a template error, not an
     empty result (see osh.nodeReadGranted). The render identity under Krateo is
     composition-dynamic-controller's service account, whose role core-provider generates from
     chart-inspector, and chart-inspector traces every API request the chart makes during a
     server-side render, lookups included -- which is why the CRD lookup in compositions.yaml and
     the ClusterRole/ClusterRoleBinding lookups in osh.nodeReadGranted already work. These are
     traced the same way. A NotFound is the empty result, which is the fresh-cluster case. */ -}}
{{- define "capabilities.liveObjects" -}}
{{- $top := . -}}
{{- $live := dict -}}
{{- range $cap := $top.Values.serviceContract.capabilities -}}
{{- $name := include "capabilities.objectName" (list $top $cap) -}}
{{- $o := lookup "rbac.authorization.k8s.io/v1" "ClusterRole" "" $name -}}
{{- if $o -}}{{- $_ := set $live (printf "ClusterRole/%s" $name) $o -}}{{- end -}}
{{- if eq $cap.enforcement "abac" -}}
{{- $p := lookup "admissionregistration.k8s.io/v1" "ValidatingAdmissionPolicy" "" $name -}}
{{- if $p -}}{{- $_ := set $live (printf "ValidatingAdmissionPolicy/%s" $name) $p -}}{{- end -}}
{{- $b := lookup "admissionregistration.k8s.io/v1" "ValidatingAdmissionPolicyBinding" "" $name -}}
{{- if $b -}}{{- $_ := set $live (printf "ValidatingAdmissionPolicyBinding/%s" $name) $b -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- toJson $live -}}
{{- end -}}

{{- /* "true" when the object <kind>/<name> is to be rendered in this mode. */ -}}
{{- define "capabilities.renders" -}}
{{- $top := index . 0 -}}
{{- $mode := index . 1 -}}
{{- $live := index . 2 -}}
{{- $key := index . 3 -}}
{{- if eq $mode "standalone" -}}
true
{{- else if eq $mode "handoff" -}}
{{- if include "capabilities.ownedHere" (list $top (index $live $key)) -}}
true
{{- end -}}
{{- else -}}
{{- fail (printf "capabilities.render: mode must be \"standalone\" or \"handoff\", got %q" $mode) -}}
{{- end -}}
{{- end -}}

{{- define "capabilities.render" -}}
{{- $top := index . 0 -}}
{{- $mode := index . 1 -}}
{{- $live := index . 2 | default dict -}}
{{- $svc := $top.Values.serviceContract.service.name -}}
{{- range $cap := $top.Values.serviceContract.capabilities }}
{{- $name := include "capabilities.objectName" (list $top $cap) }}
{{- /*
Each capability renders one ClusterRole:
  - labeled krateo.io/capability=true so a role composer can list it
  - labeled rbac.krateo.io/aggregate-to-<role>=true so predefined roles
    (built with aggregationRule) absorb it automatically on install
  - annotated with title / enforcement layer / required ACR for the catalog
*/ -}}
{{- if include "capabilities.renders" (list $top $mode $live (printf "ClusterRole/%s" $name)) }}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {{ $name }}
  labels:
    krateo.io/capability: "true"
    # The catalog shows this when present and falls back to the role name.
    # Stable across a title change, which the title is not.
    krateo.io/capability-id: {{ $name | quote }}
    app.kubernetes.io/name: {{ $svc }}
    app.kubernetes.io/managed-by: {{ $top.Release.Service }}
    {{- range $cap.aggregateTo }}
    rbac.krateo.io/aggregate-to-{{ . }}: "true"
    {{- end }}
  annotations:
    helm.sh/resource-policy: keep
    krateo.io/title: {{ $cap.title | quote }}
    krateo.io/enforcement: {{ $cap.enforcement | quote }}
    {{- with $cap.description }}
    krateo.io/description: {{ . | quote }}
    {{- end }}
    {{- with $cap.requiresAcr }}
    krateo.io/requires-acr: {{ . | quote }}
    {{- end }}
    {{- with $cap.crossOrgRole }}
    krateo.io/cross-org-role: {{ . | quote }}
    {{- end }}
rules:
  {{- toYaml $cap.rules | nindent 2 }}
{{- end }}
{{- /*
Capabilities with enforcement=abac carry a CEL rule that RBAC cannot express: RBAC authorizes
on verb + resource and never sees the object's contents, so a condition like "profile=full
requires an approval annotation" has to be enforced at admission.

Rendered as a built-in ValidatingAdmissionPolicy rather than a Kyverno ClusterPolicy. The rule
is already plain CEL, and admissionregistration.k8s.io/v1 is a core API (GA since 1.30), so:

  - no third-party dependency, and nothing to install before the guard works
  - the guard CANNOT silently disappear. This template previously opened with a check for
    kyverno.io/v1, so on a cluster without Kyverno it rendered NOTHING -- a security guard
    that failed open, with no signal that it was absent
  - kyverno.io/v1 ClusterPolicy is deprecated; this avoids a migration rather than performing one

matchConstraints is derived from the capability's own `rules` (apiGroups/resources/verbs) rather
than from `abac.match.kinds`, because ValidatingAdmissionPolicy matches on plural resources and
the rules already carry them -- no Kind-to-plural guessing.
*/ -}}
{{- if eq $cap.enforcement "abac" }}
{{- $ops := list }}
{{- range $cap.rules }}
  {{- range .verbs }}
    {{- if eq . "create" }}{{- $ops = append $ops "CREATE" }}
    {{- else if or (eq . "update") (eq . "patch") }}{{- $ops = append $ops "UPDATE" }}
    {{- else if eq . "delete" }}{{- $ops = append $ops "DELETE" }}
    {{- end }}
  {{- end }}
{{- end }}
{{- if include "capabilities.renders" (list $top $mode $live (printf "ValidatingAdmissionPolicy/%s" $name)) }}
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: {{ $name }}
  labels:
    krateo.io/capability: "true"
    app.kubernetes.io/name: {{ $svc }}
    app.kubernetes.io/managed-by: {{ $top.Release.Service }}
  annotations:
    helm.sh/resource-policy: keep
    krateo.io/title: {{ $cap.title | quote }}
    krateo.io/enforcement: {{ $cap.enforcement | quote }}
spec:
  # Fail: an evaluation error denies the request. The point of this guard is that it cannot be
  # absent, so it must not be bypassable by making it error either.
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      {{- range $cap.rules }}
      - apiGroups:
          {{- toYaml .apiGroups | nindent 10 }}
        apiVersions:
          - "*"
        operations:
          {{- toYaml ($ops | uniq | sortAlpha) | nindent 10 }}
        resources:
          {{- toYaml .resources | nindent 10 }}
      {{- end }}
  validations:
    - expression: {{ $cap.abac.celExpression | quote }}
      {{- with $cap.abac.message }}
      message: {{ . | quote }}
      {{- end }}
{{- end }}
{{- if include "capabilities.renders" (list $top $mode $live (printf "ValidatingAdmissionPolicyBinding/%s" $name)) }}
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: {{ $name }}
  labels:
    krateo.io/capability: "true"
    app.kubernetes.io/name: {{ $svc }}
    app.kubernetes.io/managed-by: {{ $top.Release.Service }}
  annotations:
    helm.sh/resource-policy: keep
spec:
  policyName: {{ $name }}
  validationActions:
    - Deny
{{- end }}
{{- end }}
{{- end }}
{{- end -}}
