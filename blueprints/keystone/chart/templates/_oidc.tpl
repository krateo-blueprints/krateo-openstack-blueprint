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

  keystone.oidc.caBundleEnabled / keystone.oidc.ownedConfEnabled
    oidc.caBundleRef (kind ConfigMap|Secret, name, key) names the identity provider's CA
    certificate, for an IdP whose TLS is signed by a private CA. The chart mounts that object
    read-only at /etc/keystone/oidc-ca (a whole-volume mount, not subPath, so a rotated
    certificate reaches the pod) and renders `OIDCCABundlePath /etc/keystone/oidc-ca/<key>` in
    its own file, so mod_auth_openidc validates the IdP against it; TLS validation stays on.
    OIDCCABundlePath written into the free-form config is refused: the path and the mount must
    come from one place. ownedConfEnabled is true when the chart-owned file has any directive.

  keystone.oidc.configured
    "true" when OIDC is configured: a clientSecretRef, or any OIDC* directive or
    `AuthType openid-connect` in the free-form Apache config.

  keystone.oidc.requireRemoteIdAttribute
    Fails the render when OIDC is configured but conf.keystone.openid.remote_id_attribute is
    empty. Without it Keystone's validate_idp returns before comparing the token's issuer with
    the identity provider's remote_ids (keystone 2026.1, keystone/federation/utils.py; unchanged since 2025.1), so the
    IdP's remote_ids constrain nothing. Also fails when conf.keystone.federation.remote_id_attribute
    is set: Keystone falls back to it when the protocol group has none, and one setting must
    decide. The group is `openid` because the protocol id is `openid`, fixed by the websso paths
    (/v3/auth/OS-FEDERATION/identity_providers/keycloak/protocols/openid/websso); the
    FederationProtocol CR that registers it (openstack-keystone-operator-kog) has no
    remote_id_attribute field, so the keystone conf is its single source.

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
{{- fail (printf "%s needs both name and key: one without the other configures nothing. See docs/configuration.md, keystone, oidc.clientSecretRef and oidc.cryptoPassphraseRef." .field) -}}
{{- end -}}
{{- end -}}

{{- define "keystone.oidc.clientSecretEnabled" -}}
{{- include "keystone.oidc.refEnabled" (dict "ref" ((.Values.oidc).clientSecretRef) "field" "oidc.clientSecretRef") -}}
{{- end -}}

{{- define "keystone.oidc.cryptoPassphraseEnabled" -}}
{{- include "keystone.oidc.refEnabled" (dict "ref" ((.Values.oidc).cryptoPassphraseRef) "field" "oidc.cryptoPassphraseRef") -}}
{{- end -}}

{{- define "keystone.oidc.caBundleEnabled" -}}
{{- $ref := (.Values.oidc).caBundleRef | default dict -}}
{{- if include "keystone.oidc.refEnabled" (dict "ref" $ref "field" "oidc.caBundleRef") -}}
{{- if not (has ($ref.kind | default "ConfigMap") (list "ConfigMap" "Secret")) -}}
{{- fail (printf "oidc.caBundleRef.kind is %q: it must be ConfigMap or Secret." ($ref.kind | toString)) -}}
{{- end -}}
true
{{- end -}}
{{- end -}}

{{- define "keystone.oidc.ownedConfEnabled" -}}
{{- if or (include "keystone.oidc.secretsEnabled" .) (include "keystone.oidc.caBundleEnabled" .) -}}
true
{{- end -}}
{{- end -}}

{{- define "keystone.oidc.secretsEnabled" -}}
{{- if or (include "keystone.oidc.clientSecretEnabled" .) (include "keystone.oidc.cryptoPassphraseEnabled" .) -}}
true
{{- end -}}
{{- end -}}

{{- define "keystone.oidc.configured" -}}
{{- $envAll := . -}}
{{- $configured := include "keystone.oidc.clientSecretEnabled" $envAll -}}
{{- range $field := list "wsgi_keystone" "security" "mpm_event" -}}
{{- $template := index $envAll.Values.conf $field -}}
{{- if and $template (regexMatch "(?im)^[ \\t]*(OIDC[A-Za-z]+|AuthType[ \\t]+\"?openid-connect\"?)([ \\t]|$)" (tpl (toString $template) $envAll)) -}}
{{- $configured = "true" -}}
{{- end -}}
{{- end -}}
{{- $configured -}}
{{- end -}}

