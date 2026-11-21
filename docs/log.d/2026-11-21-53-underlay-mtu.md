---
type: Log
title: neutron's tunnel MTU follows the underlay, and the OVS agent refuses a smaller NIC
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **VXLAN networks advertised more than the tunnel carried (#433, found by dp303 run 4 on 0.3.74-main.gc16973f).**
  - **Measured:** on nodes whose NIC MTU is 1460, neutron gave a VXLAN network MTU 1450, its default 1500 minus the
    50-byte VXLAN overhead. A full-size DF ping from the router's namespace to the guest got
    `Frag needed and DF set (mtu = 1410)`: only 1460 - 50 fit. Small packets and SSH worked, so nothing else showed it.
    A guest sending full 1450-byte frames, for example a bulk TCP transfer without working path MTU discovery,
    stalls or drops them.
  - **0.3.74 shipped with it as a known issue (Diego).**
- **The fix (Diego's design, #433):**
  - **The input:** `network.underlayMTU` on the umbrella, an integer from 1280 to 9216, default 1500. It is always
    passed to neutron, as `conf.neutron.DEFAULT.global_physnet_mtu` and `conf.plugins.ml2_conf.ml2.path_mtu`, so a
    VXLAN network advertises underlayMTU - 50. An explicit `componentValues.neutron` entry wins.
  - **The refusal:** the OVS agent's init already finds the tunnel NIC from the route. It now reads that NIC's MTU and
    refuses to start when the MTU is below the one neutron uses for tunnels (ML2 `path_mtu`, else
    `global_physnet_mtu`, else 1500). The message names `network.underlayMTU: <the NIC's MTU>` and the MTU that would be
    advertised, with no provider assumed. An unreadable MTU refuses too. Upstream divergence `neutron-433`.
- **The guard:** `hack/validate-underlay-mtu.py`, in capability-roles. It checks five things:
  - the umbrella's span for every component: only neutron receives the value, 1500 when absent, and componentValues
    wins;
  - the schema's bounds;
  - the rendered `neutron.conf` and `ml2_conf.ini`, the files neutron reads, for 1460 and 1500;
  - the init's `UNDERLAY_MTU` for standalone values;
  - the refusal function, extracted from the rendered script and run against a fake `/sys/class/net`.

  Three mutants are all killed: the comparison removed, the call removed, and the umbrella's merge removed.
- **Upgrading:** on nodes whose NIC MTU is below 1500, set `network.underlayMTU` to it before upgrading; otherwise the OVS
  agent refuses to start there. Networks created before the change keep their MTU (README "neutron's underlay MTU").
