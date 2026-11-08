#!/usr/bin/env bash
# Prove hack/migrate-to-pvc.py moves real state, for one component, on a throwaway kind cluster.
#
#   1. Install that component's RELEASED 0.3.35 chart, which keeps its state on a hostPath.
#   2. Write a marker into it: a row for mariadb, a vhost for rabbitmq.
#   3. Run prepare, then upgrade to this tree's chart, then run finish.
#   4. Read the marker back from the pod that now runs on the claim.
#   5. Run prepare again, which must report there is nothing left to migrate.
#   6. On a second fresh install, empty the node path the way a kind node restart did (#177),
#      and require prepare to REFUSE: it must not create a claim from an empty directory.
#
# The Composition is a stand-in: a CRD with the real group and plural and no controller,
# because what this tests is the script against Kubernetes. The controller's side was measured
# separately on GKE with cdc 2.13.72 and is recorded on #177: krateo.io/paused holds a deleted
# StatefulSet and a manual scale to 0, and the new StatefulSet adopts a claim made first.
#
# finish decides from the component's CompositionDefinition (spec.chart.version), also a stand-in
# here. The Composition's krateo.io/composition-version label is deliberately held at the WRONG
# value in both finish steps: on an umbrella install it alternates between the old and the new
# version about once a minute (#179), so a finish that read it would be right only by timing.
#
# rabbitmq cannot run on Apple Silicon (#114), so run this on amd64.
#
# Usage: hack/smoke-migrate-to-pvc.sh <mariadb|rabbitmq> [repo-root]
set -euo pipefail

COMPONENT="${1:?usage: $0 <mariadb|rabbitmq> [repo-root]}"
ROOT="$(cd "${2:-$(dirname "$0")/..}" && pwd)"
FROM="0.3.35"
TO="0.3.36"
CLUSTER="mig-${COMPONENT}"
WORK="$(mktemp -d)"
KC="$WORK/kubeconfig"
CTX="kind-$CLUSTER"
NS="openstack"
trap 'kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

case "$COMPONENT" in
  mariadb)
    STS="mariadb-server"
    KIND="Mariadb"
    HOSTPATH="/tmp/mysql-data"
    ;;
  rabbitmq)
    STS="rabbitmq-rabbitmq"
    KIND="Rabbitmq"
    HOSTPATH="/var/lib/rabbitmq"
    ;;
  *)
    echo "unknown component $COMPONENT" >&2
    exit 2
    ;;
esac

for tool in kind kubectl helm python3; do
  command -v "$tool" >/dev/null || { echo "FAIL: $tool is not installed, so nothing was tested" >&2; exit 2; }
done
# The upgrade must merge the way composition-dynamic-controller's Helm 3 library does. Helm 4
# applies server-side and fails on the mariadb Service selector that mariadb-controller owns,
# and only when that controller got there first, which makes the result depend on timing.
case "$(helm version --template '{{.Version}}')" in
  v3.*) ;;
  *) echo "FAIL: this needs Helm 3 to upgrade the way Krateo does; found $(helm version --short)" >&2; exit 2 ;;
esac

k() { kubectl --kubeconfig "$KC" --context "$CTX" -n "$NS" "$@"; }
h() { helm --kubeconfig "$KC" --kube-context "$CTX" -n "$NS" "$@"; }
step() { echo "=== $(date -u +%T) $*"; }
fail() { echo "FAIL: $*"; k get pods,pvc,sts -o wide 2>&1 || true; exit 1; }
migrate() { KUBECONFIG="$KC" python3 "$ROOT/hack/migrate-to-pvc.py" "$@" --context "$CTX" --namespace "$NS" --component "$COMPONENT"; }

write_marker() {
  case "$COMPONENT" in
    mariadb)
      k exec "$STS-0" -c mariadb -- mariadb --defaults-file=/etc/mysql/admin_user.cnf -N -e \
        "CREATE DATABASE m177; CREATE TABLE m177.t (v VARCHAR(40)); INSERT INTO m177.t VALUES ('survives-177');"
      ;;
    rabbitmq)
      k exec "$STS-0" -c rabbitmq -- rabbitmqctl add_vhost survives-177 >/dev/null
      ;;
  esac
}

read_marker() {
  case "$COMPONENT" in
    mariadb)
      k exec "$STS-0" -c mariadb -- mariadb --defaults-file=/etc/mysql/admin_user.cnf -N -e \
        "SELECT v FROM m177.t;" 2>/dev/null || true
      ;;
    rabbitmq)
      k exec "$STS-0" -c rabbitmq -- rabbitmqctl list_vhosts -q 2>/dev/null | grep -x survives-177 || true
      ;;
  esac
}

