#!/usr/bin/env python

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

import argparse
import base64
import errno
import grp
import logging
import os
import pwd
import re
import subprocess  #nosec
import sys
import time

import requests

FERNET_DIR = os.environ['KEYSTONE_KEYS_REPOSITORY']
KEYSTONE_USER = os.environ['KEYSTONE_USER']
KEYSTONE_GROUP = os.environ['KEYSTONE_GROUP']
NAMESPACE = os.environ['KUBERNETES_NAMESPACE']

# k8s connection data
KUBE_HOST = None
KUBE_CERT = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
KUBE_TOKEN = None

LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FORMAT = "%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s"
logging.basicConfig(format=LOG_FORMAT, datefmt=LOG_DATEFMT)
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


def read_kube_config():
    global KUBE_HOST, KUBE_TOKEN
    KUBE_HOST = "https://%s:%s" % ('kubernetes.default',
                                   os.environ['KUBERNETES_SERVICE_PORT'])
    with open('/var/run/secrets/kubernetes.io/serviceaccount/token', 'r') as f:
        KUBE_TOKEN = f.read()


def get_secret_definition(name):
    url = '%s/api/v1/namespaces/%s/secrets/%s' % (KUBE_HOST, NAMESPACE, name)
    resp = requests.get(url,
                        headers={'Authorization': 'Bearer %s' % KUBE_TOKEN},
                        verify=KUBE_CERT)
    if resp.status_code != 200:
        LOG.error('Cannot get secret %s.', name)
        LOG.error(resp.text)
        return None
    return resp.json()


def create_secret(name):
    """Create an empty Secret so setup has something to populate.

    The Job's Role already grants `create` on secrets -- it always did -- but this
    script only ever implemented get and update, so a Secret that did not exist yet
    was a hard failure. That is fine while something else emits the Secret first, and
    fatal once nothing does: see #74, where the chart stopped emitting an unpopulated
    Secret (#61, to stop a reconcile wiping populated keys) and fresh installs could
    no longer come up at all.

    Creating it here resolves that for good. The chart can keep its rule of never
    emitting an unpopulated Secret -- so a render can never wipe keys -- while a fresh
    install still gets one, because the component that generates the keys also owns
    bringing the Secret into existence.
    """
    url = '%s/api/v1/namespaces/%s/secrets' % (KUBE_HOST, NAMESPACE)
    body = {
        'apiVersion': 'v1',
        'kind': 'Secret',
        'type': 'Opaque',
        'metadata': {
            'name': name,
            'namespace': NAMESPACE,
            'annotations': {'helm.sh/resource-policy': 'keep'},
        },
        'data': {},
    }
    resp = requests.post(url,
                         json=body,
                         headers={'Authorization': 'Bearer %s' % KUBE_TOKEN},
                         verify=KUBE_CERT)
    if resp.status_code == 409:
        # Raced with another setup Job, or with a render that emitted it. Either way
        # it exists now, which is all this needs.
        LOG.info("Secret '%s' already created concurrently; re-reading it.", name)
        return get_secret_definition(name)
    if resp.status_code not in (200, 201):
        LOG.error('Cannot create secret %s.', name)
        LOG.error(resp.text)
        return None
    LOG.info("Created empty secret '%s'.", name)
    return resp.json()


def update_secret(name, secret):
    url = '%s/api/v1/namespaces/%s/secrets/%s' % (KUBE_HOST, NAMESPACE, name)
    resp = requests.put(url,
                        json=secret,
                        headers={'Authorization': 'Bearer %s' % KUBE_TOKEN},
                        verify=KUBE_CERT)
    if resp.status_code != 200:
        LOG.error('Cannot update secret %s.', name)
        LOG.error(resp.text)
        return False
    return True


def read_from_files():
    keys = [name for name in os.listdir(FERNET_DIR) if os.path.isfile(FERNET_DIR + name)
            and re.match("^\d+$", name)]
    data = {}
    for key in keys:
        with open(FERNET_DIR + key, 'r') as f:
            data[key] = f.read()
    if len(list(keys)):
        LOG.debug("Keys read from files: %s", keys)
    else:
        LOG.warning("No keys were read from files.")
    return data


