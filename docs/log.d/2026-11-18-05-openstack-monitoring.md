---
type: Log
title: Monitoring (#312, phase 1)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Monitoring (#312, phase 1).** New component `openstack-monitoring`, switched by the umbrella's `monitoring` value.
  Unset, it is on wherever the cluster serves alert-provider's CRDs. Off, the render is byte-identical.
  - **What it scrapes.** An OpenTelemetry collector forwards OTLP to the Krateo observability stack. The endpoint is an
    input, and nothing in the defaults is cloud-specific. It scrapes:
    - openstack-exporter 2.0.0-alpha, through its own generated Keystone user;
    - a standalone mysqld-exporter v0.21.0, whose least-privilege MariaDB user the mariadb chart creates without
      restarting the database;
    - RabbitMQ's `/metrics/detailed`.
  - **Why a plain collector Deployment.** It is a Deployment rather than an `OpenTelemetryCollector` CR because the
    Krateo stack runs no OpenTelemetry Operator. Its collectors come from the upstream opentelemetry-collector Helm
    chart.
  - **Alerts.** They are alert-provider CRs in `krateo-system`, rendered from one source file. Each has a runbook (see
    [the runbooks](../runbooks/openstack-api-down.md)) and a scrape-down companion. Each metric name is cited at the
    pinned image's tag.
  - **The gate.** `hack/validate-openstack-monitoring.py` checks the alerts, the credential readers and the switch, and
    plants a defect for each check.
  - **Not yet run on a cluster.**
