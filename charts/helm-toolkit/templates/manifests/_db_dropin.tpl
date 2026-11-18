{{/*
Krateo blueprint addition (issue #228, per-service MariaDB users): the database URLs a chart's main oslo.config file
carries, built from each user's generated root and delivered as ONE drop-in per chart.

A per-service MariaDB user's password sits inside a URL ([database] connection, [api_database] connection, gnocchi's
[indexer] url, ...). The chart lists every such option as an entry {endpoint, user, section, option, tls}: `endpoint` the
oslo_db family (oslo_db, oslo_db_api, oslo_db_cell0, oslo_db_persistence), `user` its endpoints.<endpoint>.auth key,
(section, option) where the URL sits in .Values.conf.<confKey> (or .Values.conf.<entry.conf>), and `tls` whether the
chart appends the MariaDB TLS query string to it in this render (the chart's own condition, evaluated where the entries
are listed: manifests.certificates, tls.oslo_db, ...). For every entry whose user has a
passwordFrom reference:
  - helm-toolkit.utils.db_dropin_refuse refuses a literal at (section, option). Call it BEFORE the chart fills its
    defaults: a URL written into values carries a password, and it would be silently replaced;
  - helm-toolkit.utils.db_dropin_strip deletes (section, option), so the published password renders nowhere. Call it
    after the chart has filled its defaults, right before the file is rendered;
  - helm-toolkit.manifests.db_dropin renders ExternalSecret <service>-db-connection, one file 87-db-connection.conf:
    each section once, each option = the URL built from endpoints exactly as helm-toolkit's
    authenticated_endpoint_uri_lookup builds it, with the password read from the user's root and URL-quoted;
  - workloads read it from the main config's .conf.d (oslo.config reads it even with --config-file), through
    helm-toolkit.snippets.db_dropin_etc_source (pod.etcSources) or the mount/volume snippets. db-init and db-drop read
    the same directory (helm-toolkit.scripts.db_init, db_drop).
With no referenced user all of these render nothing and change nothing.

One MariaDB account has one password. A chart that names the same username under two entries (nova's three
databases, octavia's persistence database) must reference the same root under both, or, without references, set the
same password in values under both: two passwords for one account would have db-init's ALTER USER set them in turn.
helm-toolkit.manifests.db_dropin refuses anything else, in both modes.
*/}}

{{- define "helm-toolkit.utils.db_dropin_file" -}}87-db-connection.conf{{- end -}}

{{/* (tuple $envAll $endpoint $user): "true" when endpoints.<endpoint>.auth.<user>.passwordFrom names a root. */}}
{{- define "helm-toolkit.utils.db_from_root" -}}
{{- $envAll := index . 0 -}}{{- $endpoint := index . 1 -}}{{- $user := index . 2 -}}
{{- $auth := (index $envAll.Values.endpoints $endpoint | default dict).auth | default dict -}}
{{- if ((index $auth $user | default dict).passwordFrom | default dict).secret }}true{{- end -}}
{{- end -}}

