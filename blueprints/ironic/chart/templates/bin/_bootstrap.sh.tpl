#!/bin/bash

{{/*
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/}}

set -ex
{{ include "helm-toolkit.scripts.openstack_count" . }}

{{ $source_base := .Values.bootstrap.image.source_base | default "" }}
{{ range $name, $opts := .Values.bootstrap.image.structured }}
{{ $source := empty $source_base | ternary $opts.source (printf "%s/%s" $source_base $opts.source) }}
{{- /* Krateo (#295): the image is created only when glance answered and holds none of that name. upstream's
     `image show <name> || create` read any failure as "missing": run while glance-api was rolling, it uploaded a
     second copy (GKE 2026-11-17), and glance accepts two images with one name. */}}
n=$(openstack_count {{ $name | quote }} image list --name {{ $name | quote }})
if [ "${n}" -eq 0 ]; then
  IMAGE_LOC=$(mktemp)
  curl --fail -sSL {{ $source }} -o ${IMAGE_LOC}
  openstack image create {{ $name | quote }} \
  --disk-format {{ $opts.disk_format }} \
  --container-format {{ $opts.container_format }} \
  --file ${IMAGE_LOC} \
  {{ if $opts.properties -}} {{ range $k, $v := $opts.properties }}--property {{$k}}={{$v}} {{ end }}{{ end -}} \
  --{{ $opts.visibility | default "public" }}
  rm -f ${IMAGE_LOC}
elif [ "${n}" -gt 1 ]; then
  echo "glance holds ${n} images named {{ $name }}: uploading nothing; delete the extra ones (#295)" >&2
fi
{{ else }}
{{ .Values.bootstrap.image.script | default "echo 'Not Enabled'" }}
{{ end }}
