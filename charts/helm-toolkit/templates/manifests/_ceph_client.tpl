{{/*
Krateo blueprint addition (#308): a chart consumes Ceph through copies of the keys Ceph mints, never through
client.admin.

With storage.backend ceph-rook or ceph-external the umbrella passes a chart two references (store, secret, key), each to
a root it copies from Ceph's side (a Rook CephClient, or the Ceph admin):
  krateo.ceph.keyringFrom   the key of the Ceph user this chart authenticates as (client.<user>);
  krateo.ceph.monHostFrom   the monitor addresses, as ceph.conf's mon_host.
With both, the chart is CEPH-MANAGED (helm-toolkit.utils.ceph_managed): it renders the two projections below and mounts
them where upstream mounted the keyring Secret and the ceph-etc ConfigMap; it renders no client.admin keyring, runs no
`ceph auth` and creates no pool -- the users, their caps and the pools are made on Ceph's side. Without them it renders
exactly as upstream does.
*/}}

{{- define "helm-toolkit.utils.ceph_managed" -}}
{{- $ceph := ((.Values.krateo | default dict).ceph | default dict) -}}
{{- $k := $ceph.keyringFrom | default dict -}}{{- $m := $ceph.monHostFrom | default dict -}}
{{- if and $k.secret $m.secret -}}true
{{- else if or $k.secret $m.secret -}}
{{- fail "\n\nkrateo.ceph.keyringFrom and krateo.ceph.monHostFrom come together (#308): the umbrella passes both with a ceph storage.backend, and one without the other has a key and no cluster, or a cluster and no key.\n" -}}
{{- end -}}
{{- end -}}

{{/* (tuple $envAll (list "conf.ceph.admin_keyring" ...)): in a ceph-managed render, refuse each value path that would
     carry a literal key or reach Ceph another way. The schema cannot refuse them: they sit under open trees (conf). */}}
{{- define "helm-toolkit.utils.ceph_refuse_literals" -}}
{{- $envAll := index . 0 -}}
{{- if eq (include "helm-toolkit.utils.ceph_managed" $envAll) "true" -}}
{{- range $path := index . 1 -}}
  {{- $v := $envAll.Values -}}
  {{- range $p := splitList "." $path -}}{{- if kindIs "map" $v -}}{{- $v = index $v $p -}}{{- else -}}{{- $v = "" -}}{{- end -}}{{- end -}}
  {{- if $v -}}
    {{- fail (printf "\n\n%s is set, and this chart consumes Ceph through krateo.ceph references (#308).\n\nWith a ceph storage.backend the key comes from Ceph's side and reaches the chart as a projection; a literal key in values, a client.admin keyring or a second Ceph wired by hand is refused, so no published or hand-copied key reaches a rendered object.\n" $path) -}}
  {{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* (dict "envAll" $ "keyring" <Secret name> "etc" <Secret name>): the two projections, through the umbrella's store.
     <keyring> holds `key`, the user's key, as upstream's keyring Secrets do; <etc> holds `ceph.conf` with mon_host. */}}
{{- define "helm-toolkit.manifests.ceph_projections" -}}
{{- $envAll := .envAll -}}
{{- if eq (include "helm-toolkit.utils.ceph_managed" $envAll) "true" -}}
{{- $k := $envAll.Values.krateo.ceph.keyringFrom -}}{{- $m := $envAll.Values.krateo.ceph.monHostFrom -}}
{{- range $ref := list $k $m -}}
{{- range $field := list "store" "secret" "key" -}}
{{- if not (index $ref $field) -}}{{- fail (printf "krateo.ceph references need %s (store, secret and key)" $field) -}}{{- end -}}
{{- end -}}
{{- end }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ .keyring }}
  annotations:
    # Deleted by helm WITH the release, after the pre-delete hooks (#254).
    krateo.io/teardown: with-release
spec:
  refreshInterval: 5m
  secretStoreRef:
    kind: SecretStore
    name: {{ $k.store | quote }}
  target:
    name: {{ .keyring }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        key: {{ "{{ .key }}" | quote }}
  data:
    - secretKey: key
      remoteRef:
        key: {{ $k.secret | quote }}
        property: {{ $k.key | quote }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ .etc }}
  annotations:
    krateo.io/teardown: with-release
spec:
  refreshInterval: 5m
  secretStoreRef:
    kind: SecretStore
    name: {{ $m.store | quote }}
  target:
    name: {{ .etc }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        ceph.conf: {{ "[global]\nmon_host = {{ .mon_host }}\n" | quote }}
  data:
    - secretKey: mon_host
      remoteRef:
        key: {{ $m.secret | quote }}
        property: {{ $m.key | quote }}
{{- end -}}
{{- end -}}

{{/* (tuple $envAll <keyring Secret name>): a pod-template annotation carrying a checksum of the projected key as it is
     live in the cluster, so a reader restarts when the key changes. Ceph's side can rotate a key (Rook's cephx rotation,
     or the Ceph admin), the copy and the projection follow within their refresh, but a reader holds the key it read at
     start -- the keyring file its init container wrote, and libvirt's secret, defined once at libvirtd's start. The next
     reconcile after the change renders a new checksum, and the workload rolls. One named GET of the projection.
     Ceph-managed only: otherwise it renders nothing, and the pod template is unchanged. Offline (helm template) no
     Secret exists, and the checksum is of nothing. hack/validate-ceph-consumer.py (check 18c) renders it with a
     stand-in live Secret. */}}
{{- define "helm-toolkit.snippets.ceph_key_checksum" -}}
{{- $envAll := index . 0 -}}{{- $name := index . 1 -}}
{{- if eq (include "helm-toolkit.utils.ceph_managed" $envAll) "true" -}}
{{- $live := lookup "v1" "Secret" $envAll.Release.Namespace $name -}}
krateo.io/ceph-key-checksum: {{ (($live | default dict).data | default dict).key | default "" | sha256sum | quote }}
{{- end -}}
{{- end -}}
