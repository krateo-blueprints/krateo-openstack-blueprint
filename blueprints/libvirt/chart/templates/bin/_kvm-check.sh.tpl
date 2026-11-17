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
  echo "compute.virtType is kvm, but ${dev} does not exist on node ${NODE_NAME:-$(hostname)}: this node offers no hardware" >&2
  echo "virtualization to its pods. On GKE, run compute on a node pool created with --enable-nested-virtualization" >&2
  echo "(N2, Ubuntu; README \"Running guests on KVM\"), or set compute.virtType: qemu (software emulation)." >&2
  exit 1
fi
echo "${dev} present: guests run on KVM"