{{/* (tuple $envAll $entries): the entries whose user has a passwordFrom reference, as a JSON list. */}}
{{- define "helm-toolkit.utils.db_dropin_active" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}
{{- $out := list -}}
{{- range $e := $entries }}
{{- if include "helm-toolkit.utils.db_from_root" (tuple $envAll ($e.endpoint | default "oslo_db") $e.user) }}{{- $out = append $out $e -}}{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{/* (tuple $url $username $password $placeholder $what): $url with exactly its `://<username>:<urlquoted password>@`
     replaced by `://<username>:<placeholder>@`, as helm-toolkit.endpoints.authenticated_endpoint_uri_lookup renders the
     userinfo. Fails when the URL does not hold exactly one. */}}
{{- define "helm-toolkit.utils.db_url_with_password" -}}
{{- $url := index . 0 -}}{{- $username := index . 1 -}}{{- $password := index . 2 -}}{{- $placeholder := index . 3 -}}{{- $what := index . 4 -}}
{{- $literal := printf "://%s:%s@" $username ($password | toString | urlquery) -}}
{{- if ne (len (splitList $literal $url)) 2 }}
{{- fail (printf "cannot place the password in the %s URL: expected exactly one %q" $what "://<user>:<password>@") }}
{{- end }}
{{- replace $literal (printf "://%s:%s@" $username $placeholder) $url -}}
{{- end -}}

{{/* (tuple $envAll $endpoint $user $tls): the URL user <user> of <endpoint> connects with, as the charts build it:
     authenticated_endpoint_uri_lookup, plus the TLS query string when $tls (the entry's `tls`: the chart's own
     condition for appending it, evaluated by the chart -- manifests.certificates, tls.oslo_db, ...). */}}
{{- define "helm-toolkit.utils.db_connection_url" -}}
{{- $envAll := index . 0 -}}{{- $endpoint := index . 1 -}}{{- $user := index . 2 -}}{{- $tls := index . 3 -}}
{{- $url := tuple $endpoint "internal" $user "mysql" $envAll | include "helm-toolkit.endpoints.authenticated_endpoint_uri_lookup" -}}
{{- if $tls -}}
{{- $url = printf "%s?charset=utf8&ssl_ca=/etc/mysql/certs/ca.crt&ssl_key=/etc/mysql/certs/tls.key&ssl_cert=/etc/mysql/certs/tls.crt&ssl_verify_cert" $url -}}
{{- end -}}
{{- $url -}}
{{- end -}}

{{/* (dict "envAll" $ "entries" $entries "confKey" "aodh"): with a user's root, a literal at one of its options is
     refused. Call it before the chart fills its defaults. Renders nothing. */}}
{{- define "helm-toolkit.utils.db_dropin_refuse" -}}
{{- $envAll := .envAll -}}{{- $confKey := .confKey -}}
{{- range $e := (include "helm-toolkit.utils.db_dropin_active" (tuple .envAll .entries) | fromJsonArray) }}
{{- $k := $e.conf | default $confKey -}}
{{- $opts := index (index $envAll.Values.conf $k | default dict) $e.section | default dict -}}
{{- if not (empty (index $opts $e.option)) }}
{{- fail (printf "conf.%s.%s.%s is set while endpoints.%s.auth.%s.passwordFrom is: the URL carries that MariaDB user's password, so it is built from the generated root (drop-in %s). Remove it, or set the database through endpoints.%s (#228)." $k $e.section $e.option ($e.endpoint | default "oslo_db") $e.user (include "helm-toolkit.utils.db_dropin_file" $envAll) ($e.endpoint | default "oslo_db")) }}
{{- end }}
{{- end }}
{{- end -}}

{{/* (dict "envAll" $ "entries" $entries "confKey" "aodh"): delete every referenced (section, option) from
     .Values.conf.<confKey> (or .Values.conf.<entry.conf>). Call it after the chart has filled its defaults. */}}
{{- define "helm-toolkit.utils.db_dropin_strip" -}}
{{- $envAll := .envAll -}}{{- $confKey := .confKey -}}
{{- range $e := (include "helm-toolkit.utils.db_dropin_active" (tuple .envAll .entries) | fromJsonArray) }}
{{- $opts := index (index $envAll.Values.conf ($e.conf | default $confKey) | default dict) $e.section -}}
{{- if kindIs "map" $opts }}{{- $_ := unset $opts $e.option -}}{{- end -}}
{{- end -}}
{{- end -}}

{{/* (dict "envAll" $ "entries" $entries "service" "aodh"): the ExternalSecret <service>-db-connection. */}}
{{- define "helm-toolkit.manifests.db_dropin" -}}
{{- $envAll := .envAll -}}
{{- /* one account, one root: every entry naming the same username has the same reference, or none has one */ -}}
{{- $byName := dict -}}
{{- range $e := .entries }}
{{- $ep := $e.endpoint | default "oslo_db" -}}
{{- $u := index (index $envAll.Values.endpoints $ep).auth $e.user -}}
{{- $from := $u.passwordFrom | default dict -}}
{{- $ref := printf "%s/%s/%s" ($from.store | default "") ($from.secret | default "") ($from.key | default "") -}}
{{- $seen := index $byName $u.username -}}
{{- if and $seen (ne $seen.ref $ref) }}
{{- fail (printf "MariaDB user %s is endpoints.%s.auth.%s and endpoints.%s.auth.%s, with different password references (%q, %q): one account has one password, so both must name the same root (db-init's ALTER USER would set each in turn) (#228)" $u.username $seen.where.endpoint $seen.where.user $ep $e.user $seen.ref $ref) }}
{{- end }}
{{- /* values mode too: with no reference, the password in values is the account's, and two different ones would have
       db-init's ALTER USER set each in turn, the last winning and the other readers refused. Compared by hash, so the
       message names the families and never a password. */ -}}
{{- if and $seen (not $from.secret) (ne $seen.pw ($u.password | toString | sha256sum)) }}
{{- fail (printf "MariaDB user %s is endpoints.%s.auth.%s and endpoints.%s.auth.%s, with different passwords: one account has one password, so both must set the same (db-init's ALTER USER would set each in turn, and the readers of the other would be refused) (#228)" $u.username $seen.where.endpoint $seen.where.user $ep $e.user) }}
{{- end }}
{{- if not $seen }}
{{- $_ := set $byName $u.username (dict "ref" $ref "pw" ($u.password | toString | sha256sum) "where" (dict "endpoint" $ep "user" $e.user)) -}}
{{- end }}
{{- end }}
{{- $active := include "helm-toolkit.utils.db_dropin_active" (tuple $envAll .entries) | fromJsonArray -}}
{{- if $active }}
{{- $name := printf "%s-db-connection" .service -}}
{{- $sections := dict -}}
{{- $keys := dict -}}
{{- $store := "" -}}
{{- range $e := $active }}
{{- $ep := $e.endpoint | default "oslo_db" -}}
{{- $u := index (index $envAll.Values.endpoints $ep).auth $e.user -}}
{{- $from := $u.passwordFrom -}}
{{- range $field := list "store" "secret" "key" }}
{{- if not (index $from $field) }}
{{- fail (printf "endpoints.%s.auth.%s.passwordFrom needs %s (store, secret and key)" $ep $e.user $field) }}
{{- end }}
{{- end }}
{{- if and $store (ne $store $from.store) }}
{{- fail (printf "%s: every passwordFrom must name the same SecretStore (one ExternalSecret reads them all)" $name) }}
{{- end }}
{{- $store = $from.store -}}
{{- $key := printf "pw_%s_%s" $ep $e.user -}}
{{- $url := include "helm-toolkit.utils.db_connection_url" (tuple $envAll $ep $e.user ($e.tls | default false)) -}}
{{- $url = include "helm-toolkit.utils.db_url_with_password" (tuple $url $u.username $u.password (printf "{{ .%s | urlquery }}" $key) (printf "%s %s" $ep $e.user)) -}}
{{- $lines := index $sections $e.section | default list -}}
{{- $_ := set $sections $e.section (append $lines (printf "%s = %s" $e.option $url)) -}}
{{- $_ := set $keys $key $from -}}
{{- end }}
{{- $text := "" -}}
{{- range $section := keys $sections | sortAlpha }}
{{- $text = printf "%s[%s]\n%s\n" $text $section (index $sections $section | join "\n") -}}
{{- end }}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $name }}
  annotations:
    # Deleted by helm WITH the release, after the pre-delete hooks (#254).
    krateo.io/teardown: with-release
