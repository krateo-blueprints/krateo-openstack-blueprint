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

{{/*
abstract: |
  A readiness probe for an OpenStack worker that has no HTTP endpoint (#231, #151). It is
  OpenStack-Helm's own nova/neutron health-probe.py made generic: the same RPC ping, with the
  service's topic, exchange and server name passed as arguments instead of written into one copy
  of the script per chart. Contains no template directives: the rendered text is the Python.
usage: |
  {{ include "helm-toolkit.scripts.health_probe" . }}
*/}}

{{- define "helm-toolkit.scripts.health_probe" }}
#!/usr/bin/env python
"""Readiness probe for an OpenStack worker with no HTTP endpoint (#231, #151).

Derived from OpenStack-Helm's nova and neutron health-probe.py (the probes on nova-conductor,
nova-scheduler, nova-compute and the neutron agents). Like them it proves a worker serves by asking
it something over its own message queue; unlike them it is one script for every chart, told the
topic, exchange and server name of the program it probes, which were read from each program's
stable/2026.1 source (hack/readiness-probe-exceptions.yaml says where).

It finds the program in /proc -- its processes in this container's mount namespace, so a hostPID
pod never judges another pod's process -- and, for the RabbitMQ modes, reads the configuration the
program was started with from the program's own command line, so the probe cannot read a different
config than the process it judges. Then, by mode:

  --rpc-topic T   an oslo.messaging RPC server. Its process must hold an ESTABLISHED connection to
                  a RabbitMQ port of its transport_url, and an RPC call to Target(topic=T,
                  server=<its server name>) on <exchange> must be answered by an RPC server. The
                  method does not exist, so the answer is the dispatcher's NoSuchMethod or
                  UnsupportedVersion: proof that a process consumed that server queue, which a
                  dead, wedged or disconnected one does not. The `_ignore_errors` suffix makes
                  oslo.messaging log NoSuchMethod at DEBUG (neutron's probe uses the same name),
                  and rpc_ping_enabled in the service's config makes every server answer
                  NoSuchMethod rather than UnsupportedVersion, which it logs at ERROR.
  --broker        a notification listener. Its process must hold an ESTABLISHED connection to a
                  RabbitMQ port of its transport_url: a listener without one consumes nothing.
  --listen L      a server on its own socket: its process must hold the listening socket L,
                  tcp:<port>, udp:<port> or unix:<path>. Attributed by socket inode, so on
                  hostNetwork another process's socket on the same port does not count.

Readiness only, never liveness: a failed answer cannot tell a wedged process from an unreachable
broker, and a liveness probe on it restarts every replica of every worker during a RabbitMQ outage
and repairs nothing (#146). So every failure exits 1 -- the probe reports what it sees -- and
nothing is restarted because of it.

Exit 0 only on positive proof, 1 otherwise, with the reason on stderr (the pod's events).
"""

import argparse
import os
import signal
import socket
import sys

# Exit before the kubelet's timeoutSeconds, so a probe stuck on a dead broker never piles up behind
# the next one (OpenStack-Helm's scripts keep a pidfile for the same reason).
DEADLINE = int(os.getenv("PROBE_DEADLINE", "45"))
RPC_TIMEOUT = int(os.getenv("RPC_PROBE_TIMEOUT", "20"))
RPC_RETRIES = int(os.getenv("RPC_PROBE_RETRIES", "1"))
PROBE_METHOD = "pod_health_probe_method_ignore_errors"
REACHED = ("NoSuchMethod", "UnsupportedVersion")
SELF = os.path.basename(sys.argv[0]) or "worker-health-probe.py"
TCP_STATES = {"established": "01", "listen": "0A"}
UDP_BOUND = "07"
SO_ACCEPTCON = 0x10000


def not_ready(msg):
    sys.stderr.write("not ready: %s\n" % msg)
    sys.exit(1)


def mount_ns(pid):
    try:
        return os.readlink("/proc/%s/ns/mnt" % pid)
    except OSError:
        return None


def find_program(name):
    """[(pid, argv after the program)] for every process of this container running `name`.

    Matched on the basename of any argv element, so `python /usr/bin/heat-engine ...` and an
    emulated `/run/rosetta/rosetta /usr/bin/python3 ...` match as well as a direct exec. /proc's
    cmdline, not /proc/<pid>/exe: under an emulating runtime exe is the emulator (#96). Only
    processes in this probe's mount namespace count: the probe runs in the program's container,
    and a hostPID pod sees every process on the node.
    """
    me = os.getpid()
    ns = mount_ns("self")
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        if ns is not None and mount_ns(pid) != ns:
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if any(os.path.basename(a) == SELF for a in argv):
            continue  # this probe, or an earlier one still running
        for i, a in enumerate(argv):
            if os.path.basename(a) == name:
                found.append((int(pid), argv[i + 1:]))
                break
    return found


