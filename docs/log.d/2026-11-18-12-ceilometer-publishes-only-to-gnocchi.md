---
type: Log
title: ceilometer publishes only to gnocchi (#294)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **ceilometer publishes only to gnocchi (#294).** Both pipelines listed `notifier://` next to `gnocchi://`. The
  notifier publisher sends every sample and event back onto ceilometer's vhost, as topics `metering` and `event`
  (stable/2026.1 `ceilometer/publisher/messaging.py`), and nothing in this blueprint listens there: their consumers, the
  collector and panko, are retired. On GKE (2026-11-17) `event.sample` went from 954 to 1062 messages in 10 minutes
  with 0 consumers, and `metering.sample` grew too. The sinks now publish to `gnocchi://` alone. New gate
  `hack/validate-ceilometer-publishers.py` renders ceilometer and requires each publisher to reach a consumer in its
  `deps` closure: gnocchi's API, or a listener on that topic and vhost other than ceilometer's own agent (which would
  re-ingest its own samples). Any other publisher fails until its consumer is measured. An upstream-divergences entry
  keeps `notifier://` out across a re-vendor. On an existing install the two queues stop growing but keep what they
  hold until deleted (`rabbitmqctl delete_queue -p ceilometer event.sample`, and `metering.sample`). ceilometer.conf
  sets no `[oslo_messaging_rabbit]`, so at oslo.messaging's defaults they're non-durable and a RabbitMQ restart also
  drops them (read from the defaults, not measured live).
