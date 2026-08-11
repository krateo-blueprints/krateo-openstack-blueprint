---
type: Runbook
title: krateo-openstack-blueprint — quickstart
description: The happy path — deploy the Openstack umbrella Composition, reconfigure a component via spec.componentValues, then optionally install the openstack-blueprint-expert kagent SME.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags: [openstack, quickstart, orchestrator, kagent]
timestamp: 2026-08-11T00:00:00Z
---

# Quickstart — deploy the umbrella, then install the kagent SME

Stand up a whole OpenStack install from **one** `Openstack` Composition (the
orchestrator rolls the components out in dependency order), then optionally add
the [`openstack-blueprint-expert`](../kagent) kagent agent so an LLM can read
and drive it.

For cluster-specific notes (image pre-loading, compute on QEMU, Horizon over a
LoadBalancer, teardown) see [`quickstart-kind.md`](../quickstart-kind.md) and
[`quickstart-gke.md`](../quickstart-gke.md). This page is the happy path:
**umbrella up → kagent.**

## Prerequisites

A Kubernetes cluster with `kubectl` access plus Helm 3 and Krateo's
`core-provider`. The identity plane runs on kind (Apple Silicon supported);
the compute and bare-metal planes need an amd64 cluster — see the GKE
quickstart.

```sh
helm repo add jetstack https://charts.jetstack.io
helm repo add krateo https://charts.krateo.io
helm repo update

helm upgrade --install cert-manager jetstack/cert-manager -n cert-manager \
  --create-namespace --set crds.enabled=true --wait

helm upgrade --install core-provider krateo/core-provider \
  --version 1.0.0 -n krateo-system --create-namespace --wait
```

## 1. Register the orchestrator blueprint

Only the umbrella `CompositionDefinition` is registered by hand — it registers
each component blueprint itself.

```sh
kubectl create namespace openstack-system
kubectl apply -f blueprints/openstack/compositiondefinition.yaml
kubectl wait compositiondefinition/openstack -n openstack-system \
  --for=condition=Ready --timeout=180s
```

`core-provider` pulls `oci://ghcr.io/krateo-blueprints/charts/openstack:0.2.0`,
generates the `Openstack` CRD (`composition.krateo.io/v0-2-0`) and its
controller.

## 2. Deploy one OpenStack install

One `Openstack` Composition = one install. Pick the scope with `spec`:

- `profile: identity` — MariaDB + Memcached + Keystone + Glance + Horizon.
- `profile: full` — the whole 34-component catalog (needs a large amd64
  cluster).
- `spec.enabled` — an explicit component list that overrides `profile` and is
  expanded to its **transitive dependency closure**, so you get exactly what
  you ask for plus what it needs.

```sh
kubectl create namespace openstack
kubectl apply -f examples/openstack.yaml      # spec.profile: identity
```

Or deploy a precise subset — e.g. just Heat and everything it depends on:

```sh
kubectl apply -f - <<'EOF'
apiVersion: composition.krateo.io/v0-2-0
kind: Openstack
metadata:
  name: openstack
  namespace: openstack
spec:
  enabled:
    - heat            # -> heat + keystone + rabbitmq + mariadb + memcached
EOF
```

The umbrella self-bootstraps: on the first reconcile only zero-dependency
components render; each later pass unlocks the next tier once its dependency
Compositions report `Ready=True`. Watch it converge:

```sh
kubectl get openstack openstack -n openstack -o \
  jsonpath='{.status.conditions}' ; echo
kubectl get compositions.composition.krateo.io -n openstack
helm list -n openstack          # per-component releases appear in dep order
```

To **reconfigure** a component without touching its Composition, patch the
`Openstack` CR's `spec.componentValues.<name>` (strictly typed — every
component's own `values.schema.json` is merged into the umbrella CRD). For
example, scale keystone-api to 2 replicas:

```sh
kubectl patch openstack openstack -n openstack --type merge -p '
spec:
  componentValues:
    keystone:
      pod:
        replicas:
          api: 2
'
```

