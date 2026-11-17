{{/*
abstract: |
  Krateo blueprint addition (#303). The pod spec that writes what Krateo KOG operators read: one mint program and
  one writer, shared by keystone (the token, keystone_url, region_name, and nova_url once nova is in the catalog) and
  nova (nova_url, as soon as nova registered its endpoint). One copy, so the two charts resolve nova_url the same way.

  The mint step is the only container that holds the admin password (the chart's admin openrc, ksUserSecret). Each
  attempt logs in once; it tries at most 4 times (keystone locks a user after 5 failures). From what keystone
  answers it takes the token, the project id and the internal endpoints of identity and compute; nova_url is compute's
  catalog URL with the token's project id as its last path segment (not appended twice), and a URL that kept an
  unresolved template is refused. Mode keystone accepts a catalog without compute (keystone installs before nova);
  mode nova requires it and writes no token.

  The writer holds what the mint step wrote, never the password, and sends JSON merge patches of only its own keys:
  keystone patches openstack-kog-token and keystone_url, region_name (and nova_url when it resolved one) of
  openstack-kog-endpoints; nova patches nova_url of openstack-kog-endpoints and nothing else. A merge patch keeps the
  keys it does not name, so neither writer clobbers the other's. hack/validate-kog-credentials.py runs both programs.
values: |
  The chart's labels.job, pod.tolerations.<application>, images.tags.ks_user and dependencies.static.<depsKey>.
usage: |
  {{ dict "envAll" $envAll "application" "nova" "mode" "nova" "depsKey" "kog_endpoints" "serviceAccount" "nova-kog-endpoints" "ksUserSecret" .Values.secrets.identity.admin "useCA" false | include "helm-toolkit.snippets.kog_endpoints_pod" }}
*/}}

{{- define "helm-toolkit.snippets.kog_endpoints_pod" -}}
{{- $envAll := .envAll -}}
{{- if not (has .mode (list "keystone" "nova")) -}}
{{- fail (printf "helm-toolkit.snippets.kog_endpoints_pod: mode %q is neither keystone nor nova" .mode) -}}
{{- end -}}
serviceAccountName: {{ .serviceAccount }}
restartPolicy: Never
{{- if (index $envAll.Values.pod.tolerations .application).enabled }}
{{ tuple $envAll .application | include "helm-toolkit.snippets.kubernetes_tolerations" }}
{{- end }}
nodeSelector:
  {{ $envAll.Values.labels.job.node_selector_key }}: {{ $envAll.Values.labels.job.node_selector_value }}
initContainers:
{{ tuple $envAll .depsKey list | include "helm-toolkit.snippets.kubernetes_entrypoint_init_container" }}
# The mint step: the only container that holds the admin password (the admin openrc, from the generated root). Each
# attempt logs in once; it tries at most 4 times and writes only what keystone answered, to a memory emptyDir.
- name: mint
{{ tuple $envAll "ks_user" | include "helm-toolkit.snippets.image" | indent 2 }}
  env:
    - name: KOG_MODE
      value: {{ .mode | quote }}
{{- with $env := dict "ksUserSecret" .ksUserSecret "useCA" .useCA }}
{{- include "helm-toolkit.snippets.keystone_openrc_env_vars" $env | indent 4 }}
{{- end }}
  command:
    - /bin/bash
    - -c
    - |
      set -u
      umask 077
      cat > /run/kog/mint.py <<'PY'
      import json
      import os
      import sys

      import openstack
      from keystoneauth1 import exceptions as ksa_exceptions

      mode = os.environ["KOG_MODE"]
      # One login (the admin openrc's OS_* variables); everything else comes from what keystone answered.
      sess = openstack.connect(cloud="envvars").session
      region = os.environ["OS_REGION_NAME"]
      token = sess.get_token()
      project = sess.get_project_id()
      keystone_url = sess.get_endpoint(service_type="identity", interface="internal", region_name=region)
      try:
          compute = sess.get_endpoint(service_type="compute", interface="internal", region_name=region)
      except ksa_exceptions.EndpointNotFound:
          # keystone installs before nova: at keystone's own install the catalog has no compute entry yet.
          if mode == "nova":
              raise
          compute = None
      if not token or not project or not keystone_url:
          sys.exit("keystone answered without a token, a project id or its own internal endpoint")
      nova_url = ""
      if compute:
          # nova's catalog entry, as keystone resolved it for this token, with the token's project id as its last
          # path segment (nova's /v2.1/<project_id> form, which nova-kog's nova.url takes).
          nova_url = compute.rstrip("/")
          if project not in nova_url.split("/"):
              nova_url = nova_url + "/" + project
          if "%(" in nova_url:
              sys.exit("keystone's compute endpoint holds a template its catalog did not resolve: " + compute)
      else:
          print("compute is not in keystone's catalog yet: nova's own hook writes nova_url when nova installs")
      if mode == "keystone":
          with open("/run/kog/token", "w") as f:
              f.write(token)
      with open("/run/kog/endpoints.json", "w") as f:
          json.dump({"nova_url": nova_url, "keystone_url": keystone_url, "region_name": region}, f)
      PY
      for attempt in 1 2 3 4; do
        if python3 /run/kog/mint.py; then
          echo "keystone answered on attempt ${attempt}"
          exit 0
        fi
        echo "attempt ${attempt} of 4 failed" >&2
        sleep 15
      done
      echo "nothing written after 4 attempts: stopping before keystone's lockout (5 failures)" >&2
      exit 1
  volumeMounts:
    - name: kog-run
      mountPath: /run/kog
containers:
# The writer: holds what the mint step wrote, never the password, and merge-patches only its own keys.
- name: write
{{ tuple $envAll "ks_user" | include "helm-toolkit.snippets.image" | indent 2 }}
  env:
    - name: KOG_MODE
      value: {{ .mode | quote }}
    - name: KOG_NAMESPACE
      valueFrom:
        fieldRef:
          fieldPath: metadata.namespace
  command:
    - python3
    - -c
    - |
      import base64, json, os, ssl, urllib.request
      sa = "/var/run/secrets/kubernetes.io/serviceaccount"
      api = "https://%s:%s/api/v1/namespaces/%s/" % (
          os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"], os.environ["KOG_NAMESPACE"])
      ctx = ssl.create_default_context(cafile=sa + "/ca.crt")
      bearer = "Bearer " + open(sa + "/token").read().strip()

      def patch(path, data):
          # A JSON merge patch: keys it does not name stay as they are.
          req = urllib.request.Request(api + path, data=json.dumps({"data": data}).encode(), method="PATCH",
                                       headers={"Authorization": bearer, "Content-Type": "application/merge-patch+json"})
          urllib.request.urlopen(req, context=ctx).read()
          print("%s: patched %s" % (path, ", ".join(sorted(data))))

      endpoints = json.load(open("/run/kog/endpoints.json"))
      if os.environ["KOG_MODE"] == "nova":
          patch("configmaps/openstack-kog-endpoints", {"nova_url": endpoints["nova_url"]})
      else:
          token = open("/run/kog/token").read().strip()
          patch("secrets/openstack-kog-token", {"token": base64.b64encode(token.encode()).decode()})
          mine = {"keystone_url": endpoints["keystone_url"], "region_name": endpoints["region_name"]}
          if endpoints["nova_url"]:
              mine["nova_url"] = endpoints["nova_url"]
          patch("configmaps/openstack-kog-endpoints", mine)
  volumeMounts:
    - name: kog-run
      mountPath: /run/kog
      readOnly: true
volumes:
- name: kog-run
  emptyDir:
    medium: Memory
{{- end -}}
