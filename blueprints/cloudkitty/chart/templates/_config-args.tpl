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

{{- define "cloudkitty.config_args.processor" }}
- file: /etc/cloudkitty/cloudkitty.conf
- dir: /etc/cloudkitty/cloudkitty.conf.d
{{- end }}
