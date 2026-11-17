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
export HOME=/tmp
{{ include "helm-toolkit.scripts.openstack_count" . }}

cd /tmp/images

{{ range .Values.bootstrap.structured.images }}
{{- /* Krateo (#295): the image is created only when glance answered and holds none of that name. upstream's
     `image show <name> || create` read any failure as "missing": run while glance-api was rolling, it uploaded a
     second copy (GKE 2026-11-17), and glance accepts two images with one name. */}}
n=$(openstack_count {{ .name | quote }} image list --name {{ .name | quote }})
if [ "${n}" -eq 0 ]; then
  curl --fail -sSL -O {{ .source_url }}{{ .image_file }}
  openstack image create {{ .name | quote }} \
  {{ if .id -}} --id {{ .id }} {{ end -}} \
  --min-disk {{ .min_disk }} \
  --disk-format {{ .image_type }} \
  --file {{ .image_file }} \
  {{ if .properties -}} {{ range $key, $value := .properties }}--property {{$key}}={{$value}} {{ end }}{{ end -}} \
  --container-format {{ .container_format | quote }} \
  {{ if .private -}}
  --private
  {{- else -}}
  --public
  {{- end }}
elif [ "${n}" -gt 1 ]; then
  echo "glance holds ${n} images named {{ .name }}: uploading nothing; delete the extra ones (#295)" >&2
fi
{{ end }}

{{ .Values.bootstrap.script | default "echo 'Not Enabled'" }}
