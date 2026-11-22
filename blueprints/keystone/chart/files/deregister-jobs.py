# Krateo (#444): delete the leftover hook Jobs of components that left the selection.
# DEREGISTER_COMPONENTS: the not-selected components (umbrella osh.deregistration). A Job is deleted only when its
# `application` label names one of them AND it carries a helm.sh/hook annotation: a hook Job left behind after its
# release was uninstalled (helm uninstall keeps hook resources). Deletion propagates in the background, so its pods go
# too. Holds no password: only this pod's ServiceAccount token, whose Role allows list and delete on Jobs.
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

components = json.loads(os.environ.get("DEREGISTER_COMPONENTS") or "[]")
if not components:
    print("deregister: no component left the selection")
    sys.exit(0)
sa = "/var/run/secrets/kubernetes.io/serviceaccount"
api = "https://%s:%s/apis/batch/v1/namespaces/%s/jobs" % (
    os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"], os.environ["DEREGISTER_NAMESPACE"])
ctx = ssl.create_default_context(cafile=sa + "/ca.crt")
bearer = "Bearer " + open(sa + "/token").read().strip()


def call(url, method="GET", body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": bearer, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, context=ctx).read() or b"{}")


selector = "application in (%s)" % ",".join(sorted(components))
jobs = call(api + "?" + urllib.parse.urlencode({"labelSelector": selector})).get("items") or []
deleted = 0
for j in jobs:
    md = j.get("metadata") or {}
    app = (md.get("labels") or {}).get("application")
    if app not in components or "helm.sh/hook" not in (md.get("annotations") or {}):
        print("deregister: keep Job %s (application %s): not a leftover hook Job of a not-selected component"
              % (md.get("name"), app))
        continue
    call("%s/%s" % (api, urllib.parse.quote(md["name"])), "DELETE", {"propagationPolicy": "Background"})
    print("deregister: %s: deleted leftover hook Job %s" % (app, md["name"]))
    deleted += 1
print("deregister: %d leftover hook Job(s) deleted, of %d labelled with a not-selected component" % (deleted, len(jobs)))
