{{/*
Krateo blueprint addition (issue #295): a bash function for bootstrap scripts that create a resource only when it is
absent, for resources whose API accepts a duplicate name (glance images).

  openstack_count <name> <list command...>
    prints how many entries `openstack <list command> -f value -c Name` names exactly <name>. When the command fails
    (the API is unreachable, or refuses), it exits the script with status 1, so the Job fails and is retried: a failure
    is never read as "absent".

Upstream's `openstack image show <name> || { download; openstack image create <name> ... }` read any failure as "missing".
On a 0.3.68 -> 0.3.70 upgrade (GKE 2026-11-17) manila's bootstrap ran while glance-api was rolling, the show failed to
reach glance, and a second 952 MB manila-service-image filled the 2 GiB store. glance accepts two images with one name,
and a later `image show <name>` fails with "More than one Image exists", which the same pattern read as "missing" again.

Usage (a script under set -e):
  {{ include "helm-toolkit.scripts.openstack_count" . }}
  n=$(openstack_count "NAME" image list --name "NAME")
  if [ "${n}" -eq 0 ]; then ...create...; elif [ "${n}" -gt 1 ]; then echo "... uploading nothing" >&2; fi
*/}}

{{- define "helm-toolkit.scripts.openstack_count" }}
# openstack_count <name> <list command...> (Krateo, #295): how many entries `openstack <list command>` names exactly
# <name>. A failure to list exits the script, so the Job retries: it is never read as "absent".
openstack_count () {
  local name="$1"
  shift
  local names
  if ! names=$(openstack "$@" -f value -c Name); then
    echo "openstack $*: no answer, so nothing is created; the Job retries (#295)" >&2
    exit 1
  fi
  printf '%s\n' "${names}" | grep -cxF -- "${name}" || true
}
{{- end }}
