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

{{- define "helm-toolkit.scripts.keystone_domain_user" }}
#!/bin/bash

# Copyright 2017 Pete Birley
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -ex

# Manage domain
SERVICE_OS_DOMAIN_ID=$(openstack domain create --or-show --enable -f value -c id \
    --description="Service Domain for ${SERVICE_OS_DOMAIN_NAME}" \
    "${SERVICE_OS_DOMAIN_NAME}")

# Display domain
openstack domain show "${SERVICE_OS_DOMAIN_ID}"

# Manage user
SERVICE_OS_USERID=$(openstack user create --or-show --enable -f value -c id \
    --domain="${SERVICE_OS_DOMAIN_ID}" \
    --description "Service User for ${SERVICE_OS_REGION_NAME}/${SERVICE_OS_DOMAIN_NAME}" \
    --password="${SERVICE_OS_PASSWORD}" \
    "${SERVICE_OS_USERNAME}")

{{ if not (and (kindIs "map" .) (index . "krateoRotate")) -}}
# Manage user password (we do this to ensure the password is updated if required)
openstack user set --password="${SERVICE_OS_PASSWORD}" "${SERVICE_OS_USERID}"
{{ end }}
# Display user
openstack user show "${SERVICE_OS_USERID}"

# Manage role
SERVICE_OS_ROLE_ID=$(openstack role show -f value -c id \
    "${SERVICE_OS_ROLE}" || openstack role create -f value -c id \
    "${SERVICE_OS_ROLE}" )

# Manage user role assignment
openstack role add \
          --domain="${SERVICE_OS_DOMAIN_ID}" \
          --user="${SERVICE_OS_USERID}" \
          --user-domain="${SERVICE_OS_DOMAIN_ID}" \
          "${SERVICE_OS_ROLE_ID}"

# Display user role assignment
openstack role assignment list \
          --role="${SERVICE_OS_ROLE_ID}" \
          --user-domain="${SERVICE_OS_DOMAIN_ID}" \
          --user="${SERVICE_OS_USERID}"
{{- if and (kindIs "map" .) (index . "krateoRotate") }}

# Krateo (#228): only in the pre-upgrade Job's inline copy (the chart passes krateoRotate).
# A machine user must never be locked out: while its password rotates, a reader still holding the previous value
# fails authentication, and five failures lock the user for 30 minutes. So the exemption comes BEFORE the password.
openstack user set --ignore-lockout-failure-attempts "${SERVICE_OS_USERID}"

# The password changes LAST, after the domain, user and role assignment exist: every reader still holding the
# previous value fails from here until it restarts, after this Job completes (measured, 0.3.65).
openstack user set --password="${SERVICE_OS_PASSWORD}" "${SERVICE_OS_USERID}"
{{- end }}
{{- end }}
