---
type: Decision
title: krateo-openstack-blueprint — Marketplace icons, sources and licences
description: Why every chart's icon is self-hosted here or the org logo, where each file comes from, and the CC BY-ND rule that the OpenStack mascot files are never modified.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack
tags:
  - icons
  - marketplace
  - licence
  - attribution
timestamp: 2026-11-10T00:00:00Z
---

# Marketplace icons: sources and licences

The charts' `icon:` fields point here, on a host this repository controls. Third-party URLs
moved and broke 23 of 37 tiles at 0.3.45. These files are the OpenStack project mascots.

## Licence: CC BY-ND, unmodified

The OpenStack project mascots are © the Open Infrastructure Foundation and licensed under
**Creative Commons Attribution-NoDerivatives (CC BY-ND)**. In the Foundation's own words, in the
official `openstack/i18n` repository
(<https://opendev.org/openstack/i18n/src/branch/master/resources/mascot/LICENSE>):

> Logos are licensed under the Creative Commons CC-BY-ND, similar to other Foundation materials.
> That means you can use them freely, but can't make derivative work.

**Do not modify these files:** no resizing, recolouring, cropping or format conversion. Replace a
file only with a newer official one from the Foundation. Each file is byte-for-byte the official
`OpenStack_Project_<Name>_mascot.png` from the Foundation's mascot gallery storage:

`https://object-storage-ca-ymq-1.vexxhost.net/swift/v1/6e4619c416ff4bd19e1c087f27a43eea/www-images-prod/project-mascots/<Name>/OpenStack_Project_<Name>_mascot.png`

| file | project mascot (`<Name>`) | used by chart |
|---|---|---|
| `barbican.png` | Barbican | barbican |
| `cinder.png` | Cinder | cinder |
| `cloudkitty.png` | Cloudkitty | cloudkitty |
| `designate.png` | Designate | designate |
| `freezer.png` | Freezer | freezer |
| `glance.png` | Glance | glance |
| `heat.png` | Heat | heat |
| `horizon.png` | Horizon | horizon |
| `ironic.png` | Ironic | ironic |
| `keystone.png` | Keystone | keystone |
| `magnum.png` | Magnum | magnum |
| `manila.png` | Manila | manila |
| `masakari.png` | Masakari | masakari |
| `mistral.png` | Mistral | mistral |
| `neutron.png` | Neutron | neutron |
| `nova.png` | Nova | nova |
| `octavia.png` | Octavia | octavia |
| `swift.png` | Swift | swift |
| `tacker.png` | Tacker | tacker |
| `trove.png` | Trove | trove |
| `watcher.png` | Watcher | watcher |
| `zaqar.png` | Zaqar | zaqar |
| `telemetry.png` | Telemetry (the project team that owns aodh and ceilometer) | aodh, ceilometer |

Downloaded 2026-11-10.

## Charts without an OpenStack mascot

These use the organisation's standard logo,
`https://raw.githubusercontent.com/krateo-platformops/.github/main/brand/logo.svg`, rather than a
third-party trademark:
- no mascot in the published gallery: blazar, cyborg, placement, skyline;
- gnocchi, which left OpenStack governance, so the Telemetry mascot would misattribute it;
- the infrastructure charts: libvirt, mariadb, memcached, openvswitch, powerdns, rabbitmq, redis;
- the `openstack` umbrella.
