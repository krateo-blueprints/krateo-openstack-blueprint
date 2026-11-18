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

{{- define "neutron.config_args.bgp_dragent" }}
- file: /etc/neutron/neutron.conf
- file: /etc/neutron/bgp_dragent.ini
- dir: /etc/neutron/neutron.conf.d
{{- end }}

{{- define "neutron.config_args.ironic_agent" }}
- file: /etc/neutron/neutron.conf
{{- if and ( empty .Values.conf.neutron.DEFAULT.host ) ( .Values.pod.use_fqdn.neutron_agent ) }}
- file: /tmp/pod-shared/neutron-agent.ini
{{- end }}
- file: /etc/neutron/plugins/ml2/ml2_conf.ini
- dir: /etc/neutron/neutron.conf.d
{{- end }}

{{- define "neutron.config_args.rpc_server" }}
- file: /etc/neutron/neutron.conf
{{- if ( has "ovn" .Values.network.backend ) }}
- file: /tmp/pod-shared/ovn.ini
{{- end }}
{{- if .Values.conf.plugins.taas.taas.enabled }}
- file: /etc/neutron/taas_plugin.ini
{{- end }}
{{- if ( has "sriov" .Values.network.backend ) }}
- file: /etc/neutron/plugins/ml2/sriov_agent.ini
{{- end }}
{{- if .Values.conf.plugins.l2gateway }}
- file: /etc/neutron/l2gw_plugin.ini
{{- end }}
- file: /etc/neutron/plugins/ml2/ml2_conf.ini
- dir: /etc/neutron/neutron.conf.d
{{- end }}
