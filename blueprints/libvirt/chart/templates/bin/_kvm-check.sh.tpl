#!/bin/bash

{{/*
Krateo blueprint addition (#303): with conf.libvirt.virt_type kvm (umbrella compute.virtType: kvm), refuse to start on a
node without /dev/kvm. libvirtd's qemu processes, in this pod, are what open /dev/kvm when a guest starts; without it
every boot fails later, inside nova, with an error far from the cause. KVM_DEVICE exists only so the gate can run this
script against a stand-in path.
*/}}

set -e
dev="${KVM_DEVICE:-/dev/kvm}"
if [ ! -c "${dev}" ]; then
  echo "compute.virtType is kvm, but node ${NODE_NAME:-$(hostname)} has no ${dev}: enable hardware virtualization." >&2
  echo "On bare metal: VT-x or AMD-V in the firmware, with the kvm module (kvm_intel or kvm_amd) loaded." >&2
  echo "On cloud VMs: nested virtualization, on an instance type and image that offer it." >&2
  echo "Or set compute.virtType: qemu (software emulation). README \"Running guests on KVM\" has examples." >&2
  exit 1
fi
echo "${dev} present: guests run on KVM"
