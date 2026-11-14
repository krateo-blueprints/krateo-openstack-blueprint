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

{{/* The bare composition.krateo.io version (e.g. "v0-3-1") of THIS chart: .Chart.Version,
     which the release stamps into Chart.yaml (crdgen maps version 0.1.1 -> apiVersion v0-1-1).
     Single source of truth so osh.apiVersion and osh.crdExists can never drift on which
     version they mean.

     Never a value. Up to 0.3.37 this read .Values.chartVersion, and values.schema.json
     defaulted that field to the release version. The schema becomes the CRD, and the API
     server writes a default into the object when it is created, so every Composition held
     the version it was born at as if a user had typed it. Every later umbrella then rendered
     its components at that old version: measured on GKE upgrading 0.3.34 to 0.3.36, the
     umbrella moved and all 33 components stayed at 0.3.34 (#179). A stored object must not
     be able to choose the version, so the version is not in the values at all.
     hack/validate-component-version-follows-chart.py renders the chart with the stale value a
     0.3.34 Composition still carries and asserts it is ignored. */}}
{{- define "osh.version" -}}
{{- $top := index . 0 -}}
{{- printf "v%s" ($top.Chart.Version | toString | replace "." "-") -}}
{{- end -}}

{{/* The composition.krateo.io apiVersion at an EXPLICIT version ("v0-3-28" ->
     "composition.krateo.io/v0-3-28"). The group prefix is written here and nowhere else, so
     osh.apiVersion below -- the CURRENT version -- and the Composition emission, which may be
     at an older one -- can never disagree about what a version string means. */}}
{{- define "osh.apiVersionAt" -}}
{{- printf "composition.krateo.io/%s" (index . 0 | toString) -}}
{{- end -}}

{{/* The composition.krateo.io apiVersion served by the component CRDs, derived
     from the chart's own version (crdgen maps version 0.1.1 -> apiVersion v0-1-1). Keeping
     this in one place means a version bump never strands a hardcoded v0-1-0.

     This is "the version this chart IS". NOTHING IN THIS FILE CALLS IT, and nothing should.

     The sentence that used to stand here said it was "the right answer for every per-object
     `lookup` in this file". That sentence was the instruction that produced #166: every such
     lookup asked about the chart's current version, no CRD serves that version at the instant
     a bump renders, and the emission gate closed for all 34 components and pruned their live
     CRs. Per-object lookups now resolve through osh.servedVersion, and the version a component
     is emitted at comes from osh.emitVersion.

     It survives for exactly one consumer: hack/validate-origin-version-pin.py substitutes this
     call for the emitted apiVersion to render the deliberately UNPINNED counterfactual it compares
     the pinned render against. Deleting it would break that differential. So it is kept as the
     thing a gate needs to be able to name, not as a helper to reach for -- if you are reading
     this because you wanted a version to look an object up at, you want osh.servedVersion. */}}
{{- define "osh.apiVersion" -}}
{{- $top := index . 0 -}}
{{- include "osh.apiVersionAt" (list (include "osh.version" (list $top))) -}}
{{- end -}}

{{/* The annotation core-provider's engine stamps on a Composition the first time it reconciles
     it, recording the served version the object was CREATED at ("v0-3-28").

     Write-once by construction, and that is its whole value: pkg/meta.SetOriginVersion returns
     early when the key is already present, so a later reconcile through a re-pointed endpoint
     cannot overwrite it. Nothing else on the object answers "where did this come from" --
     .apiVersion is whatever endpoint delivered the object just now, and the sibling label
     krateo.io/composition-version is re-stamped on EVERY write by the cluster-wide
     krateo-composition-version MutatingAdmissionPolicy, which is exactly what makes it useless
     as an origin.

     A literal here rather than at the call site, and read back by
     hack/validate-origin-version-pin.py from this one definition, for the same reason
     osh.nodeReaderName is a literal: it is an agreement with another component, and a rename on
     either side produces no error anywhere. The umbrella would simply stop finding a stamp,
     fall back to the current version on every component, and silently stop pinning. Confirmed against core-provider
     pkg/meta/meta.go (AnnotationKeyOriginVersion) and against a live CR on a0-openstack-d3. */}}
{{- define "osh.originVersionAnnotation" -}}
krateo.io/composition-origin-version
{{- end -}}

