{{/*
Krateo blueprint addition (issue #228, 0.3.67): the generic driver logs in to its service instances (the share-server
VMs) with a per-install SSH key instead of the published [generic] service_instance_password (manila).

Not a generated password: the stock manila-service-image has user manila with password manila built in, so a
password of our own would lock manila out of its share servers. With a key, manila creates the Nova keypair
manila_service_keypair_name from path_to_public_key, boots each service VM with it (cloud-init puts it in
service_instance_user's authorized_keys) and connects with path_to_private_key; the image's password login is no
longer used.

With endpoints.sharev2.auth.service_instance_ssh_keyFrom (store, secret, key, publicKey: set by the umbrella from an
SSHKey item):
  - configmap-etc.yaml removes service_instance_password from [generic] and sets the two paths and the keypair name;
  - secret-service-instance-ssh-key.yaml renders the ExternalSecret manila-service-instance-ssh-key, two files;
  - manila-share, the only program that loads the share driver, mounts them at manila.ssh_key_dir: the private key
    0400, and the pod gets fsGroup, because the kubelet hands a Secret file to root unless fsGroup is set (then it is
    group-owned and group-readable), and manila-share runs as an unprivileged user (pod.security_context).
Without the reference all of these render nothing and change nothing.
*/}}

{{/* The reference as JSON ({} when unset). */}}
{{- define "manila.ssh_key_ref" -}}
{{- (((.Values.endpoints.sharev2 | default dict).auth | default dict).service_instance_ssh_keyFrom | default dict) | toJson -}}
{{- end -}}

{{- define "manila.ssh_key_from" -}}
{{- with (include "manila.ssh_key_ref" . | fromJson) }}{{ if .secret }}true{{ end }}{{ end -}}
{{- end -}}

{{- define "manila.ssh_key_dir" -}}/etc/manila/service-instance-ssh{{- end -}}
{{- define "manila.ssh_key_private_file" -}}id_rsa{{- end -}}
{{- define "manila.ssh_key_public_file" -}}id_rsa.pub{{- end -}}

{{/* (dict "envAll" $): the share pod's extra securityContext line, indented for a pod securityContext at 6. */}}
{{- define "manila.ssh_key_fs_group" -}}
{{- if and (include "manila.ssh_key_from" .envAll) (not (hasKey (.envAll.Values.pod.security_context.manila.pod | default dict) "fsGroup")) }}
        fsGroup: {{ .envAll.Values.pod.security_context.manila.pod.runAsUser | default 42424 }}
{{- end }}
{{- end -}}

{{- define "manila.ssh_key_volume_mount" -}}
{{- if include "manila.ssh_key_from" . }}
- name: manila-service-instance-ssh-key
  mountPath: {{ include "manila.ssh_key_dir" . }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "manila.ssh_key_volume" -}}
{{- if include "manila.ssh_key_from" . }}
- name: manila-service-instance-ssh-key
  secret:
    secretName: manila-service-instance-ssh-key
    defaultMode: 0400
    items:
      - key: {{ include "manila.ssh_key_private_file" . }}
        path: {{ include "manila.ssh_key_private_file" . }}
        mode: 0400
      - key: {{ include "manila.ssh_key_public_file" . }}
        path: {{ include "manila.ssh_key_public_file" . }}
        mode: 0444
{{- end }}
{{- end -}}
