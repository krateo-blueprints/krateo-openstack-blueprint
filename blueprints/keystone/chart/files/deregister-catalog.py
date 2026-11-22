# Krateo (#444): delete, with their endpoints, the Keystone services of components that left the selection.
# DEREGISTER_SERVICES: [{"component": ..., "type": ..., "name": ...}], computed by the umbrella (osh.deregistration).
# A service is deleted only when BOTH its type and its name are what that component registers; any other service of
# the type is kept and named. Endpoints go first, so no endpoint is ever left pointing at a deleted service. Absent
# services are a no-op, so a re-run deletes nothing. One login per run (the admin openrc's OS_* variables).
import json
import os
import sys

import openstack

want = json.loads(os.environ.get("DEREGISTER_SERVICES") or "[]")
if not want:
    print("deregister: no service to deregister")
    sys.exit(0)
conn = openstack.connect(cloud="envvars")
ident = conn.identity
services = list(ident.services())
endpoints = list(ident.endpoints())
deleted, kept = 0, 0
for w in want:
    for s in services:
        if s.type != w["type"]:
            continue
        if s.name != w["name"]:
            print("deregister: keep service %s (type %s, name %s): %s registers the name %s, not this one"
                  % (s.id, s.type, s.name, w["component"], w["name"]))
            kept += 1
            continue
        for e in [e for e in endpoints if e.service_id == s.id]:
            ident.delete_endpoint(e, ignore_missing=True)
            print("deregister: %s: deleted endpoint %s (%s %s)" % (w["component"], e.id, e.interface, e.url))
        ident.delete_service(s, ignore_missing=True)
        print("deregister: %s: deleted service %s (type %s, name %s)" % (w["component"], s.id, s.type, s.name))
        deleted += 1
print("deregister: %d service(s) deleted, %d kept, for %d registration(s) of not-selected components"
      % (deleted, kept, len(want)))
