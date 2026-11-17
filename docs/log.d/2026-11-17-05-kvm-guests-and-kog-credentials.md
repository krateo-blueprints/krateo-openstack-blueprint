---
type: Log
title: KVM guests and KOG credentials (0.3.73, #303)
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **KVM guests and KOG credentials (0.3.73, #303).** These are the blueprint's half of the data-plane release stage.
  - **`compute.virtType: kvm`:** sets nova `virt_type=kvm` and `cpu_mode=host-model` and libvirt's `virt_type`. libvirt
    then refuses, in an init container, to start on a node without `/dev/kvm`; the message names GKE's
    `--enable-nested-virtualization`. qemu, the default, renders as before.
  - **`kogCredentials.enabled`:** has keystone render, from the generated admin root and in the release's namespace,
    `openstack-kog-clouds` (an ExternalSecret clouds.yaml for the KOG auth proxies), `openstack-kog-token` (nova-kog's
    admin token) and `openstack-kog-endpoints` (nova's internal URL with the admin project id, keystone's internal URL
    and the region, as keystone answered them; no secret). keystone's post-install,post-upgrade hook Job and a CronJob
    (every 6 h) write the token, `keystone_url` and `region_name`. nova's own hook Job writes `nova_url` once nova
    registered its endpoint. Both run one shared program (`helm-toolkit.snippets.kog_endpoints_pod`), at most 4 tries
    per run, and merge-patch only their own keys. A failed mint fails that chart's release. Only the mint step holds
    the password, and each writer may patch only its own objects.
    Cross-namespace is not supported yet. The README gives each KOG's setting for these names, read from the KOG
    charts.
  - **New gates:** `hack/validate-virt-type.py` and `hack/validate-kog-credentials.py`.
