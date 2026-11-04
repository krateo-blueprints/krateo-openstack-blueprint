#!/usr/bin/env python3
{{/*
Mint the PowerDNS API key into a Secret, exactly once per install.

WHY A JOB AND NOT A TEMPLATE. The key must be three things at once: random, stable across
upgrades, and absent from every rendered manifest. A template cannot be all three. Generating
with randAlphaNum re-mints on every render, so designate authenticates with the previous key
and zone updates start failing with 401 at an interval nobody connects to a chart upgrade --
and it breaks the two-byte-identical-renders gate, which is how this was caught here rather
than on a cluster. Guarding that with `lookup` does not help: `helm template` has no cluster,
so the fallback runs anyway. Putting a fixed value in values.yaml is deterministic and is what
this repo did before, which is how `api_token: chiave_segreta` came to sit in a public
repository (#165).

So the key is never in a template, never in values, and never in a ConfigMap. The charts only
reference the Secret by name; this job is the only thing that knows the value.

RE-ENTRANT BY CONSTRUCTION. The repo's charts are hook-free by gate, so this job runs on every
release including upgrades. It creates the Secret only when it is absent, and treats "someone
else created it first" as success rather than as an error -- two reconciles racing is normal
here, and the loser must not fail the release.
*/}}
import os
import secrets
import sys

from kubernetes import client, config

NAMESPACE = os.environ["POD_NAMESPACE"]
SECRET_NAME = os.environ["SECRET_NAME"]
KEY = "PDNS_API_KEY"

config.load_incluster_config()
v1 = client.CoreV1Api()

try:
    existing = v1.read_namespaced_secret(SECRET_NAME, NAMESPACE)
    if existing.data and existing.data.get(KEY):
        print(f"{SECRET_NAME} already holds {KEY}; leaving it alone.")
        sys.exit(0)
    # Present but empty is a real state -- a partially-created Secret -- and overwriting the
    # whole object would discard anything else in it, so patch just this key.
    print(f"{SECRET_NAME} exists without {KEY}; patching it in.")
    v1.patch_namespaced_secret(
        SECRET_NAME, NAMESPACE,
        client.V1Secret(string_data={KEY: secrets.token_urlsafe(32)}),
    )
    sys.exit(0)
except client.ApiException as exc:
    if exc.status != 404:
        raise

print(f"creating {SECRET_NAME} with a fresh {KEY}.")
body = client.V1Secret(
    metadata=client.V1ObjectMeta(name=SECRET_NAME, namespace=NAMESPACE),
    string_data={KEY: secrets.token_urlsafe(32)},
    type="Opaque",
)
try:
    v1.create_namespaced_secret(NAMESPACE, body)
except client.ApiException as exc:
    if exc.status == 409:
        # Another reconcile won. That is the correct outcome, not a failure: the key exists and
        # it is the one everything else will read.
        print(f"{SECRET_NAME} was created concurrently; keeping that one.")
        sys.exit(0)
    raise
print(f"{SECRET_NAME} created.")