def config_args(argv):
    """The --config-file/--config-dir arguments, in order, as the program was given them."""
    out = []
    i = 0
    while i < len(argv):
        a = argv[i]
        for flag in ("--config-file", "--config-dir"):
            if a == flag and i + 1 < len(argv):
                out += [flag, argv[i + 1]]
                i += 1
            elif a.startswith(flag + "="):
                out += [flag, a.split("=", 1)[1]]
        i += 1
    return out


def socket_inodes(pids):
    inodes = set()
    for pid in pids:
        try:
            fds = os.listdir("/proc/%d/fd" % pid)
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink("/proc/%d/fd/%s" % (pid, fd))
            except OSError:
                continue
            if link.startswith("socket:["):
                inodes.add(link[8:-1])
    return inodes


def _rows(tables):
    for table in tables:
        try:
            with open(table) as f:
                for row in f.read().splitlines()[1:]:
                    yield row.split()
        except OSError:
            continue


def _port(addr):
    return int(addr.rsplit(":", 1)[1], 16)


def established_to(pids, ports):
    """True if one of these processes holds an ESTABLISHED TCP connection to one of `ports`."""
    inodes = socket_inodes(pids)
    return any(len(c) > 9 and c[3] == TCP_STATES["established"] and _port(c[2]) in ports and c[9] in inodes
               for c in _rows(("/proc/net/tcp", "/proc/net/tcp6")))


def listening(pids, spec):
    """True if one of these processes holds the listening socket `spec` (tcp:P, udp:P or unix:PATH)."""
    kind, _, where = spec.partition(":")
    inodes = socket_inodes(pids)
    if kind == "tcp":
        return any(len(c) > 9 and c[3] == TCP_STATES["listen"] and _port(c[1]) == int(where) and c[9] in inodes
                   for c in _rows(("/proc/net/tcp", "/proc/net/tcp6")))
    if kind == "udp":
        return any(len(c) > 9 and c[3] == UDP_BOUND and _port(c[1]) == int(where) and c[9] in inodes
                   for c in _rows(("/proc/net/udp", "/proc/net/udp6")))
    if kind == "unix":
        # Num RefCount Protocol Flags Type St Inode Path
        return any(len(c) > 7 and int(c[3], 16) & SO_ACCEPTCON and c[6] in inodes and c[7] == where
                   for c in _rows(("/proc/net/unix",)))
    raise ValueError("--listen %s: want tcp:<port>, udp:<port> or unix:<path>" % spec)


def reached(e):
    """True if exception `e` is an RPC server's answer that it has no such method or version.

    Over RabbitMQ the server's NoSuchMethod/UnsupportedVersion arrives as RemoteError(exc_type=...),
    because oslo_messaging.rpc.dispatcher is not in the client's allowed_remote_exmods; a driver
    that does not serialize (the in-process fake one) raises the dispatcher's own class. Both are
    the same answer. (OpenStack-Helm's scripts match only the first and let the second through
    their catch-all, which exits 0 for any exception at all.)
    """
    import oslo_messaging
    from oslo_messaging.rpc import dispatcher
    if isinstance(e, (dispatcher.NoSuchMethod, dispatcher.UnsupportedVersion)):
        return True
    return isinstance(e, oslo_messaging.RemoteError) and e.exc_type in REACHED


def rpc_ping(transport, topic, server):
    """None if an RPC server consumed Target(topic, server) on this transport, else why not.

    server=None addresses the topic queue, which any server of the topic consumes.
    """
    import oslo_messaging
    where = topic if server is None else "%s.%s" % (topic, server)
    try:
        target = oslo_messaging.Target(topic=topic, server=server)
        client = oslo_messaging.get_rpc_client(transport, target, timeout=RPC_TIMEOUT, retry=RPC_RETRIES)
        client.call({}, PROBE_METHOD)
    except oslo_messaging.MessagingTimeout:
        return "nothing consumed %s within %ds" % (where, RPC_TIMEOUT)
    except Exception as e:  # only the server's own "no such method" answer is proof of service
        if reached(e):
            return None
        return "RPC call to %s failed: %s: %s" % (where, type(e).__name__, (str(e).splitlines() or [""])[0])
    # A plain reply means a real method of this name exists; it was still consumed and answered.
    return None