The umbrella re-renders keystone's Composition and the composition-dynamic-
controller reconciles it — `keystone-api` scales 1 -> 2 live.

## 3. Verify

```sh
kubectl -n openstack run osclient --rm -it --restart=Never \
  --image=quay.io/airshipit/openstack-client:2025.1-ubuntu_jammy \
  --env OS_AUTH_URL=http://keystone-api.openstack.svc.cluster.local:5000/v3 \
  --env OS_USERNAME=admin --env OS_PASSWORD=password --env OS_PROJECT_NAME=admin \
  --env OS_USER_DOMAIN_NAME=Default --env OS_PROJECT_DOMAIN_NAME=Default \
  --env OS_IDENTITY_API_VERSION=3 --env OS_REGION_NAME=RegionOne --env OS_INTERFACE=internal \
  --command -- openstack token issue
```

For the compute plane (`profile: full` on GKE) also try `compute service
list`, `hypervisor list`, `network agent list`, and log in to Horizon as
`admin` / `password` (domain `Default`). See [`quickstart-gke.md`](../quickstart-gke.md).

## 4. (Optional) Install the kagent SME

[`kagent/openstack-blueprint-expert`](../kagent) is a [kagent](https://kagent.dev)
Agent CRD that turns an LLM into a subject-matter expert on this blueprint — it
reads the live `Openstack` / `Composition` / `CompositionDefinition` objects,
explains the two-pass engine, and can patch `spec.componentValues` to
(re)configure a component (with confirmation).

```sh
kubectl create ns kagent --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kagent-crds \
  oci://ghcr.io/kagent-dev/kagent/helm/kagent-crds \
  --version 0.9.6 --namespace kagent --wait --timeout 5m

helm upgrade --install kagent \
  oci://ghcr.io/kagent-dev/kagent/helm/kagent \
  --version 0.9.6 --namespace kagent \
  --set providers.default=anthropic \
  --set providers.anthropic.model=claude-sonnet-4-6 \
  --timeout 10m
```

The agent ships referencing a `GeminiVertexAI` ModelConfig named
`vertex-gemini`. Either create that (one-time GCP prep + `kagent-vertex`
secret, see [`kagent/README.md`](../kagent/README.md)) and apply both files, or
point the agent at the chart's auto-created `default-model-config` for whatever
provider you set above:

```sh
# Option A — Vertex Gemini (what the repo ships)
kubectl -n kagent create secret generic kagent-vertex \
  --from-file=key.json=$HOME/Downloads/<your-sa-key>.json
$EDITOR kagent/modelconfig-vertex-gemini.yaml         # set projectID + location
kubectl apply -f kagent/modelconfig-vertex-gemini.yaml
kubectl apply -f kagent/agent-openstack-expert.yaml

# Option B — reuse the provider you installed kagent with
sed 's/modelConfig: vertex-gemini/modelConfig: default-model-config/' \
  kagent/agent-openstack-expert.yaml | kubectl apply -f -
```

```sh
kubectl -n kagent get agent openstack-blueprint-expert
```

Open the kagent UI (or its A2A endpoint) and ask one of the skill examples:

- "How does the Openstack umbrella roll out components in dependency order?"
- "How do I deploy just heat and its dependencies via the umbrella?"
- "How do I scale keystone to 2 replicas without touching its Composition?"
- "My Openstack Composition is Synced=False with a lookup error."
- "Is the umbrella values.yaml equal to kolla's globals.yml?"

The agent reads before it acts, configures components by patching the
`Openstack` CR (never `helm_upgrade` on a CDC-owned release), and walks you
through GC ordering before any `CompositionDefinition` delete. See
[`kagent/README.md`](../kagent/README.md) for what it will and won't do.

## Next steps

- [`README.md`](../README.md) — the per-component + umbrella architecture.
- [`quickstart-kind.md`](../quickstart-kind.md) / [`quickstart-gke.md`](../quickstart-gke.md) — cluster-specific recipes.
- [`docs/kolla-ansible-audit-and-helm-plan.md`](kolla-ansible-audit-and-helm-plan.md) — the kolla-ansible comparison the kagent SME quotes.
