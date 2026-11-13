#!/bin/bash

{{/*
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/}}

set -ex

keystone-manage \
    --config-file=/etc/keystone/keystone.conf \
    --config-dir=/etc/keystone/keystone.conf.d \
    db_sync

# Exempt the admin user from failed-login lockout (Krateo blueprint addition, #228).
# keystone.conf enables [security_compliance] lockout_failure_attempts, and the admin password is a
# machine credential the umbrella generates and rotates. While it rotates, every client still holding
# the previous value -- and every component authenticating before the bootstrap below applies the new
# one -- fails; five failures locked the admin account for 30 minutes at a time, for everyone
# (measured upgrading 0.3.51 to 0.3.52: Glance's hooks waited 34 min, the correct password got 401).
# The password is 32 random characters, so brute force is not what lockout would stop here.
# Set through Keystone's own provider API, exactly as keystone-manage's subcommands and bootstrap do:
# no authentication, so it cannot itself be locked out. Run BEFORE bootstrap, so the exemption holds
# before the reset can make anyone fail, and AFTER it, for a fresh install where bootstrap creates the
# user. Human and federated users keep the lockout.
ensure_admin_lockout_exempt() {
  python3 - <<'PYEOF'
import os
import sys

import keystone.conf
from keystone.common import provider_api
from keystone.common import sql
from keystone import exception
from keystone.server import backends

CONF = keystone.conf.CONF
keystone.conf.configure()
sql.initialize()
keystone.conf.set_default_for_default_log_levels()
CONF(args=["--config-file", "/etc/keystone/keystone.conf",
           "--config-dir", "/etc/keystone/keystone.conf.d"], project="keystone")
keystone.conf.setup_logging()
backends.load_backends()
identity = provider_api.ProviderAPIs.identity_api
name = os.environ["OS_BOOTSTRAP_USERNAME"]
try:
    user = identity.get_user_by_name(name, CONF.identity.default_domain_id)
except exception.UserNotFound:
    print("lockout exemption: user %s does not exist yet" % name)
    sys.exit(0)
if (user.get("options") or {}).get("ignore_lockout_failure_attempts") is True:
    print("lockout exemption: user %s already exempt" % name)
else:
    identity.update_user(user["id"], {"options": {"ignore_lockout_failure_attempts": True}})
    print("lockout exemption: set on user %s" % name)
PYEOF
}

ensure_admin_lockout_exempt

keystone-manage \
    --config-file=/etc/keystone/keystone.conf \
    --config-dir=/etc/keystone/keystone.conf.d \
    bootstrap

ensure_admin_lockout_exempt
