---
type: Log
title: Public endpoints over HTTPS through the Gateway API
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Public endpoints over HTTPS through the Gateway API (#311, phase 1).**
  - **Umbrella `publicEndpoints`:** off by default, and off renders byte-identical. When on, the umbrella renders:
    - one Gateway (`gatewayClassName`, default `agentgateway`, which Krateo's `features.ingress` installs);
    - an HTTPRoute per public endpoint of every selected component;
    - an HTTP-to-HTTPS redirect;
    - a cert-manager Certificate from `tls.issuerRef`;
    - an ingress NetworkPolicy set: deny by default, allow the namespace, `nodeCIDRs`, the Gateway proxy, and
      `allowNamespaces`. The policies select every pod without the label `krateo.io/networkpolicy-exempt`, in block
      style; the label is an explicit opt-out.
  - **The components:**
    - Each public catalog URL becomes `https://<service>.<baseDomain>`, with proxy-header parsing set.
    - The charts' inert Ingress switches go off (upstream removed those templates in 6ec1132485).
    - Horizon becomes ClusterIP with Secure cookies.
    - Routes set HSTS, and overwrite X-Forwarded-Proto with `https`.
    - Never routed: metadata, RabbitMQ, MariaDB, memcached.
  - **Refused on a live cluster** without the Gateway API or cert-manager CRDs, and on a missing input.
  - **The new gate:** `hack/validate-public-endpoints.py` checks:
    - off equals before;
    - the routes against what each chart registers;
    - the deny list, HSTS and redirect, and the policy's exact peers;
    - the refusals;
    - every routed chart rendered with the values the umbrella gives it (backend Service, catalog URL, no Ingress,
      proxy headers, Horizon);
    - the tables against the charts.

    It also runs thirteen mutations, each of which must be reported as the defect it plants.
  - **Not yet verified on a cluster.** The live checks are in the README. Follow-ups: #325, #326, #327.
