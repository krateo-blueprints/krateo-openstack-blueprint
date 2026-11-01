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
COMMAND="${@:-start}"

function start () {
  # uwsgi, not apache. The images were repointed from quay.io/attcomdev/ubuntu-source-gnocchi-*
  # (Docker Registry v1 manifests, unpullable by any current docker -- see issue #115) to the
  # official OpenStack Kolla set, which is the only maintained gnocchi image family: upstream
  # openstack-helm still references the attcomdev ones and airshipit publishes no gnocchi image
  # at all.
  #
  # Kolla images have no apache: `command -v apache2 httpd` finds nothing and /etc/apache2 does
  # not exist, so the previous `exec apache2 -DFOREGROUND` would exec a binary that is not in
  # the image. They ship uwsgi at /var/lib/kolla/venv/bin/uwsgi with gnocchi's standard WSGI
  # entry module. Verified in the image before this was written: uwsgi binds the port and
  # initialises Python, then fails only on the absent gnocchi.conf that this chart mounts.
  exec uwsgi \
    --http "0.0.0.0:{{ tuple "metric" "internal" "api" . | include "helm-toolkit.endpoints.endpoint_port_lookup" }}" \
    --module gnocchi.rest.wsgi \
    --master \
    --processes {{ .Values.conf.gnocchi.api.workers | default 2 }} \
    --need-app \
    --die-on-term \
    --honour-stdin
}

function stop () {
  kill -TERM 1
}

$COMMAND