{{/* The version an EXISTING <kind>/<name> Composition was created at, read from its origin
     annotation, or "" when no such object exists or it carries no stamp.

     One named GET of one object, guarded by osh.crdExists exactly as osh.exists is and for the
     same reason: a `lookup` of an unregistered group/version ERRORS rather than returning nil,
     which would fail the whole render on first install. The CRD list is the parameter this
     file threads everywhere; nothing here fetches it. See osh.crdExists for the 13-minute
     measurement that made that a rule.

     It is a second GET of the object osh.exists already fetched, and that is deliberate rather
     than overlooked. The alternative -- hoisting one `lookup` per component into
     compositions.yaml and deriving both answers from it -- would make the component's own
     existence test a different piece of code from the one every DEPENDENCY edge uses, which is
     the kind of divergence this file has paid for before. One extra named GET per SELECTED
     component is 34 in the largest profile, against the 154 crdExists calls and the ~120 named
     GETs the dependency walk already makes; osh.crdExists records that the named GETs were
     never the cost, the full-list fetch was.

     Read through osh.servedVersion, a version the cluster serves. That is safe for this
     purpose even when that version's schema is narrower than the object's own: the apiserver's structural pruning applies to the schema'd spec, and
     metadata.annotations is not schema'd. The stamp is readable through any served version. */}}
{{- define "osh.originVersion" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $crds := index . 3 -}}
{{- $v := "" -}}
{{- /* Read through a version the cluster SERVES, not the chart's current version. This guard
       used to be osh.crdExists at the current version, which made this return "" at the bump
       instant -- so osh.pinVersion fell back to current and the origin pin was inert in exactly
       the instant it was written for. The stamp is an annotation, annotations are not schema'd,
       so it is readable through any served version: the comment above already said so and
       nothing in the code relied on it. #166. */ -}}
{{- $sv := include "osh.servedVersion" (list $kind (include "osh.version" (list $top)) $crds) -}}
{{- if ne $sv "" -}}
{{- $o := lookup (include "osh.apiVersionAt" (list $sv)) $kind $top.Release.Namespace $name -}}
{{- if $o -}}
{{- $ann := (($o.metadata).annotations) | default dict -}}
{{- $v = (index $ann (include "osh.originVersionAnnotation" $top)) | default "" -}}
{{- end -}}
{{- end -}}
{{- $v -}}
{{- end -}}

{{/* The version a Composition must be EMITTED at: its origin version when it has one and the
     CRD still serves it, otherwise the current version.

     A pure function of its four arguments -- no `lookup` -- which is what makes the decision
     testable without a cluster: hack/validate-origin-version-pin.py calls it from a probe
     template with synthetic origins and synthetic CRD lists and asserts every branch.

     # Why pinning exists, and what it does not do

     This comment used to say a bump's GVK change makes helm delete and recreate the object, and
     cited d3. That was wrong. Helm matches resources on name, namespace, kind and group, not
     version (isMatchingInfo in helm v3.19.0 and v3.20.2; plumbing's Reconcile uses the same
     match). Measured with Helm 3.19.0 on k3s 1.33: a move between served versions kept the UID.

     The d3 split on 2026-11-03 was #166. Its audit log shows helm upgrade to umbrella revision 9
     at 07:13:57Z, 28 composition CRs deleted by helm at 07:14:07-13Z, and v0-3-28 added to the
     mariadbs CRD at 07:14:55Z, after they were gone. The gate had closed on a version not yet
     served. The re-created CR got a new UID, CalculateReleaseName is <CR name>-<hash of UID>, so
     the component acquired a second release, and that release's Deployments carry its name in
     spec.selector, which Kubernetes forbids changing.

     So the pin does not protect UIDs. What it still does is keep a component's reads and writes
     on one version's schema across a bump. Whether that is worth the cost below is an open
     question.

     # Why it is safe to ask for an older version

     core-provider's CRD generation ACCUMULATES versions: generation.AppendVersion appends the
     new one and then sets Served=true on every non-vacuum version, so after a bump the CRD
     serves the old version and the new one. The third condition below checks exactly that,
     against the CRD list already in hand, and falls back to the current version if the origin
     is no longer served -- because emitting at a version nothing serves does not degrade, it
     500s the render ("no matches for kind"), and a release that stops rendering is a release
     that gets uninstalled (#68). A component that changes version is recoverable; that is not.

     # THE COST, which is real and silent

     Reading and writing an object through an OLDER version means reading and writing it
     through that version's schema, and a narrower schema PRUNES. core-provider's fieldloss
     package records the counter-intuitive half, verified against kind v1.36.4: "READS through
     a narrow endpoint are pruned too, not only writes." Reproduced for this change on kind
     v1.36.1, with a CRD serving a narrow v0-3-28 and a wide v0-3-30 over one object written
     through the wide endpoint:

       GET /apis/composition.krateo.io/v0-3-30/.../mariadbs/mariadb -> spec {a, b}
       GET /apis/composition.krateo.io/v0-3-28/.../mariadbs/mariadb -> spec {a}

     Same object, same storage, one field gone on the way out. So any `values` property this chart
     adds AFTER a pinned component's origin version is silently DROPPED for that component --
     not rejected, not warned about, dropped. The only symptom is that the setting does
     nothing. A component that needs a newly added field has to be recreated (delete its
     Composition and let the umbrella emit it fresh, which re-stamps the origin at the current
     version) and not merely configured. README, "Upgrading an existing install", carries this
     in operator terms.

     # WHEN THE PIN APPLIES: only while migration is NOT approved

     The pin and core-provider's instance migration are two writers of one object, so only one may
     act. osh.emitVersion asks osh.migrationApprovedFor -- the engine's own predicate, read off the
     component's live CompositionDefinition -- and drops the origin (osh.effectiveOrigin) when the
     engine is approved to migrate: Automatic, unset (the CRD default), or Manual with
     krateo.io/upgrade-to-version naming the current version. Then the component is emitted at the
     current version once it is served, and at the newest served version before that, so the bump
     instant is protected exactly as above. Paused, or Manual without that annotation, keeps the
     pin.

     It used to be coupled the other way, by coincidence: the pin was assumed inert under
     Automatic because the engine pruned the old version (measured on 0.3.28: one served version
     per CRD). core-provider 2.13.82 keeps a version that a release revision still uses, and the
     umbrella's own manifest at the origin was such a revision, so the pin stayed in force under
     Automatic: every child kept being written through its origin's narrower schema, values added
     since were dropped on store, the old version was never retired, and the engine and the
     umbrella wrote the child at two versions (#233, core-provider#462). Measured on GKE, 0.3.47 to
     0.3.48: all 32 children emitted at v0-3-47 an hour after the upgrade, and a keystone
     `oidc.caBundleRef` rendered by the umbrella was missing from the stored child.

     Pausing a component's migration is still a per-component decision with a cost (its CRD keeps
     every version it ever served, and core-provider runs a controller per served version). README,
     "Hard incompatibility", states it and how to opt a component out. */}}
{{- define "osh.pinVersion" -}}
{{- $current := index . 0 -}}{{- $origin := index . 1 -}}{{- $kind := index . 2 -}}{{- $crds := index . 3 -}}
{{- if and (ne $origin "") (ne $origin $current) (eq (include "osh.crdExists" (list $kind $origin $crds)) "true") -}}
{{- $origin -}}
{{- else -}}
{{- $current -}}
{{- end -}}
{{- end -}}