def server_names(conf, args):
    """The server name(s) the program's RPC server listens under, as the program computes them."""
    from oslo_config import cfg
    if args.rpc_any_server:
        return [None]
    group, _, opt = args.rpc_server_opt.rpartition(".")
    group = None if group in ("", "DEFAULT") else group
    section = conf if group is None else getattr(conf, group)
    base = getattr(section, opt)
    if not base:
        base = {"@hostname": socket.gethostname(), "@fqdn": socket.getfqdn()}.get(args.rpc_server_default,
                                                                                  args.rpc_server_default)
    if not args.rpc_backends:
        return [base]
    bgroup, _, bopt = args.rpc_backends.rpartition(".")
    bsection = conf if bgroup in ("", "DEFAULT") else getattr(conf, bgroup)
    backends = [b.strip() for b in (getattr(bsection, bopt) or []) if b.strip()]
    if not backends:
        not_ready("%s names no backend, so no %s server runs" % (args.rpc_backends, args.rpc_topic))
    names = []
    for b in backends:
        host = base
        if args.rpc_backend_host_opt:
            conf.register_opt(cfg.StrOpt(args.rpc_backend_host_opt), group=b)
            host = getattr(getattr(conf, b), args.rpc_backend_host_opt) or base
        names.append("%s@%s" % (host, b))
    return names


def register(conf, args):
    from oslo_config import cfg
    group, _, opt = args.rpc_server_opt.rpartition(".")
    wanted = [(None if group in ("", "DEFAULT") else group, cfg.StrOpt(opt))]
    if args.rpc_backends:
        bgroup, _, bopt = args.rpc_backends.rpartition(".")
        wanted.append((None if bgroup in ("", "DEFAULT") else bgroup, cfg.ListOpt(bopt)))
    for g, o in wanted:
        try:
            conf.register_opt(o, group=g)
        except cfg.DuplicateOptError:
            pass


def deadline(*_):
    sys.stderr.write("not ready: probe did not finish in %ds\n" % DEADLINE)
    os._exit(1)


def parser():
    p = argparse.ArgumentParser()
    p.add_argument("--process", required=True, help="the program's executable name, e.g. heat-engine")
    p.add_argument("--rpc-topic", help="an RPC server: its topic")
    p.add_argument("--exchange", help="the control_exchange the program sets by default, e.g. heat")
    p.add_argument("--rpc-server-opt", default="DEFAULT.host", help="GROUP.OPTION naming the RPC server")
    p.add_argument("--rpc-server-default", default="@hostname",
                   help="the option's default in the program: a literal, @hostname or @fqdn")
    p.add_argument("--rpc-any-server", action="store_true",
                   help="the server name is random per start: address the topic, which any server consumes")
    p.add_argument("--rpc-backends", help="GROUP.OPTION listing backends: one server <name>@<backend> each")
    p.add_argument("--rpc-backend-host-opt", help="option in each backend's section that replaces <name>")
    p.add_argument("--broker", action="store_true", help="a listener: require only the broker connection")
    p.add_argument("--listen", action="append", default=[], help="tcp:<port>, udp:<port> or unix:<path>")
    return p


def main():
    p = parser()
    args = p.parse_args()
    if sum((bool(args.rpc_topic), args.broker, bool(args.listen))) != 1:
        p.error("exactly one of --rpc-topic, --broker or --listen is required")
    if args.rpc_topic and not args.exchange:
        p.error("--rpc-topic needs --exchange")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(DEADLINE)

    procs = find_program(args.process)
    if not procs:
        not_ready("no %s process is running in this container" % args.process)
    pids = [pid for pid, _ in procs]
    if args.listen:
        for spec in args.listen:
            if not listening(pids, spec):
                not_ready("%s holds no listening socket %s" % (args.process, spec))
        return

    conf_args = config_args(procs[0][1])
    if "--config-file" not in conf_args:
        not_ready("%s was started without --config-file, so its configuration cannot be read" % args.process)

    from oslo_config import cfg
    import oslo_messaging

    if args.exchange:
        oslo_messaging.set_transport_defaults(control_exchange=args.exchange)
    if args.rpc_topic:
        register(cfg.CONF, args)
    cfg.CONF(conf_args, project=None, default_config_files=[], default_config_dirs=[])

    try:
        url = oslo_messaging.TransportURL.parse(cfg.CONF)
    except Exception as e:
        not_ready("%s's transport_url does not parse: %s" % (args.process, e))
    if not url.hosts:
        not_ready("%s's configuration names no transport_url host, so it consumes nothing" % args.process)
    ports = {h.port or 5672 for h in url.hosts}
    if not established_to(pids, ports):
        not_ready("%s holds no ESTABLISHED connection to RabbitMQ (ports %s)" % (args.process, sorted(ports)))
    if args.broker:
        return

    transport = oslo_messaging.get_rpc_transport(cfg.CONF)
    try:
        reasons = [r for r in (rpc_ping(transport, args.rpc_topic, s) for s in server_names(cfg.CONF, args)) if r]
    finally:
        transport.cleanup()
    if reasons:
        not_ready("%s (exchange %s)" % ("; ".join(reasons), cfg.CONF.control_exchange))


if __name__ == "__main__":
    main()
    sys.exit(0)
{{- end }}
