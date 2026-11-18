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

{{/*
Each probed program's oslo.config arguments, in the order oslo.config reads them (#228's drop-ins
last): ONE definition, rendered by helm-toolkit.snippets.oslo_config_args into both the program's
start script (its exec line) and its worker readiness probe, so the probe reads exactly the
configuration the program was started with (#231). Edit the list here, never in either consumer.
*/}}

{{- define "cinder.config_args.backup" }}
- file: /etc/cinder/cinder.conf
- dir: /etc/cinder/cinder.conf.d
{{- end }}

{{- define "cinder.config_args.scheduler" }}
- file: /etc/cinder/cinder.conf
- dir: /etc/cinder/cinder.conf.d
{{- end }}

{{- define "cinder.config_args.volume" }}
- file: /etc/cinder/cinder.conf
- file: /etc/cinder/conf/backends.conf
- file: /tmp/pod-shared/internal_tenant.conf
{{- if empty .Values.conf.cinder.DEFAULT.host }}
- file: /tmp/cinder-volume-host.conf
{{- end }}
- dir: /etc/cinder/cinder.conf.d
{{- end }}
