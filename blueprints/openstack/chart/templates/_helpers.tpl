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

{{/* Component <name>'s LIVE CompositionDefinition, as JSON ("{}" when there is none): the ONE
     named GET of it per render, in the release namespace where templates/definitions.yaml
     registers it. No CompositionDefinition found -- the first render, or `helm template` where
     lookup is disabled -- is the empty object.

     compositions.yaml calls this once per selected component and hands the parsed object to the
     two readers of it, osh.migrationApprovedFor and the values-phase decision (osh.holdValues,
     through osh.definitionApiVersion and osh.definitionReady). Both used to be, or would have
     been, their own GET of the same object: osh.migrationApprovedFor performed this lookup itself
     until the two-phase emission needed the definition's status too. One read means the policy
     the version is chosen by and the status the values are released by are the same snapshot of
     the object, not two reads that can straddle a reconcile of it. Returned as JSON because a
     template can only return text.

     hack/validate-two-phase-values.py asserts this structurally, because offline `lookup` returns
     nothing and a gutted helper answers exactly what this one answers without a cluster. */}}
{{- define "osh.definition" -}}
{{- $top := index . 0 -}}{{- $name := index . 1 -}}
{{- $cd := lookup "core.krateo.io/v1alpha1" "CompositionDefinition" $top.Release.Namespace $name | default dict -}}
{{- $cd | toJson -}}
{{- end -}}

{{/* osh.migrationApproved for a component, read off <cd>, its LIVE CompositionDefinition as
     osh.definition returned it, so an operator's `kubectl patch ... upgradePolicy` counts exactly
     as it does for the engine. An empty <cd> reads as unset, which the CRD defaults to Automatic.
     A pure function of (<top>, <cd>): the read is osh.definition's. */}}
{{- define "osh.migrationApprovedFor" -}}
{{- $top := index . 0 -}}{{- $cd := index . 1 | default dict -}}
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

{{/* The version compositions.yaml emits <kind>/<name> at. See osh.resolveVersion for the decision.

     compositions.yaml calls this ONCE per component and uses the answer for both the gate and the
     emitted apiVersion. Before #166 those were two derivations of one fact, the gate asking
     osh.version and the emitter asking osh.pinVersion, and that is how they came to disagree. <exists>
     is the caller's osh.exists answer for the same reason: asking again here would be a second
     read of one object, and an answer that can differ from the gate's. <approved> is the caller's
     osh.migrationApprovedFor answer for that reason too: the values-phase decision in
     compositions.yaml needs the same answer, and it must not be derived twice.

     Under `helm template` there is no cluster, so osh.originVersion finds nothing and this is
     the current version for every component -- the offline render is byte-identical to one
     with no pinning at all, which is what hack/validate-origin-version-pin.py asserts across
     all four install selections.

     Read osh.pinVersion before changing anything here: it carries what the pin does and does
     not do, the pruning cost an older schema imposes, and the one upgrade setting that releases
     it. */}}
{{- define "osh.emitVersion" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $crds := index . 3 -}}{{- $exists := index . 4 -}}{{- $approved := index . 5 -}}
{{- $current := include "osh.version" (list $top) -}}
{{- $origin := include "osh.effectiveOrigin" (list (include "osh.originVersion" (list $top $kind $name $crds)) $approved) -}}
{{- include "osh.resolveVersion" (list $current $origin $exists $kind $crds) -}}
{{- end -}}

{{/* <cd>'s status.apiVersion, the apiVersion its CRD serves for it as core-provider last applied
     it ("composition.krateo.io/v0-3-65"), or "" when <cd> is empty or has no status yet. A pure
     function of <cd>, which is osh.definition's read; it performs no lookup of its own. */}}
{{- define "osh.definitionApiVersion" -}}
{{- $cd := index . 0 | default dict -}}
{{- (($cd.status).apiVersion) | default "" | toString -}}
{{- end -}}

{{/* The STATUS of <cd>'s condition of type Ready ("True", "False", "Unknown"), or "" when it has
     none. A pure function of <cd>, which is osh.definition's read.

     Type and status only, never the reason. Ready=True is reported with more than one reason --
     Available for a definition whose controller is running, ScaledToZero for one that opted into
     scale-to-zero and has no instances (core-provider types.go, ScaleToZero) -- and both mean the
     definition serves its version. A gate on a reason string holds every scaled-to-zero component
     back forever, and the next reason core-provider adds holds back whatever reports it.

     ControllerActive is deliberately NOT read. It is absent for a definition whose controller runs
     on a remote target and False for an idle scale-to-zero one, so requiring it would hold both
     forever. The umbrella does not need the new controller to be running: see osh.holdValues for
     why writing at the new version is safe before it does. */}}
{{- define "osh.definitionReady" -}}
{{- $cd := index . 0 | default dict -}}
{{- $r := "" -}}
{{- range ((($cd.status).conditions) | default list) -}}
{{- if eq (.type | toString) "Ready" -}}{{- $r = .status | toString -}}{{- end -}}
{{- end -}}
{{- $r -}}
{{- end -}}