{{- define "keystone.oidc.refuseLiteralSecret" -}}
{{- $envAll := . -}}
{{- range $field := list "wsgi_keystone" "security" "mpm_event" -}}
{{- $template := index $envAll.Values.conf $field -}}
{{- if $template -}}
{{- $rendered := tpl (toString $template) $envAll -}}
{{- range $directive := list "OIDCClientSecret" "OIDCCryptoPassphrase" -}}
{{- if regexMatch (printf "(?im)^[ \\t]*%s([ \\t]|$)" $directive) $rendered -}}
{{- fail (printf "conf.%s carries an %s directive: a secret in values is readable by anyone who can read the Composition CR. Remove it and set oidc.clientSecretRef / oidc.cryptoPassphraseRef (name, key) to a Secret instead; the chart then renders the directive itself. See docs/configuration.md, section \"Upgrading an install that already uses OIDC\"." $field $directive) -}}
{{- end -}}
{{- end -}}
{{- if regexMatch "(?im)^[ \\t]*OIDCCABundlePath([ \\t]|$)" $rendered -}}
{{- fail (printf "conf.%s carries an OIDCCABundlePath directive: set oidc.caBundleRef (kind, name, key) to the ConfigMap or Secret holding the identity provider's CA certificate instead; the chart mounts it and renders the directive itself, so the path and the mount cannot disagree." $field) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- /* The opposite half: a reference set while OIDC is not configured. ownedConfEnabled renders
       oidc-secrets.conf for any reference, and its directives are mod_auth_openidc's; Apache
       refuses an unknown directive at start-up, so keystone-api would crash-loop on an image
       without the module, the umbrella's default included (measured, #237). clientSecretRef alone
       counts as configured, so the passphrase refusal below already covers it. */ -}}
{{- if not (include "keystone.oidc.configured" $envAll) -}}
{{- range $ref := list (list "oidc.cryptoPassphraseRef" "keystone.oidc.cryptoPassphraseEnabled" "OIDCCryptoPassphrase") (list "oidc.caBundleRef" "keystone.oidc.caBundleEnabled" "OIDCCABundlePath") -}}
{{- if include (index $ref 1) $envAll -}}
{{- fail (printf "%s is set but OIDC is not configured (no oidc.clientSecretRef and no OIDC directive in the Apache config): the chart would render %s, a mod_auth_openidc directive, and Apache refuses it unless that module is loaded, so keystone-api would crash-loop. Configure OIDC (the Apache OIDC block, auth.methods and an image that ships mod_auth_openidc), or remove %s." (index $ref 0) (index $ref 2) (index $ref 0)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and (include "keystone.oidc.configured" $envAll) (not (include "keystone.oidc.cryptoPassphraseEnabled" $envAll)) -}}
{{- fail "OIDC is configured (oidc.clientSecretRef, or OIDC directives in the Apache config) but oidc.cryptoPassphraseRef is not set: mod_auth_openidc refuses to start without OIDCCryptoPassphrase, so keystone-api would crash-loop. Set oidc.cryptoPassphraseRef (name, key) to a Secret. See docs/configuration.md, section \"Upgrading an install that already uses OIDC\"." -}}
{{- end -}}
{{- end -}}

{{- define "keystone.oidc.requireRemoteIdAttribute" -}}
{{- $envAll := . -}}
{{- $ks := $envAll.Values.conf.keystone | default dict -}}
{{- if (($ks.federation | default dict).remote_id_attribute) -}}
{{- fail "conf.keystone.federation.remote_id_attribute is set: Keystone reads it only when the protocol's own group has none, so two places would decide which identity provider a token came from. Set conf.keystone.openid.remote_id_attribute instead and remove this one. See docs/configuration.md, section \"Upgrading an install that already uses OIDC\"." -}}
{{- end -}}
{{- if and (include "keystone.oidc.configured" $envAll) (not (($ks.openid | default dict).remote_id_attribute)) -}}
{{- fail "OIDC is configured (oidc.clientSecretRef, or OIDC directives in the Apache config) but conf.keystone.openid.remote_id_attribute is not set: Keystone then skips checking the token's issuer against the identity provider's remote_ids (keystone.federation.utils.validate_idp returns early), so remote_ids bind nothing. Set it to the request variable that carries the issuer, HTTP_OIDC_ISS with OIDCClaimPrefix \"OIDC-\". See docs/configuration.md, section \"Upgrading an install that already uses OIDC\"." -}}
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
{{- if include "keystone.oidc.caBundleEnabled" . }}
# The identity provider's CA certificate, from oidc.caBundleRef, mounted read-only by the chart.
OIDCCABundlePath /etc/keystone/oidc-ca/{{ .Values.oidc.caBundleRef.key }}
{{- end }}
{{ end -}}