wait_ready() {
  for _ in $(seq 1 90); do
    [ "$(k get pod "$STS-0" -o jsonpath="{.status.containerStatuses[?(@.name==\"$COMPONENT\")].ready}" 2>/dev/null)" = true ] && return 0
    sleep 10
  done
  k describe pod "$STS-0" | tail -20 || true
  fail "$STS-0 never became Ready"
}

set_definition() {
  cat <<EOF | k apply -f - >/dev/null
apiVersion: core.krateo.io/v1alpha1
kind: CompositionDefinition
metadata:
  name: $COMPONENT
spec:
  chart:
    url: oci://ghcr.io/krateo-blueprints/charts/$COMPONENT
    version: "$1"
EOF
}

set_label() {
  k label "${COMPONENT}s.composition.krateo.io" "$COMPONENT" "krateo.io/composition-version=v${1//./-}" --overwrite >/dev/null
}

fresh_install() {
  kubectl --kubeconfig "$KC" --context "$CTX" delete ns "$NS" --ignore-not-found --wait=true >/dev/null
  docker exec "$CLUSTER-control-plane" rm -rf "$HOSTPATH"
  kubectl --kubeconfig "$KC" --context "$CTX" create ns "$NS" >/dev/null
  cat <<EOF | k apply -f - >/dev/null
apiVersion: composition.krateo.io/v${TO//./-}
kind: $KIND
metadata:
  name: $COMPONENT
  labels:
    krateo.io/composition-version: v${FROM//./-}
spec: {}
EOF
  set_definition "$FROM"
  h install "$COMPONENT" "$WORK/from/$COMPONENT" --wait=false >/dev/null
  [ -n "$(k get sts "$STS" -o jsonpath="{.spec.template.spec.volumes[?(@.hostPath.path==\"$HOSTPATH\")].name}")" ] \
    || fail "the $FROM chart did not render a hostPath at $HOSTPATH, so this would test nothing"
  wait_ready
}

step "cluster $CLUSTER"
kind create cluster --name "$CLUSTER" --kubeconfig "$KC" --wait 180s >/dev/null
kubectl --kubeconfig "$KC" --context "$CTX" label node "$CLUSTER-control-plane" openstack-control-plane=enabled >/dev/null
# An arm64 node refuses the amd64-only OpenStack-Helm images at pull time; the repo's loader
# pulls them as linux/amd64 and side-loads them. Not needed, and not run, on amd64.
if [ "$(kubectl --kubeconfig "$KC" --context "$CTX" get node "$CLUSTER-control-plane" -o jsonpath='{.status.nodeInfo.architecture}')" = arm64 ]; then
  COMPONENTS="$COMPONENT" "$ROOT/tools/kind-load-images.sh" "$CLUSTER" >/dev/null
fi
cat <<EOF | kubectl --kubeconfig "$KC" --context "$CTX" apply -f - >/dev/null
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: ${COMPONENT}s.composition.krateo.io
spec:
  group: composition.krateo.io
  scope: Namespaced
  names:
    plural: ${COMPONENT}s
    singular: $COMPONENT
    kind: $KIND
  versions:
    - name: v${TO//./-}
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
EOF
kubectl --kubeconfig "$KC" --context "$CTX" wait --for=condition=Established "crd/${COMPONENT}s.composition.krateo.io" >/dev/null
cat <<EOF | kubectl --kubeconfig "$KC" --context "$CTX" apply -f - >/dev/null
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: compositiondefinitions.core.krateo.io
spec:
  group: core.krateo.io
  scope: Namespaced
  names:
    plural: compositiondefinitions
    singular: compositiondefinition
    kind: CompositionDefinition
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
EOF
kubectl --kubeconfig "$KC" --context "$CTX" wait --for=condition=Established crd/compositiondefinitions.core.krateo.io >/dev/null

step "charts: released $FROM, and $TO from this tree"
mkdir -p "$WORK/from" "$WORK/to"
helm pull "oci://ghcr.io/krateo-blueprints/charts/$COMPONENT" --version "$FROM" --untar --untardir "$WORK/from" >/dev/null
cp -R "$ROOT/blueprints/$COMPONENT/chart" "$WORK/to/$COMPONENT"
sed -i.bak "s/CHART_VERSION/$TO/" "$WORK/to/$COMPONENT/Chart.yaml"
mkdir -p "$WORK/to/$COMPONENT/charts"
cp -R "$ROOT/charts/helm-toolkit" "$WORK/to/$COMPONENT/charts/"

step "install $FROM and write a marker"
fresh_install
write_marker
[ -n "$(read_marker)" ] || fail "the marker could not be read back before migrating"

step "prepare"
migrate prepare --apply
k get pvc "mysql-data-$STS-0" >/dev/null 2>&1 || k get pvc "rabbitmq-data-$STS-0" >/dev/null 2>&1 \
  || fail "prepare reported success and left no claim"
[ -z "$(k get sts "$STS" --ignore-not-found -o name)" ] || fail "prepare left the old StatefulSet"

step "finish refuses while the definition is still at $FROM, even with the label at $TO"
set_label "$TO"
if migrate finish --apply; then fail "finish ran while the CompositionDefinition still rendered $FROM"; fi
[ -z "$(k get sts "$STS" --ignore-not-found -o name)" ] || fail "a refused finish changed the StatefulSet"

step "upgrade to $TO, then finish, with the label back at $FROM"
h upgrade "$COMPONENT" "$WORK/to/$COMPONENT" --wait=false >/dev/null
set_definition "$TO"
set_label "$FROM"
migrate finish --apply
wait_ready

step "the marker survived"
[ -n "$(read_marker)" ] || fail "the marker is gone after the migration"
echo "  OK   marker read from $STS-0 on its claim"

step "prepare again has nothing to do"
migrate prepare --apply | grep -q "nothing to migrate" || fail "a second prepare did not recognise the migrated StatefulSet"

step "an emptied node path is refused"
h uninstall "$COMPONENT" --wait >/dev/null
fresh_install
docker exec "$CLUSTER-control-plane" sh -c "find '$HOSTPATH' -mindepth 1 -delete; mkdir -p '$HOSTPATH/.cache'"
if migrate prepare --apply; then fail "prepare accepted a node path with no state"; fi
[ -z "$(k get pvc -o name)" ] || fail "prepare created a claim from an empty node path"
echo "  OK   refused, and created no claim"

echo "PASS: $COMPONENT state moved from $HOSTPATH to its claim, and an empty path was refused"