{{/* Which version to emit at, as a pure function, so every arm is testable without a cluster.

     The pin answers first. If the pin's answer is served, that is the version. If it is not, and
     the object EXISTS, emit at what osh.servedVersion answers: the current version if served,
     otherwise the newest served one, whose schema is the widest available and so drops the
     least. The emission gate is closed by an unserved version, and a closed gate on a live
     object deletes it. That covers the two bump
     cases osh.pinVersion cannot, both of which pruned in 0.3.34: an existing object with no
     origin stamp, and one whose origin version is no longer served. In both the pin falls back
     to the current version, which nothing serves yet at the bump instant.

     A component that does not exist keeps the pin's answer, so it still waits for its current
     CRD rather than being created at an older schema it would then be pinned to.

     An existence check has to be permissive, because its job is to stop a deletion.

     THE INVARIANT THE GATE DEPENDS ON: when the object exists, this returns a version the
     cluster serves. compositions.yaml still ANDs $crd outside its $exists escape, so the
     structure alone does not protect a live object; this does. Break it and #166 returns.
     hack/validate-upgrade-does-not-prune.py asserts it.

     In practice osh.exists only answers "true" when a version is served, so the `ne $served ""`
     below is a guard against a caller passing the wrong answer. It is not what makes a live
     object safe. osh.exists looks in the release namespace, which is where compositions.yaml
     emits, and the two must stay in step: a component emitted elsewhere would be invisible to
     the escape. */}}
{{- define "osh.resolveVersion" -}}
{{- $current := index . 0 -}}{{- $origin := index . 1 -}}{{- $exists := index . 2 -}}{{- $kind := index . 3 -}}{{- $crds := index . 4 -}}
{{- $pinned := include "osh.pinVersion" (list $current $origin $kind $crds) -}}
{{- $served := include "osh.servedVersion" (list $kind $current $crds) -}}
{{- if and (eq $exists "true") (ne $served "") (ne (include "osh.crdExists" (list $kind $pinned $crds)) "true") -}}
{{- $served -}}
{{- else -}}
{{- $pinned -}}
{{- end -}}
{{- end -}}

{{/* The version compositions.yaml emits <kind>/<name> at. See osh.resolveVersion for the decision.

     compositions.yaml calls this ONCE per component and uses the answer for both the gate and the
     emitted apiVersion. Before #166 those were two derivations of one fact, the gate asking
     osh.version and the emitter asking osh.pinVersion, and that is how they came to disagree. <exists>
     is the caller's osh.exists answer for the same reason: asking again here would be a second
     read of one object, and an answer that can differ from the gate's.

     Under `helm template` there is no cluster, so osh.originVersion finds nothing and this is
     the current version for every component -- the offline render is byte-identical to one
     with no pinning at all, which is what hack/validate-origin-version-pin.py asserts across
     all four install selections.

     Read osh.pinVersion before changing anything here: it carries what the pin does and does
     not do, the pruning cost an older schema imposes, and the one upgrade setting that releases
     it. */}}
{{/* Whether core-provider's engine is approved to migrate <name>'s instances to <current>: the
     SAME predicate the engine evaluates, CompositionDefinition.MigrationApproved(targetVersion)
     at core-provider 2.13.82 (apis/compositiondefinitions/v1alpha1/types.go:258-269):

       Paused    -> false
       Manual    -> annotation krateo.io/upgrade-to-version == targetVersion (exact string)
       otherwise -> true            (Automatic, or unset: the CRD defaults it to Automatic)

     targetVersion is the served version name the engine builds from the chart version,
     "v" + "." replaced by "-" (internal/tools/chart/chart.go), which is exactly osh.version. An
     annotation naming any OTHER version approves nothing, an older one included.

     A pure function of (policy, upgrade-to annotation, current version), so
     hack/validate-upgrade-does-not-prune.py drives every case without a cluster. */}}
{{- define "osh.migrationApproved" -}}
{{- $policy := index . 0 | toString -}}{{- $upgradeTo := index . 1 | toString -}}{{- $current := index . 2 | toString -}}
{{- if eq $policy "Paused" -}}
{{- else if eq $policy "Manual" -}}
{{- if eq $upgradeTo $current -}}true{{- end -}}
{{- else -}}
true
{{- end -}}
{{- end -}}

