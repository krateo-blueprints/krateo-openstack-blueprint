{{/*
Krateo blueprint addition (#281): the notification agent's [notification] messaging_urls, one per
conf.notification_vhosts entry, as rabbit://<admin user>:<password>@<host>:<port>/<vhost>. The agent consumes other
services' notifications, which only the RabbitMQ administrator can read, so the URLs carry the admin user (upstream's
own choice). (tuple $envAll $password): $password is the literal to embed, URL-quoted by the caller's choice: the
published-or-overridden value when the admin has no passwordFrom, or the ESO template text of the drop-in.
*/}}
{{- define "ceilometer.notification_messaging_urls" -}}
{{- $envAll := index . 0 -}}{{- $password := index . 1 -}}
{{- $admin := $envAll.Values.endpoints.oslo_messaging.auth.admin -}}
{{- $host := tuple "oslo_messaging" "internal" $envAll | include "helm-toolkit.endpoints.hostname_fqdn_endpoint_lookup" -}}
{{- $port := tuple "oslo_messaging" "internal" "amqp" $envAll | include "helm-toolkit.endpoints.endpoint_port_lookup" -}}
{{- /* Krateo (#309): with more than one RabbitMQ replica (endpoints.oslo_messaging.statefulset, passed by the umbrella),
       each URL lists every node, as the transport URLs do, so the agent fails over to the nodes it would otherwise omit.
       At one replica the URL names the Service, as before. */ -}}
{{- $ss := $envAll.Values.endpoints.oslo_messaging.statefulset | default dict -}}
{{- $n := int ($ss.replicas | default 1) -}}
{{- $out := list -}}
{{- range $vhost := $envAll.Values.conf.notification_vhosts }}
{{- if gt $n 1 -}}
{{- $hosts := list -}}
{{- range $i := until $n -}}
{{- $hosts = append $hosts (printf "%s:%s@%s-%d.%s:%s" ($admin.username | urlquery) $password $ss.name $i $host $port) -}}
{{- end -}}
{{- $out = append $out (printf "rabbit://%s/%s" (join "," $hosts) $vhost) -}}
{{- else -}}
{{- $out = append $out (printf "rabbit://%s:%s@%s:%s/%s" ($admin.username | urlquery) $password $host $port $vhost) -}}
{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{/* true when the RabbitMQ administrator's password comes from a generated root: the URLs then live only in the
     drop-in. */}}
{{- define "ceilometer.notification_urls_from_root" -}}
{{- if ((.Values.endpoints.oslo_messaging.auth.admin.passwordFrom | default dict).secret) }}true{{- end -}}
{{- end -}}

{{- define "ceilometer.notification_urls_file" -}}86-notification-urls.conf{{- end -}}
