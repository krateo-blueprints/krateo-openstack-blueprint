{{- /* Krateo blueprint addition (#445): the configmap-bin-hash of ONE workload, over only the mariadb-bin keys it mounts.

       Upstream hashes the whole rendered configmap-bin.yaml into every workload that mounts mariadb-bin. That
       ConfigMap also carries scripts the server never runs: mariadb_controller.py (the controller Deployment's),
       test.sh (the test pod's), mariadb-wait-for-cluster.py (the cluster-wait Job's) and the backup scripts. So a change
       to any of them rolled the server StatefulSet, and with one replica that is a database outage: #335's controller
       heartbeat restarted MariaDB on the 0.3.71 -> 0.3.74 upgrade (measured on GKE, r074: Keystone's token API failed
       for about 2 minutes).

       usage: tuple (list "start.py" "health.sh") $ | include "mariadb.bin_hash"
       The value is the sha256 of the JSON of exactly those keys of the RENDERED mariadb-bin (the ConfigMap itself, read
       back from its own template, so the hash cannot drift from what is mounted), quoted like helm-toolkit.utils.hash.
       A key listed but not rendered contributes nothing (manifests.configmap_bin off renders none: the hash of {} then,
       as upstream's was of an empty file). Each workload's list is its subPath mounts of mariadb-bin;
       hack/validate-mariadb-bin-hash.py changes every key's source one at a time and fails unless a workload's pod
       template changes exactly when a key it mounts changes. */ -}}
{{- define "mariadb.bin_hash" -}}
{{- $keys := index . 0 -}}
{{- $ctx := index . 1 -}}
{{- $tpl := $ctx.Template.Name | replace (base $ctx.Template.Name) "configmap-bin.yaml" -}}
{{- $cm := include $tpl $ctx | fromYaml -}}
{{- $data := $cm.data | default dict -}}
{{- $picked := dict -}}
{{- range $k := $keys -}}
{{- if hasKey $data $k -}}{{- $_ := set $picked $k (index $data $k) -}}{{- end -}}
{{- end -}}
{{- $picked | toJson | sha256sum | quote -}}
{{- end -}}

{{- /* The mariadb-bin keys the server StatefulSet's containers mount (statefulset.yaml, subPath). stop.sh is mounted
       too but rendered by no template here (upstream as vendored); it is listed so a future stop.sh is hashed. */ -}}
{{- define "mariadb.server_bin_keys" -}}
{{- list "start.py" "stop.sh" "health.sh" | toJson -}}
{{- end -}}