{{/* osh.migrationApproved for component <name>, read off its LIVE CompositionDefinition, so an
     operator's `kubectl patch ... upgradePolicy` counts exactly as it does for the engine. One
     named GET in the release namespace, where templates/definitions.yaml registers it. No
     CompositionDefinition found -- the first render, or `helm template` where lookup is
     disabled -- reads as unset, which the CRD defaults to Automatic. */}}
{{- define "osh.migrationApprovedFor" -}}
{{- $top := index . 0 -}}{{- $name := index . 1 -}}
{{- $cd := lookup "core.krateo.io/v1alpha1" "CompositionDefinition" $top.Release.Namespace $name | default dict -}}
{{- $policy := (($cd.spec).upgradePolicy) | default "" -}}
{{- $upgradeTo := index ((($cd.metadata).annotations) | default dict) "krateo.io/upgrade-to-version" | default "" -}}
{{- include "osh.migrationApproved" (list $policy $upgradeTo (include "osh.version" (list $top))) -}}
{{- end -}}

{{/* The origin the emission honours: the component's origin stamp while its instances are NOT
     approved to migrate, and none ("") once they are. A pure function of (origin, approved).

     When migration is approved the engine rewrites every instance through the current version
     and retires the old controller; an umbrella still emitting the child at its origin would be a
     second writer at the old version, keeping that version served (core-provider no longer prunes
     a version a release revision still uses) and writing the child through its narrower schema,
     which PRUNES every value the component gained since (#233, measured: a keystone
     `oidc.caBundleRef` rendered and dropped on store). With no origin, osh.resolveVersion emits
     the current version when it is served and otherwise the newest served one, so #166's gate
     protection at the bump instant is unchanged. */}}
{{- define "osh.effectiveOrigin" -}}
{{- $origin := index . 0 | toString -}}{{- $approved := index . 1 | toString -}}
{{- if ne $approved "true" -}}{{- $origin -}}{{- end -}}
{{- end -}}

{{- define "osh.emitVersion" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $crds := index . 3 -}}{{- $exists := index . 4 -}}
{{- $current := include "osh.version" (list $top) -}}
{{- $approved := include "osh.migrationApprovedFor" (list $top $name) -}}
{{- $origin := include "osh.effectiveOrigin" (list (include "osh.originVersion" (list $top $kind $name $crds)) $approved) -}}
{{- include "osh.resolveVersion" (list $current $origin $exists $kind $crds) -}}
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
{{- /* Served version, as in osh.exists. A dependency's Ready condition is a property of the
       OBJECT, not of the chart's version. Read at the current version it made every dependency
       read not-ready at the bump instant, which compounded #166 rather than causing it -- the
       outer `$crd` had already closed the gate. */ -}}
{{- $sv := include "osh.servedVersion" (list $kind (include "osh.version" (list $top)) $crds) -}}
{{- if ne $sv "" -}}
{{- $o := lookup (include "osh.apiVersionAt" (list $sv)) $kind $top.Release.Namespace $name -}}
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
{{- /* Served version, for the same reason as osh.ready. This one did not contribute to #166
       -- "" is the permissive answer here, so a mis-read could not prune anything -- but it
       would have reported a genuinely paused dependency as not paused at the bump instant,
       which is the relaxed gate silently losing its only safety condition. */ -}}
{{- $sv := include "osh.servedVersion" (list $kind (include "osh.version" (list $top)) $crds) -}}
{{- if ne $sv "" -}}
{{- $o := lookup (include "osh.apiVersionAt" (list $sv)) $kind $top.Release.Namespace $name -}}
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
{{/* Whether version name <a> is newer than version name <b>. Numeric, because the names sort
     wrongly as strings: v0-3-9 is older than v0-3-32. A name that is not a plain vX-Y-Z release
     is older than any that is, and never newer than another. */}}
{{- define "osh.newerVersion" -}}
{{- $a := index . 0 -}}{{- $b := index . 1 -}}
{{- $release := "^v[0-9]+-[0-9]+-[0-9]+$" -}}
{{- if regexMatch $release $a -}}
{{- if not (regexMatch $release $b) -}}
true
{{- else if semverCompare (printf ">%s" ($b | trimPrefix "v" | replace "-" ".")) ($a | trimPrefix "v" | replace "-" ".") -}}
true
{{- end -}}
{{- end -}}
{{- end -}}

