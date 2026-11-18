{{/*
Krateo blueprint addition (#295): the storage request of glance-images.

  - No live claim (a fresh install): volume.size when set, else volume.defaultSize (10Gi; upstream's 2Gi held one
    duplicate 952 MB manila image and then stalled an upgrade).
  - A live claim, volume.size unset: the live claim's request, exactly. An upgrade never patches a claim nobody asked to
    change, so a StorageClass without allowVolumeExpansion (kind's local-path) cannot make it fail (Diego, 2026-11-17).
  - A live claim, volume.size set: volume.size, never less than the live claim's request (a claim cannot shrink; the
    API server refuses that and the release would be left failed). Raising it needs the claim's StorageClass to set
    allowVolumeExpansion: true, else the API server refuses the patch (README, "Upgrading to 0.3.71 or later").

volume.size is the user's (empty by default) and volume.defaultSize the fresh-install default, so "set by the user" is
the value being non-empty, not a guess from its value. Both are schema-validated quantities. The chart reads only the
claim itself, in its own namespace, which helm reads anyway to upgrade it; it cannot read the StorageClass
(cluster-scoped: a refused lookup would fail every render). Offline (helm template) no claim exists.
*/}}

{{/* A Kubernetes quantity (2Gi, 10G, 512Mi, 1.5Ti, 2147483648) as bytes, a float. Fails on a form it does not know. */}}
{{- define "glance.quantity_bytes" -}}
{{- $q := toString . | trim -}}
{{- $units := dict "Ki" 1024.0 "Mi" 1048576.0 "Gi" 1073741824.0 "Ti" 1099511627776.0 "Pi" 1125899906842624.0 "k" 1000.0 "M" 1000000.0 "G" 1000000000.0 "T" 1000000000000.0 "P" 1000000000000000.0 -}}
{{- if not (regexMatch "^[0-9]+([.][0-9]+)?(Ki|Mi|Gi|Ti|Pi|k|M|G|T|P)?$" $q) -}}
{{- fail (printf "glance: %q is not a storage quantity this chart can compare (e.g. 10Gi)" $q) -}}
{{- end -}}
{{- $num := regexFind "^[0-9]+([.][0-9]+)?" $q -}}
{{- $unit := trimPrefix $num $q -}}
{{- if $unit -}}
{{- mulf (float64 $num) (index $units $unit) -}}
{{- else -}}
{{- float64 $num -}}
{{- end -}}
{{- end -}}

{{/* The live glance-images claim, as JSON ("{}" when there is none): the chart's ONE read of it. The size above uses it
     when the store is the claim (storage pvc), and the refusal to move images off it uses it when the store is Ceph
     (#308, templates/secret-ceph-projections.yaml); the two never render together, so a render reads the claim at most
     once. hack/validate-glance-store.py replaces this lookup with stand-in claims. */}}
{{- define "glance.images_claim" -}}
{{- lookup "v1" "PersistentVolumeClaim" .Release.Namespace "glance-images" | default dict | toJson -}}
{{- end -}}

{{- define "glance.images_volume_size" -}}
{{- $user := .Values.volume.size | default "" | toString | trim -}}
{{- if $user -}}{{- $_ := include "glance.quantity_bytes" $user -}}{{- end -}}
{{- $live := include "glance.images_claim" . | fromJson -}}
{{- $requests := (((($live).spec | default dict).resources | default dict).requests | default dict) -}}
{{- $liveSize := index $requests "storage" | default "" | toString -}}
{{- if not $liveSize -}}
{{- $user | default .Values.volume.defaultSize -}}
{{- else if not $user -}}
{{- $liveSize -}}
{{- else if gt (include "glance.quantity_bytes" $liveSize | float64) (include "glance.quantity_bytes" $user | float64) -}}
{{- $liveSize -}}
{{- else -}}
{{- $user -}}
{{- end -}}
{{- end -}}
