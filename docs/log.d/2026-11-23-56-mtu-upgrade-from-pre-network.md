---
type: Log
title: the MTU upgrade note could not be followed; it now sets neutron's MTU through componentValues first
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The README's MTU upgrade step was impossible (found by r074 run 2, 0.3.71 -> 0.3.74-main.geb42756 on GKE).**
  - **What the note said:** "set `network.underlayMTU` before you upgrade".
  - **Why it can't work:** 0.3.71's Openstack schema has no `network`, and its top level is not preserve-unknown. The API
    server therefore drops the field from the live object. The upgrade's new apiVersion then writes the schema default,
    1500.
  - **What the run measured:** the OVS agent's init refused on the first node it reached (`ens4 has MTU 1460, below the
    underlay MTU 1500`). Its DHCP, L3, metadata and nova-compute agents waited in init, and neutron and nova stayed
    `Creating`.
  - The run's harness had also missed the note (it read the previous commit's README). Setting `network.underlayMTU:
    1460` afterwards was logged as intervention 1.
- **The fix (Diego: pre-set via componentValues):** the README now tells an operator on sub-1500 nodes to set
  `componentValues.neutron.conf.neutron.DEFAULT.global_physnet_mtu` and `conf.plugins.ml2_conf.ml2.path_mtu` on the
  release they are running, before the upgrade.
  - 0.3.71 keeps both: `componentValues.neutron` and neutron's `conf` are preserve-unknown.
  - 0.3.74 lets them win over `network.underlayMTU`, and the refusal compares the merged `path_mtu`, so the OVS agent
    starts.
  - The value can later move to `network.underlayMTU`: neutron receives the same spec, so that does not restart it.
  - Fresh installs set `network.underlayMTU` as before.
- **The guard:** `hack/validate-underlay-mtu.py` part 5 parses the README's block and checks it against 0.3.71's
  schemas, frozen from the tag in `hack/fixtures/`.
  - **The premise:** 0.3.71 has no `network` and prunes unknown top-level fields.
  - **Kept:** the block's paths are preserve-unknown, and helm accepts the block.
  - **The new release:** neutron's spec, `UNDERLAY_MTU` and `check_underlay_mtu` against a NIC of that MTU.
  - **The move:** neutron's spec is equal after it.
  - **A misspelt block:** must be caught.
- r074 run 3 tests this procedure on a real upgrade before 0.3.74 is tagged.
