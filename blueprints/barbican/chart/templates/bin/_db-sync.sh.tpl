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

# Krateo (#228): no --config-dir here. barbican-db-manage parses its own arguments with argparse (only --dburl), not
# oslo.config's, and 0.3.66 crash-looped this Job with "invalid choice: '/etc/barbican/barbican.conf.d'" (measured on
# GKE 2026-11-16). It reads its configuration through barbican.common.config, which parses with project='barbican', so
# barbican.conf.d is found there; and it needs only the database URL. hack/oslo-config-dir-programs.yaml records it.
barbican-db-manage upgrade

{{- $kek := (index (index .Values.conf.barbican "simple_crypto_plugin" | default dict) "kek") | default "" }}
{{- $old_kek := index .Values.conf.simple_crypto_kek_rewrap "old_kek" | default ""}}
{{- if and (not (empty $old_kek)) (not (empty $kek)) }}
set +x
echo "Ensuring that project KEKs are wrapped with the target global KEK"
/tmp/simple_crypto_kek_rewrap.py --old-keks="$(cat /tmp/old_keks)"
{{- end }}
