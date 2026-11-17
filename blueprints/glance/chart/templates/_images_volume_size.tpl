{{/*
Krateo blueprint addition (#295): the storage request of glance-images.

volume.size, unless the live claim already requests more: a PersistentVolumeClaim cannot shrink (the API server refuses
it, and the release would be left failed), so a live claim larger than volume.size keeps its size. A live claim smaller
than volume.size is raised to it, which the API server accepts only when the claim's StorageClass sets
allowVolumeExpansion: true (README, "glance's image store"). This chart cannot read the StorageClass: it is
cluster-scoped, and a lookup the render identity is refused is a template error that would fail every render. It reads
only the claim itself, in its own namespace, which helm reads anyway to upgrade it.

Offline (helm template) no claim exists and volume.size is rendered.
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

{{- define "glance.images_volume_size" -}}
{{- $want := .Values.volume.size -}}
{{- $live := lookup "v1" "PersistentVolumeClaim" .Release.Namespace "glance-images" -}}
{{- $requests := (((($live).spec | default dict).resources | default dict).requests | default dict) -}}
{{- $liveSize := index $requests "storage" | default "" -}}
{{- if and $liveSize (gt (include "glance.quantity_bytes" $liveSize | float64) (include "glance.quantity_bytes" $want | float64)) -}}
{{- $liveSize -}}
{{- else -}}
{{- $want -}}
{{- end -}}
{{- end -}}