def get_keys_data():
    keys = read_from_files()
    return dict([(key, base64.b64encode(value.encode()).decode())
                for (key, value) in keys.items()])


def write_to_files(data):
    if not os.path.exists(os.path.dirname(FERNET_DIR)):
        try:
            os.makedirs(os.path.dirname(FERNET_DIR))
        except OSError as exc: # Guard against race condition
            if exc.errno != errno.EEXIST:
                raise
        uid = pwd.getpwnam(KEYSTONE_USER).pw_uid
        gid = grp.getgrnam(KEYSTONE_GROUP).gr_gid
        os.chown(FERNET_DIR, uid, gid)

    for (key, value) in data.items():
        with open(FERNET_DIR + key, 'w') as f:
            decoded_value = base64.b64decode(value).decode()
            f.write(decoded_value)
            LOG.debug("Key %s: %s", key, decoded_value)
    LOG.info("%s keys were written", len(data))


def execute_command(cmd):
    LOG.info("Executing 'keystone-manage %s --keystone-user=%s "
             "--keystone-group=%s' command.",
             cmd, KEYSTONE_USER, KEYSTONE_GROUP)
    subprocess.call(['keystone-manage', cmd,  #nosec
                     '--keystone-user=%s' % KEYSTONE_USER,
                     '--keystone-group=%s' % KEYSTONE_GROUP])

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['fernet_setup', 'fernet_rotate',
                                            'credential_setup',
                                            'credential_rotate'])
    args = parser.parse_args()

    is_credential = args.command.startswith('credential')

    SECRET_NAME = ('keystone-credential-keys' if is_credential else
                   'keystone-fernet-keys')

    read_kube_config()
    secret = get_secret_definition(SECRET_NAME)
    if not secret:
        if args.command in ('credential_setup', 'fernet_setup'):
            secret = create_secret(SECRET_NAME)
            if not secret:
                sys.exit(1)
        else:
            # A rotate has nothing to rotate if setup never ran. Creating an empty
            # Secret here would silently turn that into a no-op.
            LOG.error("Secret '%s' does not exist.", SECRET_NAME)
            sys.exit(1)

    if args.command in ('fernet_rotate', 'credential_rotate'):
        LOG.info("Copying existing %s keys from secret '%s' to %s.",
                 'credential' if is_credential else 'fernet', SECRET_NAME,
                 FERNET_DIR)
        write_to_files(secret['data'])

    if args.command in ('credential_setup', 'fernet_setup'):
        if secret.get('data', False):
            LOG.info('Keys already exist, skipping setup...')
            sys.exit(0)

    execute_command(args.command)

    LOG.info("Updating data for '%s' secret.", SECRET_NAME)
    updated_keys = get_keys_data()
    secret['data'] = updated_keys
    if not update_secret(SECRET_NAME, secret):
        sys.exit(1)
    LOG.info("%s fernet keys have been placed to secret '%s'",
             len(updated_keys), SECRET_NAME)
    LOG.debug("Placed keys: %s", updated_keys)
    LOG.info("%s keys %s has been completed",
             "Credential" if is_credential else 'Fernet',
             "rotation" if args.command.endswith('_rotate') else "generation")

    if args.command == 'credential_rotate':
        # `credential_rotate` needs doing `credential_migrate` as well once all
        # of the nodes have the new keys. So we'll sleep configurable amount of
        # time to make sure k8s reloads the secrets in all pods and then
        # execute `credential_migrate`.

        migrate_wait = int(os.getenv('KEYSTONE_CREDENTIAL_MIGRATE_WAIT', "60"))
        LOG.info("Waiting %d seconds to execute `credential_migrate`.",
                 migrate_wait)
        time.sleep(migrate_wait)

        execute_command('credential_migrate')

if __name__ == "__main__":
    main()
