---
type: Log
title: Provider-agnostic KVM guidance
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **Provider-agnostic KVM guidance.** libvirt's `/dev/kvm` refusal no longer names a cloud provider. It says what to
  enable on any node: VT-x/AMD-V with the kvm module on bare metal, nested virtualization on cloud VMs, or
  `compute.virtType: qemu`. `hack/validate-virt-type.py` now asserts those terms, and that no provider is named. The
  README's "Running guests on KVM" leads with the generic requirement (nodes exposing `/dev/kvm`, labelled
  `openstack-compute-node=enabled`). Bare metal, GKE (measured), AWS and Azure follow as examples.
