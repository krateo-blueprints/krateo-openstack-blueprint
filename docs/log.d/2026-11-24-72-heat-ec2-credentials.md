---
type: Log
title: heat checks a CloudFormation signature with its service credentials
description: Log entry, rendered into docs/log.md by hack/render-log.py.
---
- **The defect (#481, GKE, 2026-11-24):** heat's CloudFormation API answered 403 to every signed request. Keystone's `/v3/ec2tokens`
  requires an authenticated caller now, and heat's `[ec2authtoken]` (heat 0242c28d) had no credentials. Anything that calls heat
  with EC2 credentials takes that path, wait conditions with a signed URL included (not exercised). The component read Ready.
- **The fix:** `auth_section: keystone_authtoken` (the service user it already has), `valid_interfaces: internal`, and
  `endpoint_override` from `endpoints.identity` without its path, because heat appends `/v3/ec2tokens`.
- **Verified on the 0.3.75 candidate's cluster, through `componentValues`:** the admin's signed request answered 200, unsigned 400.
- **Upstream, separately:** with an unknown access key heat-api-cfn fails on an unhandled Keystone 404 and drops the connection. It
  fails closed.
- **The guard:** `hack/validate-heat-ec2.py`. Found by the release run's every-service bar, whose signed request was new.