{{/* The version of <kind>'s CRD this chart should talk to, of those the cluster ACTUALLY SERVES:
     <current> if served, otherwise the newest served one; "" when the kind has no served version
     at all.

     This exists because every other version-sensitive helper here used to ask about the chart's
     CURRENT version, and at the one instant that matters the cluster does not serve it yet. On
     the first render after a version bump the new CompositionDefinitions -- rendered in that
     same pass -- are what create the new CRD version, so a question about the current version is
     a question whose answer that very render establishes. osh.crdExists answered "" for all 34
     components, `$crd` is an outer conjunct in compositions.yaml's gate, the `$exists` escape
     was never reached, and helm pruned 34 live CRs. Measured on a0-openstack-d4 during
     0.3.32 -> 0.3.33: 6 CRs left for 37 releases, 27 of them running with nothing reconciling
     them. See issue #166.

     PREFERRING current means normal renders behave exactly as before, and only the bump instant,
     where current is NOT served, falls back, which is the case that used to answer "nothing is
     here".

     The fallback is the NEWEST served version, not the first in the CRD, because this answer is
     also a version osh.resolveVersion WRITES through. core-provider only appends to the list, so
     the first entry is the oldest and narrowest schema, and a live object emitted there loses
     every field added since. A paused definition keeps several versions served, so this is a
     real state. The comparison is by version number and does not rely on list order.

     A pure function of its arguments, no `lookup`, so every branch is testable without a
     cluster. hack/validate-upgrade-does-not-prune.py does that. */}}
{{- define "osh.servedVersion" -}}
{{- $kind := index . 0 -}}{{- $current := index . 1 -}}{{- $crds := index . 2 -}}
{{- $preferred := "" -}}{{- $found := "" -}}
{{- range ($crds | default list) -}}
{{- if and (eq .spec.group "composition.krateo.io") (eq .spec.names.kind $kind) -}}
{{- range (.spec.versions | default list) -}}
{{- if .served -}}
{{- if eq .name $current -}}{{- $preferred = .name -}}{{- else if or (eq $found "") (include "osh.newerVersion" (list .name $found)) -}}{{- $found = .name -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $preferred | default $found -}}
{{- end -}}

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
{{- /* Asked at a SERVED version. "Is there a live CR here that must not be pruned" does not
       depend on which version the chart is at, and asking it at the current version answered
       "no" for every component at the bump instant -- the one time the answer had to be yes.
       This is the escape clause in #166 that was never reached. */ -}}
{{- $sv := include "osh.servedVersion" (list $kind (include "osh.version" (list $top)) $crds) -}}
{{- if ne $sv "" -}}
{{- if lookup (include "osh.apiVersionAt" (list $sv)) $kind $top.Release.Namespace $name -}}{{- $r = "true" -}}{{- end -}}
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
  {{/* A PROFILE PROMISES ONLY WHAT CAN WORK ON THE CLUSTER YOU HAVE.

       `requiresProvisioning` components are excluded from every profile, including `full`.
       They are not unsupported and nothing about them is removed -- they are selectable, and
       fully gated, through `enabled:`. What they are not is DEFAULT, because each of them waits
       on infrastructure that no Kubernetes cluster provides and that this blueprint does not
       create: an interface named ironic-pxe, a Neutron port per host on octavia's management
       network, a ReadWriteMany StorageClass for swift.

       Measured on a 4-node GKE install of profile=full: six pods at 14 restarts in Init plus
       four Pending, none of which could EVER become Ready, and `Init:` is the state a pod
       census can only count -- neither Pending nor CrashLoopBackOff, so "21 in Init" names
       nothing. A profile that installs components which cannot start is a promise that cannot
       be kept, and the cost of keeping it is paid by whoever next reads that census.

       hack/validate-provisioning-preconditions.py asserts the flag and the declared
       precondition agree in BOTH directions, so a flagged component must say what it needs and
       a declared precondition must belong to a flagged component. Issue #163. */}}
  {{- range $c := $comps -}}
    {{/* `full` is unchanged and still selects every component, including the three that need
         infrastructure an operator must provide. `full-strict` selects everything EXCEPT those,
         so it is the profile that promises only what can start on a cluster as it comes.

         Opt-in rather than a change to `full`, deliberately: a profile silently selecting three
         fewer components than it did yesterday is a surprise delivered through a patch release,
         and on 0.3.x the version number cannot signal it. `full-strict` is discoverable, says
         what it means, and leaves anyone who HAS provisioned a PXE interface, an lb-mgmt-net or
         an RWX StorageClass exactly where they were. */}}
    {{- if or (eq $top.Values.profile "full")
              (and (eq $top.Values.profile "full-strict") (not $c.requiresProvisioning))
              (eq $c.tier "identity") -}}
      {{- $_ := set $sel $c.name true -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- keys $sel | sortAlpha | join " " -}}
{{- end -}}

{{/* The name of the ClusterRole and ClusterRoleBinding that let this release read the cluster's
     nodes. They are NOT rendered by this chart -- they are a release asset
     (blueprints/openstack/node-reader-rbac.yaml) that an administrator applies alongside the
     CompositionDefinition, and this chart only reads them back through osh.nodeReadGranted.

     A fixed literal, for the same reason the asset is not templated: the asset has to be
     installable with `kubectl apply` and the guard has to find it by name, so the name cannot
     depend on anything either side knows and the other does not. In particular it is NOT
     stamped with the chart version -- a version-stamped name would leave 0.3.30 hunting for a
     grant only 0.3.29's asset ever created -- and not with the release name, since one grant
     per cluster covers every Openstack Composition in the namespace the binding names.

     Both halves of that agreement are checked: hack/validate-node-label-preconditions.py
     asserts that this literal is the name the asset actually declares, because a rename on
     either side produces no error anywhere -- the guard simply never finds the grant and the
     precondition silently stops being enforced. */}}
{{- define "osh.nodeReaderName" -}}
krateo-openstack-node-reader
{{- end -}}

