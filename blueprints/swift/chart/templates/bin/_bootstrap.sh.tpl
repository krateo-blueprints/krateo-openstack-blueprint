{{/*
Licensed under the Apache License, Version 2.0 (the "License");
*/}}

{{- define "swift.bin.bootstrap" }}
#!/bin/bash
set -ex

echo "Swift bootstrap started"

# Credentials: the Job's env carries every OS_* variable from the swift-keystone-admin Secret
# (helm-toolkit keystone_openrc_env_vars). They are not exported here: this script ships in a
# ConfigMap, so a literal password here would be readable by anyone who can read ConfigMaps, and
# would be the published default even when the Secret's password is generated (#228).

# Wait for Swift proxy to be ready
SWIFT_ENDPOINT={{ tuple "object_store" "internal" "api" . | include "helm-toolkit.endpoints.keystone_endpoint_uri_lookup" }}
echo "Waiting for Swift endpoint at ${SWIFT_ENDPOINT}..."
count=0
while ! curl -s -o /dev/null -w '%{http_code}' "${SWIFT_ENDPOINT}/healthcheck" | grep -q "200"; do
    if [ $count -ge 60 ]; then
        echo "Timeout waiting for Swift endpoint"
        exit 1
    fi
    echo "Waiting for Swift endpoint..."
    sleep 5
    count=$((count+1))
done

echo "Swift endpoint is healthy"

# Run any custom bootstrap script
{{ .Values.bootstrap.script | default "" }}

echo "Swift bootstrap complete"
{{- end }}
