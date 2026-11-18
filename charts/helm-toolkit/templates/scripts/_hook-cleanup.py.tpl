{{/*
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/}}

{{/*
Krateo blueprint addition (#206). Run at uninstall by helm-toolkit.manifests.job_hook_cleanup: deletes this release's
completed install/upgrade hook Jobs, which Helm never deletes. Standard library only, so it runs in any image of the
chart that has python. It lists the Jobs matching SELECTOR in NAMESPACE and deletes each one whose helm.sh/hook names
post-install or post-upgrade, with propagationPolicy Background so their pods go too. It always exits 0: a failed
cleanup leaves the Jobs behind, as before, and must never block the uninstall that runs it (Helm aborts an uninstall
whose pre-delete hook fails). API_URL and SA_DIR exist so hack/validate-hook-cleanup.py can run it against a stand-in.
*/}}

{{- define "helm-toolkit.scripts.hook_cleanup" }}
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


def main():
    sa = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
    api = os.environ.get("API_URL")
    if not api:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        if ":" in host:
            host = "[%s]" % host
        api = "https://%s:%s" % (host, os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
    ctx = ssl.create_default_context(cafile=os.path.join(sa, "ca.crt")) if api.startswith("https") else None
    with open(os.path.join(sa, "token")) as f:
        token = f.read().strip()
    namespace, selector = os.environ["NAMESPACE"], os.environ["SELECTOR"]
    base = "/apis/batch/v1/namespaces/%s/jobs" % urllib.parse.quote(namespace)

    def call(method, path, body=None):
        req = urllib.request.Request(api + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            return json.load(r)

    jobs = call("GET", base + "?labelSelector=" + urllib.parse.quote(selector)).get("items") or []
    deleted, failed = [], []
    for job in jobs:
        meta = job.get("metadata") or {}
        hooks = {h.strip() for h in str((meta.get("annotations") or {}).get("helm.sh/hook", "")).split(",")}
        if not hooks & {"post-install", "post-upgrade"}:
            continue
        try:
            call("DELETE", base + "/" + urllib.parse.quote(meta["name"]),
                 {"kind": "DeleteOptions", "apiVersion": "v1", "propagationPolicy": "Background"})
            deleted.append(meta["name"])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            failed.append("%s (HTTP %d)" % (meta["name"], e.code))
    print("hook-cleanup: %d hook Job(s) of %s deleted: %s" % (len(deleted), selector, ", ".join(deleted) or "none"))
    if failed:
        print("hook-cleanup: not deleted, left behind: %s" % ", ".join(failed))


try:
    main()
except Exception as e:  # noqa: BLE001 - see the header: a cleanup failure must not block the uninstall
    print("hook-cleanup: skipped, the hook Jobs are left behind: %s: %s" % (type(e).__name__, e))
sys.exit(0)
{{- end }}