{{/* Returns "true" when this release may list the cluster's nodes, which is the only safe
     precondition for looking them up.

     Why a guard at all -- the measurement that forced it. `lookup` does NOT return empty when
     it is refused. Helm returns an empty map only for a NotFound; every other API error,
     Forbidden included, comes back as a template error and fails the WHOLE render.

     That sentence says "every other API error" but the repro below exercises ONE kind, so read
     on its own it is a measurement of the node lookup generalised by assertion. The source is
     what actually generalises it: helm/helm v3.19.0, pkg/engine/lookup_func.go, is a single
     function behind every `lookup` -- Get and List, every GVK -- and it returns
     empty-with-nil-error only under `apierrors.IsNotFound(err)`, propagating everything else.
     There is no 403 branch and IsNotFound is false for one. So EMPTY MEANS LOOKUP DISABLED
     (`helm template`, no cluster), NEVER DENIED, for CRDs and Compositions as much as for
     nodes -- which is what makes the two-state existence checks in this file safe. Cited
     rather than restated so the next reader can check it: the version is pinned and the
     function is named. (Found by installer, reading the source at the installed version after
     this comment's claim was offered to them second-hand.)

     Measured against a real composition-dynamic-controller identity on a 4-node GKE cluster:

       $ kubectl auth can-i list nodes --as=system:serviceaccount:openstack:keystones-v0-3-29
       no
       $ helm template probe . --dry-run=server \
           --kube-as-user=system:serviceaccount:openstack:keystones-v0-3-29
       Error: ... at <lookup "v1" "Node" "" "">: error calling lookup: nodes is forbidden:
       User "system:serviceaccount:openstack:keystones-v0-3-29" cannot list resource "nodes"
       at the cluster scope

     core-provider's generated role grants the controller `customresourcedefinitions
     get/list/watch` -- which is why the $crds lookup in compositions.yaml works -- and no
     `nodes` rule at all. An unguarded node lookup would therefore make every reconcile of this
     umbrella error, and a Helm release that stops rendering is a Helm release that gets
     uninstalled: #68, on every cluster, immediately. `helm template` could never have caught
     it, because with no cluster the lookup is never attempted at all.

     So the permission is granted out of band, by an administrator applying the release asset
     blueprints/openstack/node-reader-rbac.yaml, and this reads back whether that grant is in
     place. Not granted: the precondition is not measured and the install behaves exactly as it
     did before the check existed. Granted: the precondition is measured on every reconcile. The
     shape is the one osh.crdExists already uses for "the component CRDs do not exist yet" --
     read the cluster for a prerequisite, and answer honestly when it is absent.

     The chart deliberately does not create the grant for itself. It could: `escalate` and `bind`
     on clusterroles are both in that generated role, and a server-side dry-run create of those
     two objects as a real controller service account passes admission. But a chart that widens
     its own permissions is a decision that belongs to a person who can refuse it, and the grant
     is cluster-scoped node read for a whole namespace's service accounts. Making it an applied
     asset also makes it auditable: `kubectl get clusterrole krateo-openstack-node-reader` is the
     answer to "is this check enforced on this cluster".

     It checks the PERMISSION, not merely that objects of those names exist: the ClusterRole must
     carry a rule that actually covers nodes/list, and the binding must reference that role with
     THIS namespace's service-account group as a subject. A name-only guard would green-light a
     lookup that still 403s, and that 403 is the render-aborting error above -- so a guard that
     is approximately right is not good enough here. Checking the namespace in the subject is
     what makes installing into a namespace the asset was not edited for a skip rather than a
     crash.

     Why the asset's subject is a group rather than the controller's own service account is
     argued in the asset itself: the account's name embeds an irregular plural and the chart
     version, and either one being wrong turns this guard into that 403.

     When the asset is never applied, this stays "" forever and the precondition is never
     measured. That is a real cost of the asset-based design -- it is also why
     templates/node-preconditions-state.yaml writes the skip and its reason into the cluster, so
     an unenforced check is visible rather than assumed.

     One residual assumption, stated because it is the only way this can still 403: the grant
     covers service accounts in the release namespace, so the guard is exact for the Krateo path,
     where the render identity is composition-dynamic-controller's own service account in that
     namespace (measured: system:serviceaccount:openstack:keystones-v0-3-29 for a Composition in
     `openstack`). An operator running `helm install` of this chart BY HAND is a kubeconfig user
     instead, and is not in that group. Cluster-admins -- the documented case -- read nodes
     anyway; a restricted human user with the grant applied but no node read of their own would
     get the Forbidden on their terminal. That is a visible error in an interactive command, not
     a controller silently losing its release, which is why the group is still the right subject;
     and verified end to end on a real cluster, including that the namespace check turns a
     mismatched grant into a skip rather than that 403.

     Two named GETs, called ONCE per render from compositions.yaml above the component range.
     Never call this from inside that range: see osh.crdExists for what a per-component cluster
     read costs here. */}}
{{- define "osh.nodeReadGranted" -}}
{{- $top := index . 0 -}}
{{- $name := include "osh.nodeReaderName" $top -}}
{{- $role := lookup "rbac.authorization.k8s.io/v1" "ClusterRole" "" $name -}}
{{- $binding := lookup "rbac.authorization.k8s.io/v1" "ClusterRoleBinding" "" $name -}}
{{- $canList := "" -}}
{{- range $r := (($role).rules | default list) -}}
{{- if and (or (has "" ($r.apiGroups | default list)) (has "*" ($r.apiGroups | default list))) (or (has "nodes" ($r.resources | default list)) (has "*" ($r.resources | default list))) (or (has "list" ($r.verbs | default list)) (has "*" ($r.verbs | default list))) -}}
{{- $canList = "true" -}}
{{- end -}}
{{- end -}}
{{- $bound := "" -}}
{{- if and $binding (eq (($binding.roleRef).name | toString) $name) (eq (($binding.roleRef).kind | toString) "ClusterRole") -}}
{{- range $s := ($binding.subjects | default list) -}}
{{- if and (eq ($s.kind | toString) "Group") (eq ($s.name | toString) (printf "system:serviceaccounts:%s" $top.Release.Namespace)) -}}
{{- $bound = "true" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and (eq $canList "true") (eq $bound "true") -}}true{{- end -}}
{{- end -}}

