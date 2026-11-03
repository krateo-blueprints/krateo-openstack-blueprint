#!/usr/bin/env python3

"""
Mariadb controller

The script is responsible for set mariadb_role: primary to first
active pod in mariadb deployment.

Env variables:
MARIADB_CONTROLLER_DEBUG: Flag to enable debug when set to 1.
MARIADB_CONTROLLER_CHECK_PODS_DELAY: The delay between check pod attempts.
MARIADB_CONTROLLER_PYKUBE_REQUEST_TIMEOUT: The timeout for kubernetes http session
MARIADB_CONTROLLER_PODS_NAMESPACE: The namespace to look for mariadb pods.
MARIADB_MASTER_SERVICE_NAME: The name of master service for mariadb.
MARIADB_CONTROLLER_HEARTBEAT_PATH: The file each completed reconcile pass reports through,
    read by the container's readiness probe.

Changelog:
0.1.0: Initial varsion
0.2.0: Report every completed reconcile pass to a heartbeat file, so the container has
    something to be unready about (issue #110).
"""


import logging
import os
import sys
import time

import pykube

MARIADB_CONTROLLER_DEBUG = os.getenv("MARIADB_CONTROLLER_DEBUG")
MARIADB_CONTROLLER_CHECK_PODS_DELAY = int(
    os.getenv("MARIADB_CONTROLLER_CHECK_PODS_DELAY", 10)
)
MARIADB_CONTROLLER_PYKUBE_REQUEST_TIMEOUT = int(
    os.getenv("MARIADB_CONTROLLER_PYKUBE_REQUEST_TIMEOUT", 60)
)
MARIADB_CONTROLLER_PODS_NAMESPACE = os.getenv(
    "MARIADB_CONTROLLER_PODS_NAMESPACE", "openstack"
)
MARIADB_MASTER_SERVICE_NAME = os.getenv(
    "MARIADB_MASTER_SERVICE_NAME", "mariadb"
)
MARIADB_CONTROLLER_HEARTBEAT_PATH = os.getenv(
    "MARIADB_CONTROLLER_HEARTBEAT_PATH", "/tmp/mariadb-controller-heartbeat"
)

log_level = "DEBUG" if MARIADB_CONTROLLER_DEBUG == "1" else "INFO"
logging.basicConfig(
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("mariadb-controller")

LOG.setLevel(log_level)


def login():
    config = pykube.KubeConfig.from_env()
    client = pykube.HTTPClient(
        config=config, timeout=MARIADB_CONTROLLER_PYKUBE_REQUEST_TIMEOUT
    )
    LOG.info(f"Created k8s api client from context {config.current_context}")
    return client


api = login()


def resource_list(klass, selector, namespace=None):
    return klass.objects(api).filter(namespace=namespace, selector=selector)


def get_mariadb_pods():
    sorted_pods = sorted(
        resource_list(
            pykube.Pod,
            {"application": "mariadb", "component": "server"},
            MARIADB_CONTROLLER_PODS_NAMESPACE,
        ).iterator(),
        key=lambda i: i.name,
    )
    return sorted_pods


def get_mariadb_master_service(namespace):
    return pykube.Service.objects(api).filter(namespace=namespace).get(name=MARIADB_MASTER_SERVICE_NAME)


def link_master_service(pod):
    svc = get_mariadb_master_service(MARIADB_CONTROLLER_PODS_NAMESPACE)
    svc.reload()
    if svc.obj['spec']['selector'].get('statefulset.kubernetes.io/pod-name') == pod.name:
        LOG.debug(f"Nothing to do, master service points to {pod.name}")
    else:
        svc.obj['spec']['selector']['statefulset.kubernetes.io/pod-name'] = pod.name
        svc.update()
        LOG.info(f"Link master service with {pod.name}")


def is_ready(pod):
    if pod.ready and "deletionTimestamp" not in pod.metadata:
        return True


def write_heartbeat(pods_seen, primary):
    """Report a completed reconcile pass, for the container's readiness probe to read.

    NOTE(krateo): this file is the only thing that tells a working controller from a wedged
    one. Nothing else in the pod does: the loop below is the whole program, every call it
    makes is to the kubernetes API, and a loop stuck in one of those calls keeps the process
    alive, the container Running and the pod Ready while it reconciles nothing. What it stops
    reconciling is the selector of the master service -- the `mariadb` Service every OpenStack
    service in the namespace resolves to -- which the chart renders pointing at ALL server
    pods and which this script narrows to one primary. Wedged, the selector keeps whatever it
    last held: broad on a fresh install, so writes land on several Galera primaries at once,
    or pinned to a pod that a failover has since replaced, so every service gets connection
    refused. Neither shows up anywhere except in the services that then break, which is the
    shape issue #110 is about.

    The file carries the DEADLINE for the next pass rather than the time this one finished,
    so the timing lives in one place: the loop knows its own delay and its own request
    timeout, and the probe only has to compare a number with the clock. A pass issues one
    list of the server pods, one reload per pod, and a get, a reload and an update of the
    master service -- pods_seen + 4 calls, each bounded by the pykube timeout -- and then
    sleeps for the delay, so that sum is what a slow but working pass is allowed to take. A
    tighter budget would make the probe fail on a slow apiserver, which is not this
    container's fault and not repaired by reporting it.

    Written to a temporary name and renamed, because the probe may read it at any moment and
    a half-written line is indistinguishable from a wedge.

    A write that fails is logged and does not stop the loop. The probe goes red, which is
    honest -- the controller can no longer report its state -- while the master service keeps
    being maintained, which is what actually matters to every service downstream.
    """
    deadline = int(time.time()) + MARIADB_CONTROLLER_CHECK_PODS_DELAY + (
        (pods_seen + 4) * MARIADB_CONTROLLER_PYKUBE_REQUEST_TIMEOUT
    )
    tmp_path = f"{MARIADB_CONTROLLER_HEARTBEAT_PATH}.tmp"
    try:
        with open(tmp_path, "w") as heartbeat:
            heartbeat.write(f"{deadline} {pods_seen} {primary or '-'}\n")
        os.replace(tmp_path, MARIADB_CONTROLLER_HEARTBEAT_PATH)
    except OSError as e:
        LOG.error(
            f"Could not write heartbeat to {MARIADB_CONTROLLER_HEARTBEAT_PATH}: {e}. "
            f"The master service is still being maintained, but readiness cannot see it."
        )


def main():
    while True:
        pods = get_mariadb_pods()
        primary = None
        for pod in pods:
            pod.reload()
            if is_ready(pod):
                link_master_service(pod)
                primary = pod.name
                break
        write_heartbeat(len(pods), primary)
        LOG.debug(f"Sleeping for {MARIADB_CONTROLLER_CHECK_PODS_DELAY}")
        time.sleep(MARIADB_CONTROLLER_CHECK_PODS_DELAY)


main()
