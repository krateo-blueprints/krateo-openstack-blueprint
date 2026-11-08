# kagent Agent: `openstack-blueprint-expert`

A [kagent](https://kagent.dev) Agent CRD that turns an LLM into a
subject-matter expert on this blueprint:

- The per-component architecture: ONE Krateo blueprint per OpenStack-Helm
  chart (34 of them) plus the `Openstack` umbrella orchestrator.
- The umbrella's two-pass, **self-bootstrapping** engine, whose creation gate is
  decided per dependency edge via a re-evaluated Helm `lookup`: readiness by
  default (`osh.crdExists` + `osh.depsReady` -> `osh.ready`), existence only for
  the edges in `.Values.dependencyRelaxations` (`osh.depsExist` -> `osh.exists` /
  `osh.paused`). Exactly one edge is relaxed today, `nova -> neutron`, because
  readiness there is unreachable against openstack-helm's own
  `kubernetes-entrypoint` waits (#110) — and `hack/validate-readiness-deadlock.py`
  proves the set is neither short nor stale.
- The `profile` / `enabled` (transitive dependency closure) /
  `componentValues` (typed per-component passthrough) operator surface.
- The comparison against `kolla-ansible` (see
  [`docs/kolla-ansible-audit-and-helm-plan.md`](../docs/kolla-ansible-audit-and-helm-plan.md)).

The system prompt embeds the non-obvious gotchas (chart-inspector caches by
version, chart version -> composition apiVersion coupling, `lookup` errors on
an unregistered GVR / self-bootstrap, private-package release 403,
componentValues CRD defaulting, block-style-only YAML), the determinism /
hook-stripping rules the Krateo CDC path requires, and the stuck-install triage
recipe.

## Prerequisites

### 1. Install kagent

Pulled directly from the GHCR OCI registry — no `helm repo add` needed.
Verified working on kagent v0.9.6.

```bash
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

Swap `providers.default` for `openAI`, `gemini`, `azureOpenAI`, or `ollama` if
you want a non-Anthropic auto-`ModelConfig`. The chart auto-creates a
`ModelConfig` named `default-model-config` matching whichever provider you
pick.

### 2. ModelConfig: Gemini on Vertex AI (what this repo ships)

The Agent here references a `ModelConfig` named **`vertex-gemini`** of kind
`GeminiVertexAI` (see
[`modelconfig-vertex-gemini.yaml`](./modelconfig-vertex-gemini.yaml)). We use
Vertex rather than the chart's auto-created provider so the agent runs on
GCP-managed Gemini with service-account auth.

**One-time GCP prep:**

1. Enable the Vertex AI API on your GCP project.
2. Create a service account (e.g. `kagent-vertex`) with role
   **`roles/aiplatform.user`** (displayed as "Agent Platform User" in the
   current GCP UI — same role ID, recent rebrand).
3. Download a JSON key for the service account.

**Create the secret** kagent will mount as `/creds/key.json`:

```bash
kubectl -n kagent create secret generic kagent-vertex \
  --from-file=key.json=$HOME/Downloads/<your-sa-key>.json
```

**Edit and apply the `ModelConfig`**, setting `projectID` and `location` to
your GCP project + region:

```bash
$EDITOR kagent/modelconfig-vertex-gemini.yaml   # set projectID and location
kubectl apply -f kagent/modelconfig-vertex-gemini.yaml
```

The kagent controller's translator auto-injects `GOOGLE_CLOUD_PROJECT`,
`GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI=true`, and
`GOOGLE_APPLICATION_CREDENTIALS=/creds/key.json` into the agent pod, and mounts
the `kagent-vertex` secret as a volume at `/creds/`. No further auth wiring
required.

#### Alternative providers

| `spec.provider`     | secret name        | secret key            | notes                          |
|---------------------|--------------------|-----------------------|--------------------------------|
| `Anthropic`         | `kagent-anthropic` | `ANTHROPIC_API_KEY`   | direct Anthropic API           |
| `OpenAI`            | `kagent-openai`    | `OPENAI_API_KEY`      |                                |
| `Gemini`            | `kagent-gemini`    | `GOOGLE_API_KEY`      | AI Studio (not Vertex)         |
| `GeminiVertexAI`    | `kagent-vertex`    | `key.json` (file)     | this repo                      |
| `AnthropicVertexAI` | same shape         | `key.json` (file)     | Claude via Vertex Model Garden |

Edit `spec.declarative.modelConfig` in
[`agent-openstack-expert.yaml`](./agent-openstack-expert.yaml) if you swap
providers.

### 3. Built-in `kagent-tool-server`

Shipped automatically by the kagent helm chart as a `RemoteMCPServer`. Provides
the `k8s_*` and `helm_*` tool families the Agent uses. The whole blueprint is
reconciled through standard Kubernetes resources (`CompositionDefinition`s,
`Composition`s) + Helm, so that toolset covers it — no custom MCP server is
required.

## Apply

```bash
kubectl apply -f kagent/agent-openstack-expert.yaml
kubectl -n kagent get agent openstack-blueprint-expert
```

Then open the kagent UI (or the A2A endpoint) and ask one of the example
prompts under `spec.declarative.a2aConfig.skills`:

- "How does the Openstack umbrella roll out components in dependency order?"
- "How do I deploy just heat and its dependencies via the umbrella?"
- "How do I scale keystone to 2 replicas without touching its Composition?"
- "Why do all the pods keep restarting every reconcile?"
- "My Openstack Composition is Synced=False with a lookup error."
- "Is the umbrella values.yaml equal to kolla's globals.yml?"

## What the agent can do

- Read `Openstack`, the per-component `Composition`s, their
  `CompositionDefinition`s, pods and helm releases, and explain the live
  two-pass position (which deps are Ready, what unlocks next).
- Patch the `Openstack` CR's `spec.profile` / `spec.enabled` /
  `spec.componentValues.<name>` to (re)configure a component through the
  umbrella — e.g. scale `keystone.pod.replicas.api`, set neutron's tunnel
  interface — with confirmation.
- Tail `kubernetes-entrypoint` init-container logs and the cdc /
  core-provider logs when diagnosing a reconciliation hang.
- Cite the canonical determinism / hook / kolla-comparison rules from the
  blueprint and `docs/kolla-ansible-audit-and-helm-plan.md` where relevant.

## What the agent will not do

- `helm_upgrade` a release a `CompositionDefinition` + composition-dynamic-
  controller owns — the cdc re-renders it from the live Composition each
  reconcile, so a manual upgrade is silently reverted. It edits the
  `Openstack` CR's `componentValues` (or the standalone Composition spec)
  instead.
- Recommend deleting a `CompositionDefinition` while live Compositions of its
  Kind exist — that can strand the generated CRD `Terminating` and GC every
  instance. It walks you through letting the CD's GC complete first.
- Invent a central `globals.yml` — this blueprint achieves kolla's
  "few inputs -> full config" via curated per-component schemas, not a central
  derivation, and the agent says so.