{{/* The node labels that the SELECTED components' pods schedule on, as sorted "key=value"
     tokens, space-joined. Pure values arithmetic -- no `lookup` -- so it returns the same
     answer under `helm template` as against a live cluster, and the precondition check and the
     state ConfigMap read one set from one place.

     Selection-aware on purpose. profile=identity selects five components that only ever ask for
     openstack-control-plane, so it must not be told to label nodes for a compute plane it is
     not installing; and .Values.nodeLabelPreconditions carries, per label, exactly the
     components whose RENDERED pods select on it -- see that block for why rendered pods and not
     the charts' values keys. */}}
{{- define "osh.nodeLabelsWanted" -}}
{{- $top := index . 0 -}}{{- $sel := index . 1 -}}
{{- $out := list -}}
{{- range $p := ($top.Values.nodeLabelPreconditions | default list) -}}
{{- range $c := ($p.components | default list) -}}
{{- if has $c $sel -}}{{- $out = append $out (printf "%s=%s" $p.key ($p.value | toString)) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $out | uniq | sortAlpha | join " " -}}
{{- end -}}

{{/* Of the labels the selected components want, the ones that NO node in <nodes> carries, as
     space-joined "key=value" tokens.

     A pure function of its arguments, the node list included, and that is what makes the
     decision testable without a cluster whose nodes lack a label:
     hack/validate-node-label-preconditions.py calls it with synthetic node lists and asserts
     both answers, and calls osh.requireNodeLabels the same way to prove the render really does
     abort.

     An empty <nodes> therefore reports EVERY wanted label as uncarried, which is the honest
     answer to the question asked: no node in an empty list carries anything. It is deliberately
     not where "cannot measure" is handled -- that lives one level up, in osh.requireNodeLabels.
     Collapsing the skip into this predicate would look equivalent and would not be: a real
     cluster whose nodes carry none of these labels would then read as "nothing missing" and
     pass, which is the defect this whole mechanism exists to catch. The gate asserts both
     halves of that division. */}}
{{- define "osh.nodeLabelsMissing" -}}
{{- $top := index . 0 -}}{{- $sel := index . 1 -}}{{- $nodes := index . 2 | default list -}}
{{- $missing := list -}}
{{- range $t := (splitList " " (include "osh.nodeLabelsWanted" (list $top $sel)) | compact) -}}
{{- $parts := splitList "=" $t -}}
{{- $key := index $parts 0 -}}{{- $value := index $parts 1 -}}
{{- $carried := "" -}}
{{- range $n := $nodes -}}
{{- if eq (index (($n.metadata).labels | default dict) $key | toString) $value -}}{{- $carried = "true" -}}{{- end -}}
{{- end -}}
{{- if ne $carried "true" -}}{{- $missing = append $missing $t -}}{{- end -}}
{{- end -}}
{{- $missing | join " " -}}
{{- end -}}

{{/* The operator-facing message for a missing node label. Self-sufficient by construction: the
     label, its value, which of the SELECTED components want it, the command that fixes it, and
     what the symptom would have looked like.

     That last part is not padding. This precondition was already documented -- the exact
     `kubectl label` line is in both of the README's install sequences, with the scheduler's
     message quoted next to it -- and a fresh GKE install stalled on it anyway, because the
     scheduler says "N node(s) didn't match Pod's node affinity/selector" and names neither the
     label nor the chart that wants it. For a DaemonSet there is no message at all: one whose
     nodeSelector matches no node has desiredNumberScheduled=0 and creates no pod, so there is
     no Pending pod and no event to find. Measured on a 4-node GKE install of profile=full whose
     nodes carried only openstack-control-plane: 263 pods, 227 of them selecting that label, and
     nova-compute, libvirt, cyborg-agent, neutron-ovs-agent and openvswitch all sitting at
     desiredNumberScheduled=0 -- an OpenStack with no hypervisor and no data plane, reported by
     nothing. */}}
{{- define "osh.nodeLabelMessage" -}}
{{- $top := index . 0 -}}{{- $sel := index . 1 -}}{{- $missing := index . 2 -}}{{- $nodes := index . 3 | default list -}}
{{- $tokens := splitList " " $missing | compact -}}
{{- $lines := list "" (printf "node labels missing: none of the %d node(s) this release can see carries %d label(s) that the selected OpenStack components schedule their pods on." (len $nodes) (len $tokens)) "" -}}
{{- range $t := $tokens -}}
{{- $who := list -}}
{{- range $p := ($top.Values.nodeLabelPreconditions | default list) -}}
{{- if eq (printf "%s=%s" $p.key ($p.value | toString)) $t -}}
{{- range $c := ($p.components | default list) -}}
{{- if has $c $sel -}}{{- $who = append $who $c -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $lines = append $lines (printf "    %s  <- wanted by: %s" $t (join ", " ($who | sortAlpha))) -}}
{{- end -}}
{{- $lines = append $lines "" -}}
{{- $lines = append $lines "Label the nodes that should run those components and this render proceeds by itself on the next reconcile, with nothing to re-run:" -}}
{{- $lines = append $lines "" -}}
{{- $lines = append $lines (printf "    kubectl label --overwrite nodes --all %s" (join " " $tokens)) -}}
{{- $lines = append $lines "" -}}
{{- $lines = append $lines "Label individual nodes instead if only some of them should run OpenStack, or deselect the components listed above through spec.enabled / spec.profile." -}}
{{- $lines = append $lines "" -}}
{{- $lines = append $lines "Nothing in the charts creates these labels. This render is refused rather than allowed to proceed because the resulting symptom is not reliably visible: a Deployment, StatefulSet or Job leaves a Pending pod saying \"node(s) didn't match Pod's node affinity/selector\", which names neither the label nor the chart, while a DaemonSet creates NO POD AT ALL -- desiredNumberScheduled=0, no Pending pod, no event. A 4-node profile=full install whose nodes carried only openstack-control-plane ran 227 pods and had no nova-compute, no libvirt and no openvswitch, with nothing anywhere reporting a problem." -}}
{{- $lines | join "\n" -}}
{{- end -}}

