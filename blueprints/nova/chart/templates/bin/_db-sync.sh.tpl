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

{{- /* Krateo (#228): every URL written into nova_api.cell_mappings is a template nova formats from each process's
       own [database] connection and [DEFAULT] transport_url (templates/_cell_mapping_templates.tpl), so the mappings
       hold no password and no process can cache a stale one. Upstream wrote the literal URLs from DB_CONNECTION,
       DB_CONNECTION_CELL0 and TRANSPORT_URL; an existing mapping is rewritten to the template on every run. */}}
{{- $tpl := include "nova.cell_mapping_templates" . | fromJson }}
function manage_cells () {
  nova-manage $([ -d /etc/nova/nova.conf.d ] && echo --config-dir /etc/nova/nova.conf.d) cell_v2 map_cell0 --database_connection '{{ $tpl.cell0 }}'
  nova-manage $([ -d /etc/nova/nova.conf.d ] && echo --config-dir /etc/nova/nova.conf.d) cell_v2 list_cells | grep -q " cell1 " || \
    nova-manage $([ -d /etc/nova/nova.conf.d ] && echo --config-dir /etc/nova/nova.conf.d) cell_v2 create_cell --name=cell1 --verbose \
      --database_connection '{{ $tpl.cell1 }}' \
      --transport-url '{{ $tpl.transport }}'

  CELL0_ID=$(nova-manage $([ -d /etc/nova/nova.conf.d ] && echo --config-dir /etc/nova/nova.conf.d) cell_v2 list_cells | awk -F '|' '/ cell0 / { print $3 }' | tr -d ' ')
  CELL1_ID=$(nova-manage $([ -d /etc/nova/nova.conf.d ] && echo --config-dir /etc/nova/nova.conf.d) cell_v2 list_cells | awk -F '|' '/ cell1 / { print $3 }' | tr -d ' ')

  nova-manage $([ -d /etc/nova/nova.conf.d ] && echo --config-dir /etc/nova/nova.conf.d) cell_v2 update_cell \
    --cell_uuid="${CELL0_ID}" \
    --name="cell0" \
    --transport-url 'none:///' \
    --database_connection '{{ $tpl.cell0 }}'

  nova-manage $([ -d /etc/nova/nova.conf.d ] && echo --config-dir /etc/nova/nova.conf.d) cell_v2 update_cell \
    --cell_uuid="${CELL1_ID}" \
    --name="cell1" \
    --transport-url '{{ $tpl.transport }}' \
    --database_connection '{{ $tpl.cell1 }}'
}

nova-manage $([ -d /etc/nova/nova.conf.d ] && echo --config-dir /etc/nova/nova.conf.d) api_db sync
manage_cells

nova-manage $([ -d /etc/nova/nova.conf.d ] && echo --config-dir /etc/nova/nova.conf.d) db sync

nova-manage $([ -d /etc/nova/nova.conf.d ] && echo --config-dir /etc/nova/nova.conf.d) db online_data_migrations

echo 'Finished DB migrations'
