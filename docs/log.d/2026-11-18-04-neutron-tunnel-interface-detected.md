---
type: Log
title: neutron's tunnel interface is detected, not assumed
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **neutron's tunnel interface is detected, not assumed.** `network.interface.tunnel` defaulted to `ens4`, a GCE NIC
  name. On a node without `ens4` the OVS agent's init set no tunnel IP ("Var LOCAL_IP is empty"). The default is now
  empty, so the init uses the interface of the default route. That logic was already in the script; the `ens4` default
  had been skipping it.
  - **Existing installs keep `ens4`.** The old schema default was written into each stored neutron object (#179). It is
    harmless on GCE, where `ens4` is the right NIC. Elsewhere, clear `componentValues.neutron.network.interface.tunnel`
    or set it to the node's NIC.
  - **New gate:** `hack/validate-neutron-tunnel-interface.py` checks the empty default and the schema, and runs the
    rendered fallback.