{{/* The node-label precondition itself: refuse to render when this release can see the
     cluster's nodes and none of them carries a label the selected components need.

     THREE states, not two, and the middle one is why the node list is an argument rather than
     something this looks up itself:

       nodes non-empty, some wanted label carried by none of them   FAIL: measured, and bad.
       nodes non-empty, every wanted label carried by some node     pass.
       nodes EMPTY                                                  SKIP: not measured.

     The skip covers both ways the measurement can be unavailable: `lookup` returns nothing
     under `helm template`, which runs in CI on every pull request, and osh.nodeReadGranted
     withholds the lookup entirely until the chart's own node-read grant exists. Failing on
     either would break every pull request in this repo and the first reconcile of every
     install.

     That skip is legitimate here in a way a CI gate's skip is not, and the difference is worth
     stating because this repo has been bitten by the other kind: four gates printed "SKIP:
     PyYAML unavailable" and exited 0 for weeks (#120), reporting success on a measurement they
     could have made at any time. A CI gate's input -- the charts -- is always present, so a
     gate that skips is a gate that declined to look. This check's input is a live cluster's
     node list, which genuinely does not exist during `helm template`. And unlike a CI gate this
     is a runtime precondition re-evaluated on every reconcile, so a pass that could not be
     measured now is measured again a minute later; nothing is being waved through for good.
     templates/node-preconditions-state.yaml writes the skip and its reason into the cluster so
     the unmeasured case is never silent either.

     `fail` rather than a rendered warning, because the operator has to be stopped, not
     informed: composition-dynamic-controller surfaces a render error on the Composition's own
     status and events, which is the one place the operator is already looking, whereas a
     warning in a ConfigMap is the same invisible prose as the README line that failed. */}}
{{- define "osh.requireNodeLabels" -}}
{{- $top := index . 0 -}}{{- $sel := index . 1 -}}{{- $nodes := index . 2 | default list -}}
{{- if gt (len $nodes) 0 -}}
{{- $missing := include "osh.nodeLabelsMissing" (list $top $sel $nodes) -}}
{{- if $missing -}}
{{- fail (include "osh.nodeLabelMessage" (list $top $sel $missing $nodes)) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- /* osh.credentialRefs (list $ $c): the credential REFERENCES component $c receives (#228), as a
       YAML dict to merge into its spec. For each id in $c.credentials, the item of
       .Values.credentials.items with that id contributes {store, secret, key} at the item's dotted
       `path` (e.g. endpoints.identity.auth.admin.passwordFrom), or at the item's `pathFor.<component>`
       when it names this component (a chart holding the same credential under another key). A
       value is never passed: the chart
       projects it from the root Secret through the store. An id no item defines fails the render,
       because a silently missing reference leaves that component on the published default. */ -}}
{{- define "osh.credentialRefs" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- $creds := $top.Values.credentials | default dict -}}
{{- $out := dict -}}
{{- range $id := ($c.credentials | default list) -}}
{{- $item := dict -}}
{{- range $i := ($creds.items | default list) -}}{{- if eq $i.id $id -}}{{- $item = $i -}}{{- end -}}{{- end -}}
{{- if not $item -}}
{{- fail (printf "component %s lists credential %q, which .Values.credentials.items does not define" $c.name $id) -}}
{{- end -}}
{{- if $item.retired -}}
{{- fail (printf "component %s lists credential %q, which is retired: no chart reads it any more" $c.name $id) -}}
{{- end -}}
{{- $ref := dict "store" $creds.store "secret" $item.secret "key" $item.key -}}
{{- $path := $item.path -}}
{{- if hasKey ($item.pathFor | default dict) $c.name -}}{{- $path = index $item.pathFor $c.name -}}{{- end -}}
{{- $parts := splitList "." $path -}}
{{- $node := $out -}}
{{- range $idx, $p := $parts -}}
{{- if eq $idx (sub (len $parts) 1) -}}
{{- $_ := set $node $p $ref -}}
{{- else -}}
{{- if not (hasKey $node $p) -}}{{- $_ := set $node $p (dict) -}}{{- end -}}
{{- $node = index $node $p -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end -}}
