{{/*
mod_auth_openidc's client secret comes from a Kubernetes Secret, never from values.

Values reach the Composition CR, which anyone who can read that CR can read. A client secret
written into the free-form Apache config (conf.wsgi_keystone, conf.security, conf.mpm_event) would
sit there in plaintext, so these helpers make it impossible rather than discouraged:

  keystone.oidc.clientSecretEnabled
    "true" when oidc.clientSecretRef names a Secret and a key, "" when both are empty, and a
    render failure when only one is set: half a reference would silently configure nothing.

  keystone.oidc.refuseLiteralSecret
    Fails the render when any free-form Apache config the chart writes carries an
    OIDCClientSecret directive of its own, whatever its value. The one directive the chart allows
    is the one it renders itself (keystone.oidc.clientSecretConf), so there is exactly one place
    the secret can come from.

  keystone.oidc.clientSecretConf
    The chart-owned Apache file: a single server-level directive that Apache resolves from the
    process environment at startup. keystone-api.sh sources /etc/apache2/envvars, which only
    unsets HOME, then execs apache2, so the env var set from the Secret reaches the parser.
    Server-level mod_auth_openidc settings are inherited by every VirtualHost that does not set
    its own, and keystone.oidc.refuseLiteralSecret guarantees none does.
*/}}

{{- define "keystone.oidc.clientSecretEnabled" -}}
{{- $ref := ((.Values.oidc).clientSecretRef) | default dict -}}
{{- $name := $ref.name | default "" | toString -}}
{{- $key := $ref.key | default "" | toString -}}
{{- if and $name $key -}}
true
{{- else if or $name $key -}}
{{- fail "oidc.clientSecretRef needs both name and key: one without the other configures no OIDC client secret at all" -}}
{{- end -}}
{{- end -}}

{{- define "keystone.oidc.refuseLiteralSecret" -}}
{{- $envAll := . -}}
{{- range $field := list "wsgi_keystone" "security" "mpm_event" -}}
{{- $template := index $envAll.Values.conf $field -}}
{{- if $template -}}
{{- $rendered := tpl (toString $template) $envAll -}}
{{- if regexMatch "(?im)^[ \\t]*OIDCClientSecret([ \\t]|$)" $rendered -}}
{{- fail (printf "conf.%s carries an OIDCClientSecret directive: a client secret in values is readable by anyone who can read the Composition CR. Remove it and set oidc.clientSecretRef (name, key) to a Secret instead; the chart then renders the directive itself." $field) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "keystone.oidc.clientSecretConf" -}}
# Rendered by the keystone chart from oidc.clientSecretRef. The value is the env var that
# keystone-api reads from that Secret; it is never written here.
OIDCClientSecret ${OIDC_CLIENT_SECRET}
{{ end -}}