{{/* "true" when this render must NOT write the component's new values, and must re-emit its live
     spec instead; "" when the values the chart computed are emitted, as before this existed.

     Arguments: (<current>, <emitVer>, <approved>, <exists>, <cdApiVersion>, <cdReady>) -- the
     chart's version, the version osh.emitVersion resolved, osh.migrationApprovedFor's answer,
     osh.exists's answer, and the component definition's status.apiVersion and Ready status. A
     pure function of them, no `lookup`, so hack/validate-two-phase-values.py asserts every branch
     with synthetic input.

     # The defect: new values written at the old version

     One umbrella render moves a component's CompositionDefinition to the new chart version AND
     writes the component's new values. In that render the component's CRD does not serve the new
     version yet -- the definition rendered in the same pass is what adds it -- so osh.resolveVersion
     answers the served OLD version (it must: an unserved version closes the gate and prunes, #166).
     core-provider's krateo-composition-version MutatingAdmissionPolicy stamps the label
     krateo.io/composition-version from the apiVersion of each write, so the object stays labelled
     with the old version, and the OLD version's controller -- still running, selecting on that
     label -- owns it and runs the OLD chart with the NEW values. Measured on GKE 2026-11-15
     (0.3.64 → 0.3.65, core-provider 2.13.102; #228): 3 of 33 components did exactly that, and the
     old chart's pre-upgrade Job rotated passwords ahead of the release that was meant to.
     core-provider confirmed the reading from its code.

     # The two phases

     A component's new values are written only AT the new apiVersion, and only once its definition
     serves that version: status.apiVersion is composition.krateo.io/<current> AND it reports Ready
     with status True (osh.definitionReady says why the reason and ControllerActive are not read),
     AND this render emits at <current>. That is the signal core-provider agreed from its code.
     Until then, an existing component is not changed AT ALL: compositions.yaml re-emits its live
     spec at the version it was last written at (osh.holdVersion), so the held write changes
     neither the spec nor the krateo.io/composition-version label, and the controller that owned
     the object keeps it with the values it already had.

     Writing at the new apiVersion is safe before the new controller runs: the write moves the
     label, the old controller's selector stops matching and it drops the object, and nothing runs
     the old chart with the new values. What must not happen is the half-way state: the OLD spec
     written at the NEW version. That is what holding at <emitVer> did whenever the CRD already
     served the new version but the definition was not Ready on it yet -- the label moved and the
     new chart ran with the old values, an upgrade nobody asked for that pass. Hence the hold is
     at the object's own version, not at <emitVer>.

     # What is not held

       * A component that does not EXIST: there is no old chart running it, and it is created
         exactly as before -- osh.resolveVersion already keeps it waiting for the current CRD.
       * A component whose migration is NOT approved (pinned at its origin, #233): the engine is
         holding it on purpose, it is written at its origin with the values the chart computes,
         and that path keeps its behaviour and its documented cost (osh.pinVersion). Holding it
         would freeze its values for as long as the operator keeps it paused.

     Offline nothing exists, so this is "" for every component and the render is byte-identical to
     the chart without it. In a steady state the signal holds and this is "" too; a definition that
     stops reporting Ready later (its own reconcile failing) defers a values change until it is
     Ready again rather than writing it at a version it may not serve. */}}
{{- define "osh.holdValues" -}}
{{- $current := index . 0 | toString -}}{{- $emitVer := index . 1 | toString -}}{{- $approved := index . 2 | toString -}}
{{- $exists := index . 3 | toString -}}{{- $cdApiVersion := index . 4 | toString -}}{{- $cdReady := index . 5 | toString -}}
{{- if and (eq $exists "true") (eq $approved "true") -}}
{{- if not (and (eq $emitVer $current) (eq $cdApiVersion (include "osh.apiVersionAt" (list $current))) (eq $cdReady "True")) -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{/* The label core-provider's cluster-wide krateo-composition-version MutatingAdmissionPolicy
     stamps on every CREATE and UPDATE of a composition.krateo.io object, with the value
     request.requestKind.version: the served version the object was LAST WRITTEN THROUGH ("v0-3-64").
     Confirmed in core-provider helm/core-provider/templates/compositions-version-policy.yaml, whose
     header says the same, and by core-provider on 2026-11-15. Every composition-dynamic-controller
     selects its instances on exact equality of it, so it names the controller that owns the object.

     It is useless as an origin for exactly that reason (osh.originVersionAnnotation) and exactly
     right as "the version this object is at now", which is what the two-phase hold needs.

     A literal here and nowhere else, read back by hack/validate-two-phase-values.py, for the same
     reason osh.originVersionAnnotation is one: it is an agreement with another component, and a
     rename on either side produces no error anywhere -- the hold would simply stop finding the
     version and fall back to the one it resolved. */}}
{{- define "osh.compositionVersionLabel" -}}
krateo.io/composition-version
{{- end -}}

{{/* The live state of <kind>/<name>, read at <ver>, as JSON {"spec": ..., "version": ...}: its spec,
     and the version its osh.compositionVersionLabel says it was last written through ("" when the
     label is absent). "" when no such object exists.

     One named GET of the one object, made ONLY while osh.holdValues holds: compositions.yaml calls
     it inside that branch, so a steady-state render pays nothing for it, and an upgrade pays one GET
     per held component for the passes until its definition is Ready on the new version. Both
     answers come from this one GET; the label is not read anywhere else. It is a third GET of an
     object osh.exists and osh.originVersion already read, for the reason osh.originVersion gives
     for being the second: hoisting one shared read into compositions.yaml would make this
     component's existence test different code from every dependency's.

     Read at <ver>, the version compositions.yaml resolved, because the version the object is at is
     what this read finds out. When the hold then emits at an older label version, the spec has
     been read through the newer schema, which can add that schema's defaults; written back through
     the older one, any field that schema does not know is pruned on the way in, so storage does
     not change. Guarded by osh.crdExists at <ver> for the reason osh.exists is: a `lookup` at a
     version nothing serves errors, it does not return empty. compositions.yaml only reaches here
     once $crd has said <ver> is served, so the guard is belt-and-braces there and is what keeps a
     probe calling it directly from erroring. */}}
{{- define "osh.liveState" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $ver := index . 3 -}}{{- $crds := index . 4 -}}
{{- if eq (include "osh.crdExists" (list $kind $ver $crds)) "true" -}}
{{- $o := lookup (include "osh.apiVersionAt" (list $ver)) $kind $top.Release.Namespace $name -}}
{{- if $o -}}
{{- $labels := (($o.metadata).labels) | default dict -}}
{{- dict "spec" (($o.spec) | default dict) "version" ((index $labels (include "osh.compositionVersionLabel" $top)) | default "") | toJson -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- /* osh.liveMessaging (list $ kind name ver crds): what a RabbitMQ user rotation reads from the LIVE component
       Composition (#309 C), as JSON {spec, managed}: its spec (the root its passwordFrom names is the active generation)
       and its status.managed. One named GET of the component at <ver>, guarded by osh.crdExists as osh.liveState is.
       Not osh.liveState: that is the hold's read, called once and only inside the hold
       (hack/validate-two-phase-values.py), so a render that holds nothing makes no GET for it. This one is made only for
       a component with an umbrella RabbitMQ projection (osh.messagingProjection: messaging.backend operator) that
       exists: one GET per such component per render, the cost of deriving the rotation from what exists. */ -}}
{{- define "osh.liveMessaging" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $ver := index . 3 -}}{{- $crds := index . 4 -}}
{{- if eq (include "osh.crdExists" (list $kind $ver $crds)) "true" -}}
{{- $o := lookup (include "osh.apiVersionAt" (list $ver)) $kind $top.Release.Namespace $name -}}
{{- if $o -}}
{{- dict "spec" (($o.spec) | default dict) "managed" ((($o.status) | default dict).managed | default list) | toJson -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* The version a HELD component is re-emitted at: <label>, the version it was last written
     through, while <kind>'s CRD still serves it; otherwise <emitVer>, the version compositions.yaml
     resolved. A pure function of its arguments, so every branch is asserted with synthetic input.

     The label is preferred because a held write must change nothing (osh.holdValues). The fallback
     is for a label that is absent (an object written before the policy existed, or with the label
     removed by hand) or names a version no longer served: emitting there would 500 the render ("no
     matches for kind"), and a release that stops rendering is uninstalled (#68). <emitVer> is
     served by construction -- the gate already passed on it -- so the answer is always a version
     the cluster serves, which is the invariant osh.resolveVersion keeps for a live object (#166). */}}
{{- define "osh.holdVersion" -}}
{{- $label := index . 0 | toString -}}{{- $emitVer := index . 1 | toString -}}{{- $kind := index . 2 -}}{{- $crds := index . 3 -}}
{{- if and (ne $label "") (eq (include "osh.crdExists" (list $kind $label $crds)) "true") -}}
{{- $label -}}
{{- else -}}
{{- $emitVer -}}
{{- end -}}
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

{{/* The infrastructure capabilities this install provides, space-joined, for components[].requires.
     block-storage is provided by storage.backend when it is not none (#196, #308). Nothing in the
     umbrella provides the others components list (pxe-interface, lb-mgmt-net, rwx-storage-class):
     they exist only as preconditions an operator meets by hand (#163), so profile full-strict never
     selects their components and `enabled:` is how to take one. hack/validate-provisioning-preconditions.py
     asserts every capability a component requires is declared in hack/provisioning-preconditions.yaml. */}}
{{- define "osh.providedCapabilities" -}}
{{- $top := index . 0 -}}
{{- $out := list -}}
{{- if ne (include "osh.storageBackend" (list $top)) "none" -}}{{- $out = append $out "block-storage" -}}{{- end -}}
{{- $out | join " " -}}
{{- end -}}

{{/* "true" when storage.backend is a Ceph backend (ceph-rook, ceph-external): the `source: provided` credential items
     are copied and passed, and the charts consume Ceph through them (#308). */}}
{{- define "osh.cephActive" -}}
{{- $top := index . 0 -}}
{{- if hasPrefix "ceph-" (include "osh.storageBackend" (list $top)) -}}true{{- end -}}
{{- end -}}

{{/* The namespace the provided items are copied from: storage.ceph.sourceNamespace, or this release's. */}}
{{- define "osh.cephSourceNamespace" -}}
{{- $top := index . 0 -}}
{{- ((($top.Values.storage | default dict).ceph | default dict).sourceNamespace | default $top.Release.Namespace) -}}
{{- end -}}

{{/* (list $ $item): where a `source: provided` item is copied from, as JSON {"secret", "key"}. ceph-rook follows Rook's
     conventions (storage.ceph.rook: a CephClient's Secret and key, the monitors' Secret and key); ceph-external reads the
     operator-named storage.ceph.keys.<ceph>, and refuses an empty secret rather than copy from nothing. */}}
{{- define "osh.cephSource" -}}
{{- $top := index . 0 -}}{{- $i := index . 1 -}}
{{- $ceph := (($top.Values.storage | default dict).ceph | default dict) -}}
{{- $backend := include "osh.storageBackend" (list $top) -}}
{{- if eq $backend "ceph-rook" -}}
  {{- $rook := $ceph.rook | default dict -}}
  {{- if eq $i.ceph "monHost" -}}
    {{- dict "secret" $rook.monHostSecret "key" $rook.monHostKey | toJson -}}
  {{- else -}}
    {{- $client := index ($ceph.clients | default dict) $i.ceph -}}
    {{- if not $client -}}{{- fail (printf "\n\nstorage.ceph.clients.%s is empty: credential %s copies that Ceph user's key.\n" $i.ceph $i.id) -}}{{- end -}}
    {{- dict "secret" (printf "%s%s" $rook.clientSecretPrefix $client) "key" $rook.clientKey | toJson -}}
  {{- end -}}
{{- else if eq $backend "ceph-external" -}}
  {{- $k := index ($ceph.keys | default dict) $i.ceph | default dict -}}
  {{- if or (not $k.secret) (not $k.key) -}}
    {{- fail (printf "\n\nstorage.backend is ceph-external and storage.ceph.keys.%s.secret (or .key) is empty.\n\nWith ceph-external the Ceph admin creates the users and places their keys and the monitor addresses in Secrets; name each one under storage.ceph.keys (in storage.ceph.sourceNamespace) so credential %s can be copied from it (#308).\n" $i.ceph $i.id) -}}
  {{- end -}}
  {{- dict "secret" $k.secret "key" $k.key | toJson -}}
{{- else -}}
  {{- fail (printf "osh.cephSource called with storage.backend %s, which is not a ceph backend" $backend) -}}
{{- end -}}
{{- end -}}

{{/* (list $ $c): "true" when component $c consumes Ceph in this install (#308): storage.backend is a ceph backend and
     $c's components[].ceph role applies -- `volumes` (cinder, libvirt) always, `images` (glance) when
     storage.ceph.glanceStore is rbd. Only then does it receive its provided items and its storage values. */}}
{{- define "osh.cephConsumes" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- if eq (include "osh.cephActive" (list $top)) "true" -}}
{{- if eq ($c.ceph | default "") "volumes" -}}true
{{- else if and (eq ($c.ceph | default "") "images") (eq ((($top.Values.storage | default dict).ceph | default dict).glanceStore | default "pvc") "rbd") -}}true
{{- end -}}
{{- end -}}
{{- end -}}

{{/* (list $ $c): the values the umbrella sets on component $c for the install's Ceph, as YAML, or nothing (#308). One
     source -- storage.ceph -- for every value the charts must agree on: the pools, the users, the libvirt secret UUID.
     cinder: its rbd1 backend; glance: the rbd store; libvirt: the secret it defines; nova: the UUID and user its
     [libvirt] section names (nova consumes no key in this release). Merged over componentValues, which may not set any
     of them (osh.requireStorageNotOverridden); hack/validate-ceph-consumer.py (check 29) renders the four charts. */}}
{{- define "osh.storageValues" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- $ceph := (($top.Values.storage | default dict).ceph | default dict) -}}
{{- $out := dict -}}
{{- if eq (include "osh.cephActive" (list $top)) "true" -}}
  {{- $uuid := $ceph.libvirtSecretUUID -}}{{- $cinderUser := $ceph.clients.cinder -}}
  {{- if eq $c.name "cinder" -}}
    {{- $_ := set $out "conf" (dict "backends" (dict "rbd1" (dict "rbd_pool" $ceph.pools.volumes "rbd_user" $cinderUser "rbd_secret_uuid" $uuid))) -}}
  {{- else if eq $c.name "libvirt" -}}
    {{- $_ := set $out "conf" (dict "ceph" (dict "enabled" true "cinder" (dict "user" $cinderUser "secret_uuid" $uuid))) -}}
  {{- else if eq $c.name "nova" -}}
    {{- $_ := set $out "conf" (dict "ceph" (dict "cinder" (dict "user" $cinderUser "secret_uuid" $uuid)) "nova" (dict "libvirt" (dict "rbd_user" $cinderUser "rbd_secret_uuid" $uuid))) -}}
  {{- else if and (eq $c.name "glance") (eq (include "osh.cephConsumes" (list $top $c)) "true") -}}
    {{- $rbd := dict "rbd_store_pool" $ceph.pools.images "rbd_store_user" $ceph.clients.glance -}}
    {{- $_ := set $out "storage" "rbd" -}}
    {{- $_ := set $out "krateo" (dict "imagesMigratedFromPvc" ($ceph.glanceImagesMigrated | default false)) -}}
    {{- $_ := set $out "conf" (dict "glance" (dict "rbd" $rbd "glance_store" (deepCopy $rbd) "DEFAULT" (dict "show_image_direct_url" true))) -}}
  {{- end -}}
{{- end -}}
{{- if $out -}}{{- toYaml $out -}}{{- end -}}
{{- end -}}

{{/* (dict "node" <map> "prefix" <path>): every leaf path of a nested map, one per line. */}}
{{- define "osh.leafPaths" -}}
{{- $prefix := .prefix -}}
{{- range $k, $v := .node -}}
{{- $p := ternary $k (printf "%s.%s" $prefix $k) (eq $prefix "") -}}
{{- if kindIs "map" $v -}}{{- include "osh.leafPaths" (dict "node" $v "prefix" $p) -}}{{- else -}}{{- $p }}
{{ end -}}
{{- end -}}
{{- end -}}

{{/* (list $ <name>): the ExternalSecret <name> in this namespace, as JSON ("{}" when absent). One named GET; the umbrella
     renders these objects (the generation roots), so its render identity may read them. A rotation also reads the
     component's own projections through osh.lookupNamed (osh.generationRolled). */}}
{{- define "osh.lookupExternalSecret" -}}
{{- $top := index . 0 -}}
{{- lookup "external-secrets.io/v1" "ExternalSecret" $top.Release.Namespace (index . 1) | default dict | toJson -}}
{{- end -}}

{{/* (list $): the copy store <credentials.store>-ceph, as JSON ("{}" when absent). One named GET. */}}
{{- define "osh.lookupCephStore" -}}
{{- $top := index . 0 -}}
{{- lookup "external-secrets.io/v1" "SecretStore" $top.Release.Namespace (printf "%s-ceph" $top.Values.credentials.store) | default dict | toJson -}}
{{- end -}}

{{/* (obj): "True", "False" or "" -- the status of obj's Ready condition, judged on type and status only. */}}
{{- define "osh.readyStatus" -}}
{{- $out := "" -}}
{{- range $c := ((. | default dict).status | default dict).conditions | default list -}}
{{- if eq ($c.type | default "") "Ready" -}}{{- $out = toString $c.status -}}{{- end -}}
{{- end -}}
{{- $out -}}
{{- end -}}

{{/* (list $ $c): "true" when component $c may be CREATED as far as Ceph is concerned (#308): it consumes no Ceph here,
     or every provided root it lists reports Ready=True -- the copy of the key Ceph minted exists. Until then the
     component is held, exactly like a dependency that is not Ready yet: listed in openstack-install-pending, never
     created on a key nothing has copied, so no post-install hook of it can time out on a missing grant or Secret. Only
     creation: compositions.yaml keeps an existing component (`or $exists`), since a component that stops rendering is
     uninstalled (#68). hack/validate-ceph-consumer.py (check 30) renders it with stand-in ExternalSecrets. */}}
{{- define "osh.cephRootsReady" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- $ready := true -}}
{{- if eq (include "osh.cephConsumes" (list $top $c)) "true" -}}
{{- range $id := ($c.credentials | default list) -}}
{{- range $i := ($top.Values.credentials.items | default list) -}}
{{- if and (eq $i.id $id) (eq ($i.source | default "") "provided") -}}
{{- if ne (include "osh.readyStatus" (include "osh.lookupExternalSecret" (list $top $i.secret) | fromJson)) "True" -}}{{- $ready = false -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $ready -}}
{{- end -}}

{{/* (list $): with a ceph backend, refuse the render while the copy store reports Ready=False (#308). External Secrets
     Operator validates a kubernetes-provider store by asking, in storage.ceph.sourceNamespace, whether its ServiceAccount
     may read Secrets there (that is why its Role carries selfsubjectrulesreviews), so Ready=False is ESO's measurement
     that the read grant is missing or wrong -- typically the Role that namespace's owner must create. Refused with the
     exact grant, on the Composition's status, rather than holding cinder with no reason given. A store not yet
     validated (no condition) only holds the consumers (osh.cephRootsReady). Check 30 renders both. */}}
{{- define "osh.requireCephReadGrant" -}}
{{- $top := index . 0 -}}
{{- if eq (include "osh.cephActive" (list $top)) "true" -}}
{{- $store := include "osh.lookupCephStore" (list $top) | fromJson -}}
{{- if eq (include "osh.readyStatus" $store) "False" -}}
{{- $msg := "" -}}
{{- range $c := ($store.status | default dict).conditions | default list -}}{{- if eq ($c.type | default "") "Ready" -}}{{- $msg = $c.message | default "" -}}{{- end -}}{{- end -}}
{{- $ns := include "osh.cephSourceNamespace" (list $top) -}}
{{- $sa := printf "%s-ceph-reader" $top.Values.credentials.store -}}
{{- $names := list -}}
{{- range $i := ($top.Values.credentials.items | default list) -}}{{- if eq ($i.source | default "") "provided" -}}{{- $names = append $names (include "osh.cephSource" (list $top $i) | fromJson).secret -}}{{- end -}}{{- end -}}
{{- fail (printf "\n\nThe Ceph copy store %s-ceph cannot read the key Secrets in namespace %s (#308). External Secrets Operator reports: %s\n\nGrant the ServiceAccount %s/%s get on exactly %s there, and create on selfsubjectrulesreviews -- the Role and RoleBinding in the README's \"Storage: consuming a Ceph cluster\". Until then no Ceph consumer (cinder, libvirt, glance with rbd) is created.\n" $top.Values.credentials.store $ns $msg $top.Release.Namespace $sa (join ", " ($names | uniq))) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* storage.backend, `none` when unset. */}}
{{- define "osh.storageBackend" -}}
{{- $top := index . 0 -}}
{{- (($top.Values.storage | default dict).backend | default "none") -}}
{{- end -}}

{{/* The umbrella's storage.backend is the one statement of which block-storage backend the install has: it is what
     full-strict selects cinder by, and what cinder renders by. A componentValues.<component>.krateo.storageBackend would
     let the two disagree (full-strict leaving cinder out while cinder renders a backend, or the reverse), so it is
     refused, for every component that requires block-storage. hack/validate-storage-backend.py renders the refusal. */}}
{{- define "osh.requireStorageNotOverridden" -}}
{{- $top := index . 0 -}}
{{- range $c := $top.Values.components -}}
  {{- $cv := (index ($top.Values.componentValues | default dict) $c.name) | default dict -}}
  {{- if has "block-storage" ($c.requires | default list) -}}
    {{- if hasKey ($cv.krateo | default dict) "storageBackend" -}}
      {{- fail (printf "\n\ncomponentValues.%s.krateo.storageBackend is set. Set storage.backend instead: it is the one value that says which block-storage backend this install has, and both profile full-strict and %s read it (#196). The umbrella passes it to %s as krateo.storageBackend.\n" $c.name $c.name $c.name) -}}
    {{- end -}}
  {{- end -}}
  {{- /* #308: the Ceph values come from storage.ceph alone, so the charts cannot disagree on a pool, a user or the
         libvirt secret UUID; and Ceph is reached only through the copies of the keys it mints, so a component is not
         wired to Ceph by hand (that path needs client.admin): componentValues may set none of the paths
         osh.storageValues sets, nor the references (krateo.ceph), nor glance's storage rbd/radosgw, nor
         conf.ceph.enabled on libvirt or nova. */ -}}
  {{- $paths := list "krateo.ceph" -}}
  {{- with include "osh.storageValues" (list $top $c) -}}{{- $paths = concat $paths (splitList "\n" (include "osh.leafPaths" (dict "node" (fromYaml .) "prefix" "")) | compact) -}}{{- end -}}
  {{- if has $c.name (list "libvirt" "nova") -}}{{- $paths = append $paths "conf.ceph.enabled" -}}{{- end -}}
  {{- range $path := $paths -}}
    {{- $v := $cv -}}{{- $found := true -}}
    {{- range $p := splitList "." $path -}}
      {{- if and $found (kindIs "map" $v) (hasKey $v $p) -}}{{- $v = index $v $p -}}{{- else -}}{{- $found = false -}}{{- end -}}
    {{- end -}}
    {{- if and $found (or (ne $path "conf.ceph.enabled") $v) -}}
      {{- fail (printf "\n\ncomponentValues.%s.%s is set. The umbrella sets it from storage.ceph (#308): one value for every chart that must agree on it, and Ceph reached only through the copies of the keys it mints, never through client.admin. Set storage.backend and storage.ceph instead.\n" $c.name $path) -}}
    {{- end -}}
  {{- end -}}
  {{- if and (eq $c.name "glance") (has ($cv.storage | default "") (list "rbd" "radosgw")) -}}
    {{- fail (printf "\n\ncomponentValues.glance.storage is %s. Glance stores images in Ceph only through storage.ceph.glanceStore rbd with a ceph storage.backend (#308): upstream's %s path reaches Ceph with client.admin.\n" $cv.storage $cv.storage) -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{/* The set of component names to deploy, space-joined. If `enabled` is non-empty it
     is an explicit selection expanded to its transitive dependency closure (so
     `enabled: [heat]` pulls in keystone, rabbitmq, mariadb, memcached). Otherwise it
     falls back to the `profile`: identity tier always, plus compute when profile=full,
     plus compute minus unmet components[].requires when profile=full-strict. */}}
{{- define "osh.selected" -}}
{{- $top := index . 0 -}}
{{- $comps := $top.Values.components -}}
{{- $enabled := $top.Values.enabled | default list -}}
{{- $sel := dict -}}
{{- if gt (len $enabled) 0 -}}
  {{- /* A `monitoring: true` component (openstack-monitoring, #312) has one switch, the `monitoring` value: listed here
         it would be selected while `monitoring.enabled: false` says it is not, or skip the credential root and the
         mariadb user that the value brings with it. Refused, naming the value. */ -}}
  {{- range $c := $comps -}}
    {{- if and $c.monitoring (has $c.name $enabled) -}}
      {{- fail (printf "enabled lists %s, which is selected by the `monitoring` value alone: set monitoring.enabled: true instead (README \"Monitoring\")" $c.name) -}}
    {{- end -}}
  {{- end -}}
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

       A component whose `requires` names a capability this install does not provide
       (osh.providedCapabilities) is excluded from `full-strict`. It is not unsupported and nothing
       about it is removed -- it is selectable, and fully gated, through `enabled:`, and `full`
       still selects it. What it is not is DEFAULT, because it waits on infrastructure that no
       Kubernetes cluster provides and that this blueprint does not create: an interface named
       ironic-pxe, a Neutron port per host on octavia's management network, a ReadWriteMany
       StorageClass for swift. cinder's block-storage is the one capability the umbrella can be
       told it has, by storage.backend (#196); then full-strict selects cinder.

       Measured on a 4-node GKE install of profile=full: six pods at 14 restarts in Init plus
       four Pending, none of which could EVER become Ready, and `Init:` is the state a pod
       census can only count -- neither Pending nor CrashLoopBackOff, so "21 in Init" names
       nothing. A profile that installs components which cannot start is a promise that cannot
       be kept, and the cost of keeping it is paid by whoever next reads that census.

       hack/validate-provisioning-preconditions.py asserts `requires` and the declared
       precondition agree in BOTH directions, so a component that requires something must say
       what it needs and a declared precondition must belong to a component that requires it, and
       it renders this selection with storage.backend none AND with a backend, so the capability
       is shown to be read and not merely declared. Issues #163, #196. */}}
  {{- $provided := splitList " " (include "osh.providedCapabilities" (list $top)) | compact -}}
  {{- range $c := $comps -}}
    {{- $unmet := false -}}
    {{- range $r := ($c.requires | default list) -}}
      {{- if not (has $r $provided) -}}{{- $unmet = true -}}{{- end -}}
    {{- end -}}
    {{/* `full` is unchanged and still selects every component, including the four that need
         infrastructure an operator must provide. `full-strict` selects everything EXCEPT those
         whose requirement the install does not provide, so it is the profile that promises only
         what can start on the cluster as described.

         Opt-in rather than a change to `full`, deliberately: a profile silently selecting three
         fewer components than it did yesterday is a surprise delivered through a patch release,
         and on 0.3.x the version number cannot signal it. `full-strict` is discoverable, says
         what it means, and leaves anyone who HAS provisioned a PXE interface, an lb-mgmt-net or
         an RWX StorageClass exactly where they were. */}}
    {{- if and (not $c.monitoring)
               (or (eq $top.Values.profile "full")
                   (and (eq $top.Values.profile "full-strict") (not $unmet))
                   (eq $c.tier "identity")) -}}
      {{- $_ := set $sel $c.name true -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- /* Monitoring (#312): the `monitoring: true` components join the selection exactly when osh.monitoringOn says so,
       with their dependency closure. Off, nothing here runs and the selection is what it was before #312. */ -}}
{{- if include "osh.monitoringOn" (list $top) -}}
  {{- range $c := $comps -}}
    {{- if $c.monitoring -}}{{- $_ := set $sel $c.name true -}}{{- end -}}
  {{- end -}}
  {{- range $i := until 8 -}}
    {{- range $c := $comps -}}
      {{- if hasKey $sel $c.name -}}
        {{- range $d := ($c.deps | default list) -}}{{- $_ := set $sel $d true -}}{{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- keys $sel | sortAlpha | join " " -}}
{{- end -}}

{{/* osh.monitoringOn (list $): "true" when the openstack-monitoring component is on (#312). Explicit opt-in (Diego,
     2026-11-18): only monitoring.enabled true turns it on; unset (null) is off, exactly like false. Nothing about the
     cluster switches it on. templates/monitoring-precondition.yaml refuses `true` where alert-provider's CRDs are not
     served. */}}
{{- define "osh.monitoringOn" -}}
{{- $top := index . 0 -}}
{{- $m := $top.Values.monitoring | default dict -}}
{{- if and (kindIs "bool" $m.enabled) $m.enabled }}true{{ end -}}
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
       when it names this component (a chart holding the same credential under another key), and again at
       each of the item's `extraPaths` (one credential a chart holds under several keys). An SSHKey
       item's reference also carries publicKey, the root's key holding the public half. A
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
{{- /* A `source: provided` item (#308) has a root only while storage.backend is a ceph backend: otherwise there is
       nothing to copy, and the component gets no reference, exactly as before the item existed. */ -}}
{{- if or (ne ($item.source | default "") "provided") (eq (include "osh.cephConsumes" (list $top $c)) "true") -}}
{{- $ref := dict "store" $creds.store "secret" $item.secret "key" $item.key -}}
{{- if $item.publicKey -}}{{- $_ := set $ref "publicKey" $item.publicKey -}}{{- end -}}
{{- $path := $item.path -}}
{{- if hasKey ($item.pathFor | default dict) $c.name -}}{{- $path = index $item.pathFor $c.name -}}{{- end -}}
{{- /* extraPaths: the same reference at further paths, for a credential a chart holds under several keys (nova's
       one MariaDB account under oslo_db, oslo_db_api and oslo_db_cell0): one root, never one per key. */ -}}
{{- range $path := prepend ($item.extraPaths | default list) $path -}}
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
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end -}}

{{- /* osh.runsRabbitInit (list $ $c): "true" when component $c's rabbit-init runs -- in chart mode its Job, with
       messaging.backend operator its topology resources: the Job is on (its componentValues manifests.job_rabbit_init when
       set, else its chart default, components[].rabbitmqInit, true unless false) and so is the Secret it reads
       (manifests.secret_rabbitmq, else rabbitmqSecret: keystone ships it off). The one rule osh.notificationVhostSources
       and osh.messagingProjection both read. */ -}}
{{- define "osh.runsRabbitInit" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- $cv := index ($top.Values.componentValues | default dict) $c.name | default dict -}}
{{- $man := $cv.manifests | default dict -}}
{{- $init := ne (toString $c.rabbitmqInit) "false" -}}
{{- if hasKey $man "job_rabbit_init" -}}{{- $init = eq (toString (index $man "job_rabbit_init")) "true" -}}{{- end -}}
{{- $secret := ne (toString $c.rabbitmqSecret) "false" -}}
{{- if hasKey $man "secret_rabbitmq" -}}{{- $secret = eq (toString (index $man "secret_rabbitmq")) "true" -}}{{- end -}}
{{- if and $init $secret -}}true{{- end -}}
{{- end -}}

{{- /* osh.messagingProjection (list $ $c): with messaging.backend operator, the RabbitMQ user projection the UMBRELLA
       writes for component $c (#309 H2 run 4, option 4), as JSON {secret, username, store, root, key}; "{}" when $c has
       none (another backend, not in haTopology.messagingQuorum, or its rabbit-init does not run).
       Why the umbrella: the topology operator's User webhook refuses a User whose import Secret does not exist yet
       (v1.20.5 label_validation.go), and a component's chart submits its ExternalSecret and its User in one helm
       transaction while ESO writes the Secret afterwards (3-85 s measured). Run 4 measured every messaging component
       refused 1-2 times and converging on CDC's retry after up to 198 s. Rendered here, the projection exists before the
       component is created (osh.messagingProjectionReady gates creation on it), so its User is admitted at once.
       The username is the chart's auth.<component>.username: componentValues' when set, else the component name, which
       every messaging chart's default equals (hack/validate-messaging-topology.py checks both). The root is the
       component's oslo-messaging-<component> credential item, read through credentials.store as every reference is. */ -}}
{{- define "osh.messagingProjection" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- $out := dict -}}
{{- $topo := $top.Values.haTopology | default dict -}}
{{- if and (eq (include "osh.messagingBackend" $top) "operator") (has $c.name ($topo.messagingQuorum | default list)) (eq (include "osh.runsRabbitInit" (list $top $c)) "true") -}}
{{- $creds := $top.Values.credentials | default dict -}}
{{- $id := printf "oslo-messaging-%s" $c.name -}}
{{- $item := dict -}}
{{- range $i := ($creds.items | default list) -}}{{- if eq $i.id $id -}}{{- $item = $i -}}{{- end -}}{{- end -}}
{{- if not $item.secret -}}
{{- fail (printf "\n\n%s runs rabbit-init with messaging.backend operator, but credentials.items has no %s: its RabbitMQ user projection would have no root (#228, #309).\n" $c.name $id) -}}
{{- end -}}
{{- $cv := index ($top.Values.componentValues | default dict) $c.name | default dict -}}
{{- $user := ((((($cv.endpoints | default dict).oslo_messaging | default dict).auth | default dict) | dig $c.name "username" "") | toString) | default $c.name -}}
{{- $_ := set $out "secret" (printf "%s-rabbitmq-topology-user" ($c.name | replace "_" "-")) -}}
{{- $_ := set $out "username" $user -}}
{{- $_ := set $out "store" $creds.store -}}
{{- $_ := set $out "root" $item.secret -}}
{{- $_ := set $out "key" $item.key -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- /* osh.messagingProjectionES (list $ $c $p suffix root): the umbrella's RabbitMQ user projection of component $c
       (osh.messagingProjection is $p) for one generation (#309 C2): named <projection><suffix>, user <username><suffix>,
       read from <root>. Generation 0 (suffix "", the base root) is templates/messaging-projections.yaml; the others are
       rendered with the rotation that needs them (compositions.yaml). One template, so they cannot differ. */ -}}
{{- define "osh.messagingProjectionES" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}{{- $p := index . 2 -}}{{- $sfx := toString (index . 3) -}}{{- $root := index . 4 }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ printf "%s%s" $p.secret $sfx }}
  namespace: {{ $top.Release.Namespace }}
  labels:
    krateo.io/messaging-projection: {{ $c.name }}
  annotations:
    # Deleted by helm WITH the release, after its pre-delete hooks (#254).
    krateo.io/teardown: with-release
spec:
  # OnChange (#309 C): refreshed only when this ExternalSecret itself changes (a new root, a new
  # template), never on a timer, so a root edited by hand (which ESO regenerates) reaches no live
  # credential until a rotation; the umbrella reports the edit (status.credentialDrift).
  refreshPolicy: OnChange
  secretStoreRef:
    kind: SecretStore
    name: {{ $p.store | quote }}
  target:
    name: {{ printf "%s%s" $p.secret $sfx }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      metadata:
        labels:
          # The topology operator reads only Secrets with this label (v1.20.5 cmd/main.go:281-283), and its User webhook
          # refuses an import Secret without it (internal/webhook/v1beta1/label_validation.go). #404.
          rabbitmq.com/topology-operator: "true"
      data:
        username: {{ printf "%s%s" $p.username $sfx | quote }}
        password: {{ "{{ .password }}" | quote }}
  data:
    - secretKey: password
      remoteRef:
        key: {{ $root | quote }}
        property: {{ $p.key | quote }}
{{- end -}}

{{- /* osh.lookupSecret (list $ name): the Secret <name> in the release namespace, as JSON ("{}" when absent). One named
       GET, so a gate can stand it in: helm template's lookup returns nothing, and an offline render therefore emits no
       component that waits on one (hack/validate-messaging-operator.py part 5 renders this gate with a stand-in). */ -}}
{{- define "osh.lookupSecret" -}}
{{- $top := index . 0 -}}
{{- lookup "v1" "Secret" $top.Release.Namespace (index . 1) | default dict | toJson -}}
{{- end -}}

{{- /* osh.messagingRotation (list $ $c $liveSpec [$managed]): the rotation state of component $c's RabbitMQ user (#309 C,
       Diego's (d): immutable roots per generation, dual users), derived from what exists, never from remembered state, as
       JSON. "{}" when $c has no umbrella projection (osh.messagingProjection). $managed is the live component
       Composition's status.managed (CDC builds it from the release manifest).
         base     its root, credentials.items oslo-messaging-<component>: generation 0, never modified;
         target   base when rotate.<id> is unset, else <base>-g<first 8 hex of sha256(token)>, a NEW root ESO mints once;
         active   the root the LIVE component Composition references now ($liveSpec's
                  endpoints.oslo_messaging.auth.<component>.passwordFrom.secret), base for a component not created yet;
         previous for a target other than base, the active root when the target was first rendered, stamped on the target
                  root's ExternalSecret (krateo.io/previous-root) and read back by lookup: the ConfigMap
                  openstack-credential-generations is not needed, so losing it loses nothing;
         refuse   a message when the token cannot be honoured: the target root already exists with a previous-root other
                  than the active one (the token names an old or retired generation), the token was removed after a
                  rotation (target base, active a generation root), or componentValues sets the component's
                  passwordFrom (the rotation would override it). Never silently re-activates an old root.
       C2, the switch. A generation's suffix is "" for the base root and -g<8 hex> otherwise; its user is
       <username><suffix>, its projection <projection><suffix>, its User <su>-rabbitmq-user<suffix>. One render at a time:
         minting   target != active. The target root and its umbrella projection are rendered; once that projection's
                   Secret exists, labelled for the topology operator, the target's User joins `users` (the component
                   renders it next to the active one, on the same vhost); once that User is Ready, this render SWITCHES:
                   the component's passwordFrom and username name the target, activeSuffix its suffix. A target waits
                   while the active generation's own previous user is still draining (never three users);
         draining  target == active, and the previous generation's User still exists: it stays in `users` until every
                   reader has rolled onto the active generation (osh.generationRolled); `blockers` says what each waits on.
                   Once rolled it leaves `users`, the component's release deletes its User, and the topology operator its
                   RabbitMQ user;
         steady    nothing to do.
       `pass` is false until a first rotation: the component's spec does not change (hack/validate-messaging-rotation.py
       renders all three phases from stand-ins, and that nothing changes without a token). */ -}}
{{- define "osh.messagingRotation" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}{{- $live := index . 2 | default dict -}}
{{- $managed := list -}}{{- if gt (len .) 3 -}}{{- $managed = index . 3 | default list -}}{{- end -}}
{{- $p := include "osh.messagingProjection" (list $top $c) | fromJson -}}
{{- $out := dict -}}
{{- if $p.secret -}}
{{- $id := printf "oslo-messaging-%s" $c.name -}}
{{- $base := $p.root -}}
{{- $tok := toString (index ($top.Values.rotate | default dict) $id | default "") -}}
{{- $target := $base -}}
{{- if $tok -}}{{- $target = printf "%s-g%s" $base (trunc 8 (sha256sum $tok)) -}}{{- end -}}
{{- $active := (((((($live.endpoints | default dict).oslo_messaging | default dict).auth | default dict) | dig $c.name "passwordFrom" "secret" "") | toString) | default $base) -}}
{{- $_ := set $out "base" $base -}}{{- $_ := set $out "target" $target -}}{{- $_ := set $out "active" $active -}}
{{- $_ := set $out "token" $tok -}}{{- $_ := set $out "id" $id -}}
{{- $es := include "osh.lookupExternalSecret" (list $top $target) | fromJson -}}
{{- $stamp := toString ((($es.metadata | default dict).annotations | default dict) | dig "krateo.io/previous-root" "") -}}
{{- $cvAuth := (((((index ($top.Values.componentValues | default dict) $c.name | default dict).endpoints | default dict).oslo_messaging | default dict).auth | default dict) | dig $c.name "passwordFrom" dict) -}}
{{- if and $tok $cvAuth -}}
{{- $_ := set $out "refuse" (printf "rotate.%s is set, but componentValues.%s.endpoints.oslo_messaging.auth.%s.passwordFrom is too: a rotation names the root itself. Remove the componentValues entry." $id $c.name $c.name) -}}
{{- else if eq $target $active -}}
{{- $_ := set $out "phase" "steady" -}}
{{- if ne $target $base -}}{{- $_ := set $out "previous" $stamp -}}{{- end -}}
{{- else if eq $target $base -}}
{{- $_ := set $out "refuse" (printf "rotate.%s was removed while %s uses the generation root %s: removing a rotation token would re-activate the old root %s. Keep the token, or set a new one to rotate again." $id $c.name $active $base) -}}
{{- else if and $es.metadata (ne $stamp $active) -}}
{{- $_ := set $out "refuse" (printf "rotate.%s token %q maps to generation root %s, which already exists (previous root %s) while %s uses %s: the token names an old generation. Set a new token." $id $tok $target ($stamp | default "none") $c.name $active) -}}
{{- else -}}
{{- $_ := set $out "phase" "minting" -}}
{{- $_ := set $out "previous" ($stamp | default $active) -}}
{{- end -}}
{{- if not $out.refuse -}}
{{- $su := $c.name | replace "_" "-" -}}
{{- $aSfx := include "osh.generationSuffix" (list $base $active) -}}
{{- $users := list -}}{{- $roots := list -}}{{- $blockers := list -}}
{{- /* The ACTIVE generation's own previous one (stamped on the active root): kept while its User exists and the
       readers have not all rolled onto the active generation. */ -}}
{{- $draining := false -}}
{{- if ne $active $base -}}
{{- $aes := include "osh.lookupExternalSecret" (list $top $active) | fromJson -}}
{{- $prev := toString ((($aes.metadata | default dict).annotations | default dict) | dig "krateo.io/previous-root" "") -}}
{{- if $prev -}}
{{- $pSfx := include "osh.generationSuffix" (list $base $prev) -}}
{{- $pu := include "osh.lookupNamed" (list $top "rabbitmq.com/v1beta1" "User" (printf "%s-rabbitmq-user%s" $su $pSfx)) | fromJson -}}
{{- if and $pu.metadata (not $pu.metadata.deletionTimestamp) -}}
{{- if ne $prev $base -}}{{- $roots = append $roots $prev -}}{{- end -}}
{{- $r := include "osh.generationRolled" (list $top $c $aSfx $active $managed) | fromJson -}}
{{- if not $r.rolled -}}
{{- $draining = true -}}
{{- $users = append $users (dict "suffix" $pSfx "secret" (printf "%s%s" $p.secret $pSfx)) -}}
{{- $blockers = concat $blockers (list (printf "the previous generation %s is kept until every reader has rolled onto %s:" $prev $active)) $r.blockers -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $users = append $users (dict "suffix" $aSfx "secret" (printf "%s%s" $p.secret $aSfx)) -}}
{{- if ne $active $base -}}{{- $roots = append $roots $active -}}{{- end -}}
{{- $final := $active -}}{{- $fSfx := $aSfx -}}
{{- if ne $target $active -}}
{{- $tSfx := include "osh.generationSuffix" (list $base $target) -}}
{{- $roots = append $roots $target -}}
{{- if $draining -}}
{{- $blockers = append $blockers (printf "the new generation %s waits until the previous rotation has drained" $target) -}}
{{- else -}}
{{- $tp := include "osh.lookupSecret" (list $top (printf "%s%s" $p.secret $tSfx)) | fromJson -}}
{{- if ne (toString ((($tp.metadata | default dict).labels | default dict) | dig "rabbitmq.com/topology-operator" "")) "true" -}}
{{- $blockers = append $blockers (printf "waiting for External Secrets to write the projection %s%s from %s" $p.secret $tSfx $target) -}}
{{- else -}}
{{- $users = append $users (dict "suffix" $tSfx "secret" (printf "%s%s" $p.secret $tSfx)) -}}
{{- $tu := include "osh.lookupNamed" (list $top "rabbitmq.com/v1beta1" "User" (printf "%s-rabbitmq-user%s" $su $tSfx)) | fromJson -}}
{{- $ready := false -}}
{{- range $cond := (($tu.status | default dict).conditions | default list) -}}
{{- if and (eq (toString $cond.type) "Ready") (eq (toString $cond.status) "True") -}}{{- $ready = true -}}{{- end -}}
{{- end -}}
{{- if $ready -}}
{{- $final = $target -}}{{- $fSfx = $tSfx -}}
{{- $_ := set $out "switched" true -}}
{{- $blockers = append $blockers (printf "switched to %s in this render; %s is kept until every reader has rolled" $target $active) -}}
{{- else -}}
{{- $blockers = append $blockers (printf "waiting for the User %s-rabbitmq-user%s to be Ready" $su $tSfx) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $phase := "steady" -}}
{{- if $out.switched -}}{{- $phase = "draining" -}}
{{- else if ne $target $active -}}{{- $phase = "minting" -}}
{{- else if $draining -}}{{- $phase = "draining" -}}
{{- end -}}
{{- $_ := set $out "phase" $phase -}}
{{- $_ := set $out "su" $su -}}
{{- $_ := set $out "users" $users -}}
{{- $_ := set $out "roots" $roots -}}
{{- $_ := set $out "blockers" $blockers -}}
{{- $_ := set $out "activeRoot" $final -}}
{{- $_ := set $out "activeSuffix" $fSfx -}}
{{- $_ := set $out "username" (printf "%s%s" $p.username $fSfx) -}}
{{- $_ := set $out "pass" (or (ne $active $base) (ne $target $base)) -}}
{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- /* osh.generationSuffix (list base root): "" for the base root (generation 0), else what the root's name adds to it
       (-g<8 hex>). */ -}}
{{- define "osh.generationSuffix" -}}
{{- $base := index . 0 -}}{{- $root := index . 1 -}}
{{- if ne $root $base -}}{{- trimPrefix $base $root -}}{{- end -}}
{{- end -}}

{{- /* osh.lookupNamed (list $ apiVersion kind name): the object <kind>/<name> in the release namespace, as JSON ("{}" when
       absent). One named GET: chart-inspector's tracer records every named request of the dry-run render, lookups and
       404s included (internal/tracer/tracer.go RoundTrip), and derives the release's RBAC from them. Never a list.
       Stood in by hack/validate-messaging-rotation.py. */ -}}
{{- define "osh.lookupNamed" -}}
{{- $top := index . 0 -}}
{{- lookup (index . 1) (index . 2) $top.Release.Namespace (index . 3) | default dict | toJson -}}
{{- end -}}

{{- /* osh.nowUnix ($): the render's wall-clock time, Unix seconds. One definition, so a gate can stand it in. */ -}}
{{- define "osh.nowUnix" -}}
{{- now | unixEpoch -}}
{{- end -}}

{{- /* osh.hookDeadline (list $ $c): the longest a hook Job of component $c can run, in seconds: helm-toolkit's
       job_hook_deadline gives every hook Job activeDeadlineSeconds, a positive per-Job jobs.<name>.activeDeadlineSeconds
       first, then a positive helm3_hook_deadline_seconds, then 1800. The largest of those componentValues can set.
       hack/validate-messaging-rotation.py checks no messaging chart's hook Job renders a longer one by default. */ -}}
{{- define "osh.hookDeadline" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- $cv := index ($top.Values.componentValues | default dict) $c.name | default dict -}}
{{- $d := 1800 -}}
{{- if gt (int ($cv.helm3_hook_deadline_seconds | default 0)) 0 -}}{{- $d = int $cv.helm3_hook_deadline_seconds -}}{{- end -}}
{{- range $j := ($cv.jobs | default dict) -}}
{{- if kindIs "map" $j -}}{{- if gt (int ($j.activeDeadlineSeconds | default 0)) $d -}}{{- $d = int $j.activeDeadlineSeconds -}}{{- end -}}{{- end -}}
{{- end -}}
{{- $d -}}
{{- end -}}

{{- /* osh.messagingReaders (list $c): the component chart's ExternalSecrets that project its RabbitMQ root, the
       connection Secret (secrets.oslo_messaging) and the transport drop-in. hack/validate-messaging-rotation.py renders
       all 20 messaging charts and fails unless this is exactly the set that reads the root. */ -}}
{{- define "osh.messagingReaders" -}}
{{- $n := (index . 0).name | replace "_" "-" -}}
{{- list (printf "%s-rabbitmq-user" $n) (printf "%s-transport" $n) | toJson -}}
{{- end -}}

{{- /* osh.generationRolled (list $ $c $suffix $activeRoot $managed): whether every reader of component $c's RabbitMQ
       credential has moved to the generation <suffix>, as JSON {rolled, blockers}. Named lookups only; whatever cannot
       be read, or cannot roll by itself, is kept and reported, never forced.

       Workloads, each object $managed lists (the component Composition's status.managed, built by CDC from the release
       manifest), by kubectl v1.36.4's rollout status (staging/src/k8s.io/kubectl/pkg/polymorphichelpers/rollout_status.go)
       and one stricter rule:
         every Deployment, StatefulSet, DaemonSet and CronJob carries the pod-template annotation
           krateo.io/messaging-generation: <suffix> (helm-toolkit custom_pod_annotations): proof that the release which
           switched it was applied. CDC writes no observedGeneration on the Composition (support.go setStatus; the
           unstructured-runtime v1.1.0 conditions set none), so this is the evidence;
         Deployment (:75-87): metadata.generation <= status.observedGeneration; not ProgressDeadlineExceeded;
           updatedReplicas == spec.replicas; status.replicas == updatedReplicas; availableReplicas == updatedReplicas;
         DaemonSet (:107-112): OnDelete refused (kubectl refuses it too: its pods keep the previous credential until
           deleted, so it is reported with that action); observedGeneration; updatedNumberScheduled and numberAvailable
           == desiredNumberScheduled;
         StatefulSet (:130-146): OnDelete as DaemonSet; observedGeneration; readyReplicas == spec.replicas; a partition
           counts as not rolled while any pod is below it (stricter than kubectl, which accepts the partition: those
           pods hold the previous credential); updateRevision == currentRevision;
         CronJob and Job: no Job running (status.active), since a Job keeps the credential it started with;
         a bare Pod not Succeeded or Failed: never restarts, reported.
       Hook Jobs are not in status.managed: CDC builds it from the release manifest, which holds no hooks. Every hook Job
       is bounded by activeDeadlineSeconds (osh.hookDeadline), so the previous user is kept until that long after the
       latest ESO sync (status.refreshTime) of the component's projections of the active root (osh.messagingReaders),
       each of which must read the active root and be synced at its own generation: a pod started after that sync reads
       the new credential, one started before it has ended. */ -}}
{{- define "osh.generationRolled" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}{{- $sfx := toString (index . 2) -}}{{- $root := index . 3 -}}{{- $managed := index . 4 | default list -}}
{{- $blk := list -}}
{{- $kinds := dict "deployments" "Deployment" "statefulsets" "StatefulSet" "daemonsets" "DaemonSet" "cronjobs" "CronJob" "jobs" "Job" "pods" "Pod" -}}
{{- if not $managed -}}{{- $blk = append $blk "the component Composition lists no status.managed yet" -}}{{- end -}}
{{- range $m := $managed -}}
{{- $kind := index $kinds (toString $m.resource) | default "" -}}
{{- if $kind -}}
{{- $o := include "osh.lookupNamed" (list $top (toString $m.apiVersion) $kind (toString $m.name)) | fromJson -}}
{{- $n := printf "%s/%s" $kind $m.name -}}
{{- if not $o.metadata -}}
{{- $blk = append $blk (printf "%s: cannot be read; kept until it can" $n) -}}
{{- else -}}
{{- $sp := $o.spec | default dict -}}{{- $st := $o.status | default dict -}}
{{- $gen := int64 ($o.metadata.generation | default 0) -}}{{- $og := int64 ($st.observedGeneration | default 0) -}}
{{- $tpl := $sp.template | default dict -}}
{{- if eq $kind "CronJob" -}}{{- $tpl = ((($sp.jobTemplate | default dict).spec | default dict).template | default dict) -}}{{- end -}}
{{- $want := 1 -}}{{- if hasKey $sp "replicas" -}}{{- $want = int64 $sp.replicas -}}{{- end -}}
{{- $strategy := toString (((index $sp (ternary "updateStrategy" "strategy" (ne $kind "Deployment"))) | default dict).type | default "") -}}
{{- if has $kind (list "Deployment" "StatefulSet" "DaemonSet" "CronJob") -}}
{{- $ann := toString (index ((($tpl.metadata | default dict).annotations) | default dict) "krateo.io/messaging-generation" | default "") -}}
{{- if ne $ann $sfx -}}
{{- $blk = append $blk (printf "%s: its pod template is at generation %q, not %q: the release that switched it is not applied yet" $n $ann $sfx) -}}
{{- end -}}
{{- end -}}
{{- if and (has $kind (list "StatefulSet" "DaemonSet")) (eq $strategy "OnDelete") -}}
{{- $blk = append $blk (printf "%s: updateStrategy OnDelete, so its pods keep the previous credential until deleted: delete them (kubectl -n %s delete pod -l <its selector>), one at a time" $n $top.Release.Namespace) -}}
{{- else if and (has $kind (list "Deployment" "StatefulSet" "DaemonSet")) (gt $gen $og) -}}
{{- $blk = append $blk (printf "%s: generation %d not observed yet (observed %d)" $n $gen $og) -}}
{{- else if eq $kind "Deployment" -}}
{{- range $cond := ($st.conditions | default list) -}}
{{- if and (eq (toString $cond.type) "Progressing") (eq (toString $cond.reason) "ProgressDeadlineExceeded") -}}
{{- $blk = append $blk (printf "%s: exceeded its progress deadline" $n) -}}
{{- end -}}
{{- end -}}
{{- $upd := int64 ($st.updatedReplicas | default 0) -}}
{{- if lt $upd $want -}}{{- $blk = append $blk (printf "%s: %d of %d pods updated" $n $upd $want) -}}
{{- else if gt (int64 ($st.replicas | default 0)) $upd -}}{{- $blk = append $blk (printf "%s: %d old pods pending termination" $n (sub (int64 ($st.replicas | default 0)) $upd)) -}}
{{- else if lt (int64 ($st.availableReplicas | default 0)) $upd -}}{{- $blk = append $blk (printf "%s: %d of %d updated pods available" $n (int64 ($st.availableReplicas | default 0)) $upd) -}}
{{- end -}}
{{- else if eq $kind "StatefulSet" -}}
{{- $part := int64 (((($sp.updateStrategy | default dict).rollingUpdate | default dict).partition) | default 0) -}}
{{- if lt (int64 ($st.readyReplicas | default 0)) $want -}}{{- $blk = append $blk (printf "%s: %d of %d pods ready" $n (int64 ($st.readyReplicas | default 0)) $want) -}}
{{- else if and (gt $part 0) (lt (int64 ($st.updatedReplicas | default 0)) $want) -}}{{- $blk = append $blk (printf "%s: partition %d leaves %d pod(s) on the previous credential: set its partition to 0" $n $part (sub $want (int64 ($st.updatedReplicas | default 0)))) -}}
{{- else if ne (toString ($st.updateRevision | default "")) (toString ($st.currentRevision | default "")) -}}{{- $blk = append $blk (printf "%s: update revision %s not current yet" $n ($st.updateRevision | default "")) -}}
{{- end -}}
{{- else if eq $kind "DaemonSet" -}}
{{- $des := int64 ($st.desiredNumberScheduled | default 0) -}}
{{- if lt (int64 ($st.updatedNumberScheduled | default 0)) $des -}}{{- $blk = append $blk (printf "%s: %d of %d pods updated" $n (int64 ($st.updatedNumberScheduled | default 0)) $des) -}}
{{- else if lt (int64 ($st.numberAvailable | default 0)) $des -}}{{- $blk = append $blk (printf "%s: %d of %d updated pods available" $n (int64 ($st.numberAvailable | default 0)) $des) -}}
{{- end -}}
{{- else if eq $kind "CronJob" -}}
{{- if ($st.active | default list) -}}{{- $blk = append $blk (printf "%s: %d Job(s) running, which keep the credential they started with" $n (len $st.active)) -}}{{- end -}}
{{- else if eq $kind "Job" -}}
{{- if gt (int64 ($st.active | default 0)) 0 -}}{{- $blk = append $blk (printf "%s: running, and keeps the credential it started with" $n) -}}{{- end -}}
{{- else if eq $kind "Pod" -}}
{{- if not (has (toString (($st.phase) | default "")) (list "Succeeded" "Failed")) -}}{{- $blk = append $blk (printf "%s: a bare Pod never restarts with the new credential: delete it" $n) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $latest := int64 0 -}}
{{- range $name := include "osh.messagingReaders" (list $c) | fromJsonArray -}}
{{- $e := include "osh.lookupNamed" (list $top "external-secrets.io/v1" "ExternalSecret" $name) | fromJson -}}
{{- if not $e.metadata -}}
{{- $blk = append $blk (printf "ExternalSecret/%s: cannot be read; kept until it can" $name) -}}
{{- else -}}
{{- $reads := false -}}
{{- range $d := (($e.spec | default dict).data | default list) -}}{{- if eq (toString (($d.remoteRef | default dict).key)) $root -}}{{- $reads = true -}}{{- end -}}{{- end -}}
{{- $est := $e.status | default dict -}}
{{- $t := int64 0 -}}{{- if $est.refreshTime -}}{{- $t = int64 (toDate "2006-01-02T15:04:05Z07:00" (toString $est.refreshTime) | unixEpoch) -}}{{- end -}}
{{- if not $reads -}}
{{- $blk = append $blk (printf "ExternalSecret/%s: does not read %s yet" $name $root) -}}
{{- else if not (hasPrefix (printf "%d-" (int64 ($e.metadata.generation | default 0))) (toString ($est.syncedResourceVersion | default ""))) -}}
{{- $blk = append $blk (printf "ExternalSecret/%s: not synced at its generation %d yet" $name (int64 ($e.metadata.generation | default 0))) -}}
{{- else if le $t 0 -}}
{{- $blk = append $blk (printf "ExternalSecret/%s: no refreshTime" $name) -}}
{{- else if gt $t $latest -}}{{- $latest = $t -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if gt $latest 0 -}}
{{- $until := add $latest (include "osh.hookDeadline" (list $top $c) | int64) -}}
{{- if lt (include "osh.nowUnix" $top | int64) $until -}}
{{- $blk = append $blk (printf "hook Jobs started before the last sync of the new credential may still hold the previous one: kept until %s (their activeDeadlineSeconds)" (dateInZone "2006-01-02T15:04:05Z" $until "UTC")) -}}
{{- end -}}
{{- end -}}
{{- dict "rolled" (not $blk) "blockers" $blk | toJson -}}
{{- end -}}

{{- /* osh.generationRoot (list $ $rot): the ROOT of a rotation's target generation (#309 C), in the form of every generated
       root (templates/credentials.yaml): an ExternalSecret writing <target> from the Password generator once
       (refreshPolicy CreatedOnce), kept by helm and torn down with the release. Stamped with the active root at the time
       it was first rendered (krateo.io/previous-root, read back by osh.messagingRotation, so the stamp never moves) and
       the token that named it. Never modified afterwards: a new rotation is a new token, so a new root. */ -}}
{{- define "osh.generationRoot" -}}
{{- $top := index . 0 -}}{{- $rot := index . 1 -}}
{{- $creds := $top.Values.credentials | default dict -}}
{{- $key := "password" -}}
{{- range $i := ($creds.items | default list) -}}{{- if eq $i.id $rot.id -}}{{- $key = $i.key -}}{{- end -}}{{- end }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $rot.target }}
  namespace: {{ $top.Release.Namespace }}
  labels:
    krateo.io/credential: {{ $rot.id }}
    krateo.io/credential-generation: {{ trimPrefix (printf "%s-" $rot.base) $rot.target | quote }}
  annotations:
    # Never deleted by an upgrade that stops rendering it, as every root (templates/credentials.yaml).
    helm.sh/resource-policy: keep
    krateo.io/teardown: with-release
    krateo.io/previous-root: {{ $rot.previous | quote }}
    krateo.io/rotation-token: {{ $rot.token | quote }}
spec:
  refreshPolicy: CreatedOnce
  target:
    name: {{ $rot.target }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        {{ $key }}: {{ "{{ .password }}" | quote }}
  dataFrom:
    - sourceRef:
        generatorRef:
          apiVersion: generators.external-secrets.io/v1alpha1
          kind: Password
          name: {{ $creds.generator }}
{{- end -}}

{{- /* osh.lookupConfigMap (list $ name): the ConfigMap <name> in the release namespace, as JSON ("{}" when absent). One
       named GET, stood in by hack/validate-credential-drift.py. */ -}}
{{- define "osh.lookupConfigMap" -}}
{{- $top := index . 0 -}}
{{- lookup "v1" "ConfigMap" $top.Release.Namespace (index . 1) | default dict | toJson -}}
{{- end -}}

{{- /* osh.credentialDrift (list $): the drift latch (#309 C), as JSON {state: {id: "<sha256>"}, drift: [{id, secret, sha}]}.
       For each GENERATED credential item whose root Secret exists, its data's sha256 (sprig; ESO's own data-hash is
       SHA3-224 of Go's %+v rendering, which a template cannot compute) is compared with the baseline latched in the
       previous render's ConfigMap openstack-credential-generations, read back by lookup:
         no baseline yet: the current sha becomes it;
         equal: nothing to report;
         different: the root was edited or regenerated by hand (ESO v2.14.0 regenerates a generated root the moment its
           data-hash stops matching, externalsecret_controller.go:370). Reported, the baseline kept, unless
           credentialDrift.acknowledge.<Secret> is exactly the current sha: then the current sha becomes the baseline.
       A rotation never needs acknowledging: it mints a NEW root (osh.generationRoot), which is latched on its own. */ -}}
{{- define "osh.credentialDrift" -}}
{{- $top := index . 0 -}}
{{- $prevCM := include "osh.lookupConfigMap" (list $top "openstack-credential-generations") | fromJson -}}
{{- $prev := $prevCM.data | default dict -}}
{{- $ack := (($top.Values.credentialDrift | default dict).acknowledge | default dict) -}}
{{- $state := dict -}}{{- $drift := list -}}
{{- /* The items' roots, and (#309 C2) each messaging generation root a rotation token names and the one its stamp says
       preceded it, latched by Secret name: an edited generation root is reported the same way. */ -}}
{{- $roots := list -}}
{{- range $i := (($top.Values.credentials | default dict).items | default list) -}}
{{- if ne ($i.source | default "") "provided" -}}{{- $roots = append $roots (dict "id" $i.id "secret" $i.secret) -}}{{- end -}}
{{- end -}}
{{- range $c := ($top.Values.components | default list) -}}
{{- $p := include "osh.messagingProjection" (list $top $c) | fromJson -}}
{{- $tok := toString (index ($top.Values.rotate | default dict) (printf "oslo-messaging-%s" $c.name) | default "") -}}
{{- if and $p.secret $tok -}}
{{- $g := printf "%s-g%s" $p.root (trunc 8 (sha256sum $tok)) -}}
{{- $roots = append $roots (dict "id" $g "secret" $g) -}}
{{- $es := include "osh.lookupExternalSecret" (list $top $g) | fromJson -}}
{{- $pr := toString ((($es.metadata | default dict).annotations | default dict) | dig "krateo.io/previous-root" "") -}}
{{- if and $pr (ne $pr $p.root) -}}{{- $roots = append $roots (dict "id" $pr "secret" $pr) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- range $i := $roots -}}
{{- $s := include "osh.lookupSecret" (list $top $i.secret) | fromJson -}}
{{- if $s.data -}}
{{- $sha := toJson $s.data | sha256sum -}}
{{- $base := toString (index $prev $i.id | default "") -}}
{{- if or (not $base) (eq $base $sha) (eq (toString (index $ack $i.secret | default "")) $sha) -}}
{{- $_ := set $state $i.id $sha -}}
{{- else -}}
{{- $_ := set $state $i.id $base -}}
{{- $drift = append $drift (dict "id" $i.id "secret" $i.secret "sha" $sha) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- dict "state" $state "drift" $drift | toJson -}}
{{- end -}}

{{- /* osh.messagingProjectionReady (list $ $c): "true" when component $c may be CREATED as far as its RabbitMQ user is
       concerned: it has no umbrella projection, or that projection's Secret exists AND carries
       rabbitmq.com/topology-operator=true (the label the User webhook requires, #404). Until then $c is held like a
       dependency that is not Ready: listed in openstack-install-pending, never created against a Secret ESO has not
       written, so its User is never refused. Only creation: compositions.yaml keeps an existing component (`or
       $exists`), as for osh.cephRootsReady. */ -}}
{{- define "osh.messagingProjectionReady" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- $p := include "osh.messagingProjection" (list $top $c) | fromJson -}}
{{- $ready := true -}}
{{- if $p.secret -}}
{{- $s := include "osh.lookupSecret" (list $top $p.secret) | fromJson -}}
{{- if ne (toString (((($s.metadata | default dict).labels) | default dict) | dig "rabbitmq.com/topology-operator" "")) "true" -}}{{- $ready = false -}}{{- end -}}
{{- end -}}
{{- $ready -}}
{{- end -}}

{{- /* osh.notificationVhostSources (list $ $sel): .Values.notificationVhosts.vhosts, in order, mapped to what the
       selected closure $sel actually creates, as JSON [{vhost, component, job}]. Each entry names a component's default vhost (components[].rabbitmqVhost). It is kept
       when that component is selected and its rabbit-init runs: the Job is on (its componentValues
       manifests.job_rabbit_init when set, else its chart default, rabbitmqInit, true unless false) and so is the Secret
       the Job reads (manifests.secret_rabbitmq, else rabbitmqSecret: keystone ships it off, and its Job then never
       starts). It is passed under the vhost that rabbit-init declares:
       its componentValues endpoints.oslo_messaging.path without the leading slash when set, else the default. As a JSON
       list. A consumer given a vhost nothing created retries it forever and never reaches the others (#285); measured by
       hack/validate-credentials.py check 26, overrides included. */ -}}
{{- define "osh.notificationVhostSources" -}}
{{- $top := index . 0 -}}{{- $sel := index . 1 -}}
{{- $cvs := $top.Values.componentValues | default dict -}}
{{- $created := dict -}}
{{- range $c := $top.Values.components -}}
{{- if and $c.rabbitmqVhost (has $c.name $sel) -}}
{{- $cv := index $cvs $c.name | default dict -}}
{{- $vhost := $c.rabbitmqVhost -}}
{{- $path := ((($cv.endpoints | default dict).oslo_messaging | default dict).path) | default "" | toString -}}
{{- if $path -}}{{- $vhost = trimPrefix "/" $path | default "/" -}}{{- end -}}
{{- if eq (include "osh.runsRabbitInit" (list $top $c)) "true" -}}{{- $_ := set $created $c.rabbitmqVhost (dict "vhost" $vhost "component" $c.name "job" (printf "%s-rabbit-init" $c.name)) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $out := list -}}
{{- range $v := (($top.Values.notificationVhosts | default dict).vhosts | default list) -}}
{{- if hasKey $created $v -}}{{- $out = append $out (index $created $v) -}}{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- /* osh.notificationVhosts (list $ $sel): the vhost names of osh.notificationVhostSources, as a JSON list. */ -}}
{{- define "osh.notificationVhosts" -}}
{{- $out := list -}}
{{- range $s := include "osh.notificationVhostSources" . | fromJsonArray -}}{{- $out = append $out $s.vhost -}}{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- /* osh.notificationVhostJobs (list $ $sel): the rabbit-init Jobs that create those vhosts, as a JSON list, the
       consumer's own left out (it is already one of its dependencies). <component>-rabbit-init is the name each chart's
       rabbit-init Job renders under (helm-toolkit job_rabbit_init: <serviceUser>-rabbit-init), measured by check 26. The
       consumer waits for them, so it starts only once every vhost it consumes exists (#298). */ -}}
{{- define "osh.notificationVhostJobs" -}}
{{- $top := index . 0 -}}
{{- $consumer := ($top.Values.notificationVhosts | default dict).component -}}
{{- $out := list -}}
{{- range $s := include "osh.notificationVhostSources" . | fromJsonArray -}}
{{- if ne $s.component $consumer -}}{{- $out = append $out $s.job -}}{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- /* osh.mqReplicas ($): the RabbitMQ server replica count, from its ONE source: componentValues.rabbitmq.pod.replicas.server,
       the rabbitmq chart's own value (default 1). Every transport URL lists this many hosts (osh.haValues). #309. */ -}}
{{- define "osh.mqReplicas" -}}
{{- $cv := (index ($.Values.componentValues | default dict) "rabbitmq") | default dict -}}
{{- (((($cv.pod | default dict).replicas | default dict).server) | default 1) | int -}}
{{- end -}}

{{- /* osh.cacheReplicas ($): the memcached replica count. componentValues.memcached.pod.replicas.server when set, else
       haTopology.cacheReplicas with ha.enabled, else 1. More than one replica without ha.enabled is refused: the chart's
       one ClusterIP pins each client to one replica (ClientIP affinity), so the replicas would hold disagreeing caches and
       no client would list them. #309. */ -}}
{{- define "osh.cacheReplicas" -}}
{{- $ha := ($.Values.ha | default dict).enabled -}}
{{- $cv := (index ($.Values.componentValues | default dict) "memcached") | default dict -}}
{{- $set := ((($cv.pod | default dict).replicas | default dict).server) -}}
{{- $n := 1 -}}
{{- if $set -}}{{- $n = int $set -}}{{- else if $ha -}}{{- $n = int ($.Values.haTopology.cacheReplicas | default 3) -}}{{- end -}}
{{- if and (gt $n 1) (not $ha) -}}
{{- fail (printf "componentValues.memcached.pod.replicas.server is %d while ha.enabled is false.\n\nWithout ha, memcached's one ClusterIP Service pins each client to a single replica (sessionAffinity ClientIP) and no component lists the replicas, so %d replicas would hold %d disagreeing caches: keystone's token-validation and revocation caches among them. Set ha.enabled: true (memcached then runs behind a headless Service and every component lists each replica), or leave the replica count at 1. #309." $n $n $n) -}}
{{- end -}}
{{- $n -}}
{{- end -}}

{{- /* osh.haValues (list $ $c): what high availability passes component $c (#309), as a YAML dict merged UNDER its
       componentValues.
         messaging: a component in haTopology.messaging gets endpoints.oslo_messaging.statefulset
           {name: haTopology.messagingStatefulset, replicas: osh.mqReplicas} when the count is above 1, so its transport
           URL lists every RabbitMQ node. At 1 nothing is passed: each chart's own default lists the one host.
         cache: with ha.enabled, memcached gets 3 replicas (osh.cacheReplicas) and manifests.service_discovery (a headless
           Service and the StatefulSet haTopology.cacheStatefulset), and a component in haTopology.cache gets
           endpoints.oslo_cache {hosts.default: haTopology.cacheDiscovery, statefulset: {name, replicas}}, so its memcache
           server list names every replica.
       Refused, because the merge would otherwise let it win silently: a component whose componentValues set
       endpoints.oslo_messaging.statefulset or endpoints.oslo_cache.statefulset to a replica count other than the one
       source. hack/validate-ha-propagation.py renders this span for every component. */ -}}
{{- define "osh.haValues" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- $topo := $top.Values.haTopology | default dict -}}
{{- $ha := ($top.Values.ha | default dict).enabled -}}
{{- $cv := (index ($top.Values.componentValues | default dict) $c.name) | default dict -}}
{{- $ep := $cv.endpoints | default dict -}}
{{- $out := dict -}}
{{- $mq := include "osh.mqReplicas" $top | int -}}
{{- /* With messaging.backend operator the RabbitMQ host list is osh.messagingOperatorValues', not this. */ -}}
{{- if and (has $c.name ($topo.messaging | default list)) (ne (include "osh.messagingBackend" $top) "operator") -}}
{{- with (($ep.oslo_messaging | default dict).statefulset) -}}
{{- if ne (int (.replicas | default 0)) $mq -}}
{{- fail (printf "componentValues.%s.endpoints.oslo_messaging.statefulset.replicas is %v, but RabbitMQ runs %d server replica(s) (componentValues.rabbitmq.pod.replicas.server, the one source).\n\nA transport URL that lists fewer hosts than the cluster has cannot fail over to the nodes it omits. Remove the override: the umbrella passes the count to every component that builds a transport URL. #309." $c.name .replicas $mq) -}}
{{- end -}}
{{- end -}}
{{- if gt $mq 1 -}}
{{- $_ := set $out "endpoints" (dict "oslo_messaging" (dict "statefulset" (dict "name" $topo.messagingStatefulset "replicas" $mq))) -}}
{{- end -}}
{{- end -}}
{{- $cache := include "osh.cacheReplicas" $top | int -}}
{{- if has $c.name ($topo.cache | default list) -}}
{{- with (($ep.oslo_cache | default dict).statefulset) -}}
{{- if or (not $ha) (ne (int (.replicas | default 0)) $cache) -}}
{{- $want := "no list, because ha.enabled is false" -}}
{{- if $ha -}}{{- $want = printf "%d replica(s), with ha.enabled" $cache -}}{{- end -}}
{{- fail (printf "componentValues.%s.endpoints.oslo_cache.statefulset is set (replicas %v), but memcached's replica list is the umbrella's: %s.\n\nThe list names pods through memcached's headless Service, which exists only with ha.enabled, and it must name every replica so that every client hashes a key to the same one. Remove the override. #309." $c.name .replicas $want) -}}
{{- end -}}
{{- end -}}
{{- if $ha -}}
{{- $eps := $out.endpoints | default dict -}}
{{- $_ := set $eps "oslo_cache" (dict "hosts" (dict "default" $topo.cacheDiscovery) "statefulset" (dict "name" $topo.cacheStatefulset "replicas" $cache)) -}}
{{- $_ := set $out "endpoints" $eps -}}
{{- end -}}
{{- end -}}
{{- if and $ha (eq $c.name "memcached") -}}
{{- $_ := set $out "pod" (dict "replicas" (dict "server" $cache)) -}}
{{- $_ := set $out "manifests" (dict "service_discovery" true) -}}
{{- end -}}
{{- if $out -}}{{- toYaml $out -}}{{- end -}}
{{- end -}}

{{- /* osh.requireHaNodes (list $ $nodes): with ha.enabled, refuse to render while fewer than haTopology.minNodes of the
       nodes this release can see carry haTopology.nodeLabel. 3 replicas with required anti-affinity schedule one per
       node: on 2 nodes the third stays Pending forever, and a quorum system on 2 nodes tolerates no node loss.
       Like osh.requireNodeLabels, EMPTY <nodes> is not measured (helm template, or no node-read grant) and is not
       refused; templates/node-preconditions-state.yaml records which. A pure function of its arguments, so
       hack/validate-ha-propagation.py calls it with synthetic node lists. #309. */ -}}
{{- define "osh.requireHaNodes" -}}
{{- $top := index . 0 -}}{{- $nodes := index . 1 | default list -}}
{{- if and (($top.Values.ha | default dict).enabled) (gt (len $nodes) 0) -}}
{{- $topo := $top.Values.haTopology | default dict -}}
{{- $key := $topo.nodeLabel.key -}}{{- $value := $topo.nodeLabel.value | toString -}}
{{- $min := int ($topo.minNodes | default 3) -}}
{{- $carry := 0 -}}
{{- range $n := $nodes -}}
{{- if eq (index (($n.metadata).labels | default dict) $key | toString) $value -}}{{- $carry = add1 $carry -}}{{- end -}}
{{- end -}}
{{- if lt $carry $min -}}
{{- fail (printf "\n\nha.enabled needs %d nodes carrying %s=%s; %d of the %d node(s) this release can see carry it.\n\nWith ha, memcached runs %d replicas with required anti-affinity, one per node, so on fewer nodes the rest stay Pending forever; and a quorum system on 2 nodes survives no node loss. Label more nodes:\n\n    kubectl label --overwrite node <node> %s=%s\n\nor set ha.enabled: false. #309, README \"High availability\".\n" $min $key $value $carry (len $nodes) $min $key $value) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- /* osh.messagingBackend ($): messaging.backend, "chart" unless set. #309 H2b. */ -}}
{{- define "osh.messagingBackend" -}}
{{- ($.Values.messaging | default dict).backend | default "chart" -}}
{{- end -}}

{{- /* osh.messagingOperatorValues (list $ $c): what messaging.backend operator passes component $c (#309 H2b), as a YAML
       dict merged UNDER its componentValues. Nothing in chart mode.
         rabbitmq: operator {enabled, name}, so it renders the RabbitmqCluster haTopology.messagingOperator.name.
         a component in haTopology.messaging (it holds endpoints.oslo_messaging.statefulset): hosts.default
           <name>-nodes and statefulset {name: <name>-server, replicas: osh.mqReplicas}, always, because the chart's
           own default names the chart-mode broker.
         a component in haTopology.messagingByService: hosts.default <name>-nodes.
         a component in haTopology.messagingQuorum: endpoints.oslo_messaging.quorum_queues (its rabbit-init drops ha-*
           policy keys) and conf.<component>.oslo_messaging_rabbit = haTopology.messagingQuorumConf.
       Refused: a component whose componentValues set conf.<component>.oslo_messaging_rabbit.rabbit_ha_queues true,
       which oslo.messaging rejects together with rabbit_quorum_queue, and a statefulset override (the count is the
       one source's). hack/validate-messaging-operator.py renders this span for every component. */ -}}
{{- define "osh.messagingOperatorValues" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- if eq (include "osh.messagingBackend" $top) "operator" -}}
{{- $topo := $top.Values.haTopology | default dict -}}
{{- $name := $topo.messagingOperator.name -}}
{{- $cv := (index ($top.Values.componentValues | default dict) $c.name) | default dict -}}
{{- $mq := include "osh.mqReplicas" $top | int -}}
{{- $out := dict -}}
{{- $om := dict -}}
{{- if eq $c.name "rabbitmq" -}}
{{- $_ := set $out "operator" (dict "enabled" true "name" $name) -}}
{{- end -}}
{{- if has $c.name ($topo.messaging | default list) -}}
{{- with ((($cv.endpoints | default dict).oslo_messaging | default dict).statefulset) -}}
{{- fail (printf "componentValues.%s.endpoints.oslo_messaging.statefulset is set, but with messaging.backend operator the RabbitMQ host list is the umbrella's: %s-server-0..%d behind %s-nodes (componentValues.rabbitmq.pod.replicas.server, the one source). Remove the override. #309." $c.name $name (sub $mq 1) $name) -}}
{{- end -}}
{{- $_ := set $om "hosts" (dict "default" (printf "%s-nodes" $name)) -}}
{{- $_ := set $om "statefulset" (dict "name" (printf "%s-server" $name) "replicas" $mq) -}}
{{- end -}}
{{- if has $c.name ($topo.messagingByService | default list) -}}
{{- $_ := set $om "hosts" (dict "default" (printf "%s-nodes" $name)) -}}
{{- end -}}
{{- if has $c.name ($topo.messagingQuorum | default list) -}}
{{- $own := ((index (($cv.conf | default dict)) $c.name | default dict).oslo_messaging_rabbit | default dict) -}}
{{- if eq (toString $own.rabbit_ha_queues) "true" -}}
{{- fail (printf "componentValues.%s.conf.%s.oslo_messaging_rabbit.rabbit_ha_queues is true, but messaging.backend operator runs quorum queues: oslo.messaging raises RuntimeError when both are enabled, and RabbitMQ 4.x has no queue mirroring. Remove it. #309." $c.name $c.name) -}}
{{- end -}}
{{- $_ := set $om "quorum_queues" true -}}
{{- /* Its rabbit-init becomes messaging-topology-operator resources on this cluster
       (helm-toolkit.manifests.rabbitmq_topology), and its pods wait on them. */ -}}
{{- $_ := set $om "operator" (dict "cluster" $name) -}}
{{- /* Its user projection is the umbrella's (osh.messagingProjection): the chart imports this name and renders none. */ -}}
{{- with (include "osh.messagingProjection" (list $top $c) | fromJson).secret -}}{{- $_ := set $om.operator "userSecret" . -}}{{- end -}}
{{- /* Its service user (#309 C2): its entrypoint waits name ITS User and Permission (of the active generation), and only
       the vhost and the administrator's permission of any other service's rabbit-init it waits on, whose generation it
       cannot know (helm-toolkit.utils.rabbitmq_topology_waits). */ -}}
{{- $_ := set $om.operator "serviceUser" ($c.name | replace "_" "-") -}}
{{- $_ := set $out "conf" (dict $c.name (dict "oslo_messaging_rabbit" (deepCopy $topo.messagingQuorumConf))) -}}
{{- end -}}
{{- if $om -}}{{- $_ := set $out "endpoints" (dict "oslo_messaging" $om) -}}{{- end -}}
{{- if $out -}}{{- toYaml $out -}}{{- end -}}
{{- end -}}
{{- end -}}