spec:
  # OnChange (#309 C): refreshed only when this ExternalSecret itself changes (a new root, a new
  # template), never on a timer, so a root edited by hand (which ESO regenerates) reaches no live
  # credential until a rotation; the umbrella reports the edit (status.credentialDrift).
  refreshPolicy: OnChange
  secretStoreRef:
    kind: SecretStore
    name: {{ $store | quote }}
  target:
    name: {{ $name }}
    creationPolicy: Owner
    template:
      type: Opaque
      engineVersion: v2
      data:
        {{ include "helm-toolkit.utils.db_dropin_file" $envAll }}: {{ $text | quote }}
  data:
{{- range $key := keys $keys | sortAlpha }}
{{- $from := index $keys $key }}
    - secretKey: {{ $key }}
      remoteRef:
        key: {{ $from.secret | quote }}
        property: {{ $from.key | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/* (tuple $envAll $entries $service): a pod.etcSources entry projecting the drop-in, as JSON, or "". */}}
{{- define "helm-toolkit.snippets.db_dropin_etc_source" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}{{- $service := index . 2 -}}
{{- if (include "helm-toolkit.utils.db_dropin_active" (tuple $envAll $entries) | fromJsonArray) -}}
{{- dict "secret" (dict "name" (printf "%s-db-connection" $service)) | toJson -}}
{{- end -}}
{{- end -}}

{{/* (tuple $envAll $entries $service [$dir]): a volumeMount of the drop-in file into <dir> (default
     /etc/<service>/<service>.conf.d), for workloads without pod.etcSources. Indent at the call site. */}}
{{- define "helm-toolkit.snippets.db_dropin_mount" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}{{- $service := index . 2 -}}
{{- $dir := printf "/etc/%s/%s.conf.d" $service $service -}}{{- if gt (len .) 3 }}{{- $dir = index . 3 -}}{{- end -}}
{{- if (include "helm-toolkit.utils.db_dropin_active" (tuple $envAll $entries) | fromJsonArray) }}
- name: {{ printf "%s-db-connection" $service }}
  mountPath: {{ printf "%s/%s" $dir (include "helm-toolkit.utils.db_dropin_file" $envAll) }}
  subPath: {{ include "helm-toolkit.utils.db_dropin_file" $envAll }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "helm-toolkit.snippets.db_dropin_volume" -}}
{{- $envAll := index . 0 -}}{{- $entries := index . 1 -}}{{- $service := index . 2 -}}
{{- if (include "helm-toolkit.utils.db_dropin_active" (tuple $envAll $entries) | fromJsonArray) }}
- name: {{ printf "%s-db-connection" $service }}
  secret:
    secretName: {{ printf "%s-db-connection" $service }}
    defaultMode: 0444
{{- end }}
{{- end -}}
