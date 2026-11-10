{{/*
mod_auth_openidc's two secrets come from Kubernetes Secrets, never from values.

Values reach the Composition CR, which anyone who can read that CR can read. A secret written into
the free-form Apache config (conf.wsgi_keystone, conf.security, conf.mpm_event) would sit there in
plaintext, so these helpers make that impossible rather than discouraged. The two secrets:

  OIDCClientSecret      oidc.clientSecretRef      env OIDC_CLIENT_SECRET
    The client's credential at the identity provider's token endpoint.
  OIDCCryptoPassphrase  oidc.cryptoPassphraseRef  env OIDC_CRYPTO_PASSPHRASE
    The key mod_auth_openidc encrypts its state and session cookies with. mod_auth_openidc 2.4.11
    refuses to start without it whenever OIDC is configured ("mandatory parameter
    'OIDCCryptoPassphrase' is not set"), and `apache2 -t` does not catch that, so a missing
    passphrase would crash-loop keystone-api instead of failing the render.

  keystone.oidc.refEnabled (dict "ref" <ref> "field" <values path>)
    "true" when the reference names a Secret and a key, "" when both are empty, and a render
    failure when only one is set: half a reference would silently configure nothing.

  keystone.oidc.refuseLiteralSecret
    Fails the render when
      - any free-form Apache config carries its own OIDCClientSecret or OIDCCryptoPassphrase,
        whatever the value: the only allowed directives are the ones the chart renders itself;
      - OIDC is configured (a clientSecretRef, or any OIDC* directive or `AuthType openid-connect`
        in the free-form config) but oidc.cryptoPassphraseRef is not set.

  keystone.oidc.secretsConf
    The chart-owned Apache file: one server-level directive per configured reference, each
    resolved by Apache from the process environment at startup. keystone-api.sh sources
    /etc/apache2/envvars, which only unsets HOME, then execs apache2, so env vars set from the
    Secrets reach the parser. Server-level mod_auth_openidc settings are inherited by every
    VirtualHost that does not set its own, and keystone.oidc.refuseLiteralSecret guarantees none
    does.
*/}}

{{- define "keystone.oidc.refEnabled" -}}
{{- $ref := .ref | default dict -}}
{{- $name := $ref.name | default "" | toString -}}
{{- $key := $ref.key | default "" | toString -}}
{{- if and $name $key -}}
true
{{- else if or $name $key -}}
{{- fail (printf "%s needs both name and key: one without the other configures nothing" .field) -}}
{{- end -}}
{{- end -}}

{{- define "keystone.oidc.clientSecretEnabled" -}}
{{- include "keystone.oidc.refEnabled" (dict "ref" ((.Values.oidc).clientSecretRef) "field" "oidc.clientSecretRef") -}}
{{- end -}}

{{- define "keystone.oidc.cryptoPassphraseEnabled" -}}
{{- include "keystone.oidc.refEnabled" (dict "ref" ((.Values.oidc).cryptoPassphraseRef) "field" "oidc.cryptoPassphraseRef") -}}
{{- end -}}

{{- define "keystone.oidc.secretsEnabled" -}}
{{- if or (include "keystone.oidc.clientSecretEnabled" .) (include "keystone.oidc.cryptoPassphraseEnabled" .) -}}
true
{{- end -}}
{{- end -}}

{{- define "keystone.oidc.refuseLiteralSecret" -}}
{{- $envAll := . -}}
{{- $oidcConfigured := include "keystone.oidc.clientSecretEnabled" $envAll -}}
{{- range $field := list "wsgi_keystone" "security" "mpm_event" -}}
{{- $template := index $envAll.Values.conf $field -}}
{{- if $template -}}
{{- $rendered := tpl (toString $template) $envAll -}}
{{- range $directive := list "OIDCClientSecret" "OIDCCryptoPassphrase" -}}
{{- if regexMatch (printf "(?im)^[ \\t]*%s([ \\t]|$)" $directive) $rendered -}}
{{- fail (printf "conf.%s carries an %s directive: a secret in values is readable by anyone who can read the Composition CR. Remove it and set oidc.clientSecretRef / oidc.cryptoPassphraseRef (name, key) to a Secret instead; the chart then renders the directive itself." $field $directive) -}}
{{- end -}}
{{- end -}}
{{- if regexMatch "(?im)^[ \\t]*(OIDC[A-Za-z]+|AuthType[ \\t]+\"?openid-connect\"?)([ \\t]|$)" $rendered -}}
{{- $oidcConfigured = "true" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and $oidcConfigured (not (include "keystone.oidc.cryptoPassphraseEnabled" $envAll)) -}}
{{- fail "OIDC is configured (oidc.clientSecretRef, or OIDC directives in the Apache config) but oidc.cryptoPassphraseRef is not set: mod_auth_openidc refuses to start without OIDCCryptoPassphrase, so keystone-api would crash-loop. Set oidc.cryptoPassphraseRef (name, key) to a Secret." -}}
{{- end -}}
{{- end -}}

{{- define "keystone.oidc.secretsConf" -}}
# Rendered by the keystone chart from oidc.clientSecretRef and oidc.cryptoPassphraseRef. Each value
# is an env var that keystone-api reads from a Secret; no secret is ever written here.
{{- if include "keystone.oidc.clientSecretEnabled" . }}
OIDCClientSecret ${OIDC_CLIENT_SECRET}
{{- end }}
{{- if include "keystone.oidc.cryptoPassphraseEnabled" . }}
OIDCCryptoPassphrase ${OIDC_CRYPTO_PASSPHRASE}
{{- end }}
{{ end -}}
