---
type: Log
title: Every long-running container has a readiness probe (#231, #151)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Every long-running container has a readiness probe (#231, #151).** On a 0.3.47 cold install,
  watcher-decision-engine exited 1 seven seconds after every start, had no probe, and counted as Ready
  in each window, so its composition and the umbrella went Ready=True. 32 of 76 long-running containers
  had no readiness probe.
  - **RPC workers** (conductors, schedulers, engines, managers, agents: 30 containers in 16 charts) run
    `worker-health-probe.py`, one script in helm-toolkit (`helm-toolkit.scripts.health_probe`). It is
    OpenStack-Helm's nova and neutron `health-probe.py` made generic. It finds the program in /proc,
    limited to its own container's processes, and reads the program's own `--config-file` and
    `--config-dir` arguments. It requires an ESTABLISHED RabbitMQ connection, then sends an RPC call to
    the program's own `topic.server` queue, which only a consuming server answers. Each topic, exchange
    and server name was read from the stable/2026.1 source, including the cases that aren't
    `CONF.host`: cinder and manila `host@backend`, mistral, trove and blazar `0.0.0.0`, cloudkitty and
    ironic `getfqdn()`, magnum's random id (topic only). The RPC charts set `rpc_ping_enabled`, so a
    server answers the probe at DEBUG rather than logging an ERROR every minute.
  - **Other workers:**
    - Notification listeners (aodh listener and notifier, ceilometer-notification,
      ironic-neutron-agent) require their RabbitMQ consumer connection.
    - octavia-health-manager, octavia-driver-agent and ironic's tftp sidecar require their listening
      socket, attributed by inode.
    - mariadb-controller writes a heartbeat after every pass.
    - skyline's nginx is probed through its proxy.
    - nova-compute-ironic uses nova's own probe.
    - tacker-server gets an HTTP probe on its versions document.
  - **Readiness only for RPC workers** (#146): an RPC failure can't tell a wedged process from a
    broker outage, and a liveness probe on it restarts every replica at once and repairs nothing.
    tacker-server is the one new liveness probe. It asks `/`, which needs no dependency, and has a
    startup probe because its start script installs packages first.
  - **Exceptions:** 12 containers have no signal a probe can test (pollers, periodic loops, masakari's
    monitors, designate-sink with no handlers). Each is listed with its reason in
    `hack/readiness-probe-exceptions.yaml`.
  - **New gates:** `hack/validate-readiness-probes.py` renders every chart at its defaults and with
    every workload on (CI, 2026-11-17: 114 long-running containers in 37 charts; 53 without a readiness
    probe on main, 12 on this change, all listed). It fails when a container has no probe and no exception, when an exception is
    unneeded, when a probe is `true`, or when a probe's /tmp script isn't mounted (this caught
    tacker-conductor's). `hack/validate-health-probe.py` runs the rendered script against
    oslo.messaging's real RPC server and dispatcher, and against real processes and sockets in /proc.
