#!/usr/bin/env python3
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
"""Report whether this gnocchi process can actually do its job.

Gnocchi fails more quietly than anything else in this repository, by construction. Both of
these are in the code shipped in the Kolla images this chart installs (gnocchi 4.7.0):

  * gnocchi/cli/metricd.py, MetricProcessBase.run(), wraps every pass in
    `except Exception: LOG.error(...)` and then loops again, forever. A metricd that cannot
    reach its indexer, its measure store or its coordinator logs and keeps running. Measures
    are accepted and never aggregated; nothing restarts, nothing goes unready, and no API
    call ever returns an error.
  * gnocchi/statsd.py, Stats.flush(), wraps every individual measure in
    `except Exception as e: LOG.error("Unable to add measure %s: %s", ...)`. A statsd whose
    measure store is unreachable keeps accepting UDP datagrams and discards every one. UDP
    sends no reply, so the sender cannot tell either.

"The process is alive" therefore carries almost no information here -- and for metricd and
statsd it was the only thing being checked, because neither had any probe at all.

This checks the things that can be false while the process is alive. Every check is a real
round trip to the real backend, and each is requested explicitly so that a container asks
only for what it actually uses:

  --indexer       A SELECT against gnocchi's own archive_policy table over the configured
                  indexer URL. Proves reachability, credentials, AND that db-sync has created
                  the schema; a bare connect proves neither of the last two.
  --storage       Loads the configured incoming driver and reads gnocchi's `gnocchi-config`
                  sack configuration through it -- the same object gnocchi/incoming/*.py
                  _get_storage_sacks() reads, written by gnocchi-upgrade. The read proves the
                  measure store is reachable, authenticated, readable and initialised. If it
                  fails, metricd cannot locate a single sack to process.
  --coordination  Starts a tooz coordinator against coordination_url and heartbeats once.
                  metricd's MetricProcessBase._configure() cannot get past it (and retries
                  forever without crashing), and the API's GnocchiHook.on_route() builds one
                  for EVERY request, so a dead coordinator is a dead gnocchi that looks fine.
  --http URL      Fetches URL and requires 200.

That last one is the whole point of the API check. The API is served by `uwsgi --http`, whose
HTTP router is not a worker: the router accepts the TCP connection itself. A `tcpSocket` probe
on 8041 -- which is what gnocchi-api had -- therefore succeeds while every worker is wedged,
exactly the way a liveness probe on uwsgi's own stats port 1717 does for heat, neutron and
placement. Measured in this image: with both uwsgi workers SIGSTOPped, a TCP connect to 8041
still succeeds while GET /healthcheck times out.

The storage check deliberately goes through gnocchi's own option registry and its own driver
classes rather than reading gnocchi.conf by hand. An earlier version of this script parsed the
file with configparser and implemented one branch per driver -- which worked for the ceph pool
the chart configured at the time, and would have failed every probe forever the moment the
chart moved to swift. Asking gnocchi to load its own driver means ceph, swift, file, s3 and
redis all work, with gnocchi's defaults, gnocchi's types and gnocchi's ${storage.*} fallback
for an absent [incoming] section, and nothing here to keep in step with it. The cost is
importing gnocchi.opts, measured at ~1s in this image.

Exits 0 only if every requested check passed. Exits non-zero with a message the kubelet
records on the pod if any failed -- or if no check was requested at all, because a probe that
measures nothing must never report Ready.
"""

import argparse
import re
import signal
import socket
import sys
import urllib.request
from urllib import parse as urlparse

CONFIG_FILE = "/etc/gnocchi/gnocchi.conf"

# gnocchi/incoming/__init__.py: IncomingDriver.CFG_PREFIX. Named here only for the messages;
# the read itself goes through the driver's own NUM_SACKS.
SACKS_OBJECT = "gnocchi-config"

# The --http check is a loopback request to a process in this same container, so it has no
# business taking seconds. It gets its own small budget rather than the whole --deadline, for a
# reason that was measured: with both uwsgi workers SIGSTOPped, giving the fetch the full
# deadline meant the overall alarm fired first and the probe reported only "checks did not
# finish within 25s", which does not say what was wrong. With this it reports that
# /healthcheck did not answer, which is the actual diagnosis -- and the rest of the deadline
# is left for the backend checks that follow.
HTTP_TIMEOUT = 5

_CREDENTIALS = re.compile(r"//[^/@\s]*:[^/@\s]*@")


def redact(text):
    """Strip user:password out of any URL before it reaches a kubelet event."""
    return _CREDENTIALS.sub("//<redacted>@", str(text))


def describe(exc):
    """Name the exception as well as its text.

    Several of these libraries stringify poorly: a tooz coordination failure against an
    unreachable memcached renders as the bare text "-2", which tells an operator reading
    `kubectl describe pod` nothing at all. The class name is the part that identifies it.
    """
    return "%s: %s" % (type(exc).__name__, redact(exc))


def load_config(path, deadline):
    """Read gnocchi.conf through gnocchi's own option registry.

    Registering opts.list_opts() is what gnocchi/service.py prepare_service() does, minus the
    logging and daiquiri setup a probe has no use for. It is also why this script needs no
    per-driver knowledge: conf.incoming carries every driver's options, with gnocchi's
    defaults, gnocchi's types, and the ${storage.<name>} default that makes an absent
    [incoming] section mean "incoming is whatever storage is".
    """
    try:
        from oslo_config import cfg
        from gnocchi import opts
    except ImportError as exc:
        sys.exit(
            "gnocchi probe: cannot import gnocchi's configuration machinery, so nothing was "
            "checked: %s" % exc
        )
    conf = cfg.ConfigOpts()
    for group, options in opts.list_opts():
        conf.register_opts(list(options), group=None if group == "DEFAULT" else group)
    try:
        conf([], project="gnocchi", default_config_files=[path],
             validate_default_values=True)
    except Exception as exc:
        sys.exit("gnocchi probe: cannot read %s: %s" % (path, describe(exc)))

    # A backend library that blocks inside C -- librados does -- cannot be interrupted by this
    # process's SIGALRM, because the handler does not run until the call returns. Capping every
    # timeout the incoming group declares makes the library itself give up inside the probe's
    # budget. Driver-agnostic on purpose: it caps whatever timeouts the configured driver has,
    # without naming any of them. Best effort -- a probe must not fail because it could not
    # tighten an option.
    for group, options in opts.list_opts():
        if group != "incoming":
            continue
        for opt in options:
            if not opt.name.endswith("_timeout"):
                continue
            try:
                current = getattr(conf.incoming, opt.name)
                if current is None or int(float(current)) <= deadline:
                    continue
                tightened = str(deadline) if isinstance(current, str) else deadline
                conf.set_override(opt.name, tightened, group="incoming")
            except Exception:
                continue
    return conf


def coordination_url(conf):
    """The coordination URL gnocchi would use, including its fallback.

    gnocchi/service.py prepare_service() fills an unset coordination_url from the redis URL if
    either store is redis, and otherwise from the indexer URL with any "+driver" suffix
    stripped off the scheme. Without reproducing that, a config which legitimately leaves it
    unset would be reported here as "not configured".
    """
    if conf.coordination_url:
        return conf.coordination_url
    if conf.storage.driver == "redis":
        return conf.storage.redis_url
    if conf.incoming.driver == "redis":
        return conf.incoming.redis_url
    if not conf.indexer.url:
        return None
    parsed = list(urlparse.urlparse(conf.indexer.url))
    parsed[0] = parsed[0].partition("+")[0]
    return urlparse.urlunparse(parsed)


def check_http(url, timeout):
    """A real request, which only a uwsgi worker can answer."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            status = response.getcode()
            response.read()
    except Exception as exc:
        sys.exit(
            "gnocchi probe: %s did not answer: %s.\n"
            "  The API is served by `uwsgi --http`, whose HTTP router accepts connections "
            "even when no worker can run Python; fetching a response is what tells the two "
            "apart." % (url, describe(exc))
        )
    if status != 200:
        sys.exit(
            "gnocchi probe: %s answered %s, not 200 -- the WSGI application is not serving."
            % (url, status)
        )
    return "http %s" % url


def check_indexer(conf):
    url = conf.indexer.url
    if not url:
        sys.exit(
            "gnocchi probe: [indexer] url is not set, so no metric metadata can be read or "
            "written at all."
        )
    try:
        import sqlalchemy
    except ImportError as exc:
        sys.exit(
            "gnocchi probe: cannot import sqlalchemy, so the indexer was never checked: %s"
            % exc
        )
    # Through sqlalchemy and the configured URL, so this is the same query whether the indexer
    # is postgresql, mysql or anything else gnocchi supports.
    engine = sqlalchemy.create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("SELECT 1 FROM archive_policy LIMIT 1"))
    except Exception as exc:
        sys.exit(
            "gnocchi probe: indexer %s is unusable: %s.\n"
            "  archive_policy is created by gnocchi-upgrade, so this also fails when the "
            "database is reachable but db-sync has not run." % (redact(url), describe(exc))
        )
    finally:
        try:
            engine.dispose()
        except Exception:
            pass
    return "indexer %s" % redact(url)


def check_storage(conf):
    try:
        from gnocchi import incoming  # noqa: F401 -- registers the stevedore namespace
        from gnocchi import utils as gnocchi_utils
    except ImportError as exc:
        sys.exit(
            "gnocchi probe: cannot import gnocchi's incoming drivers, so the measure store "
            "was never checked: %s" % exc
        )
    driver_name = conf.incoming.driver

    # get_driver_class and then the class, NOT gnocchi.incoming.get_driver(). get_driver is
    # wrapped in utils.retry_on_exception_and_log, which is a tenacity.Retrying with no stop
    # condition: it retries forever. That is right for a daemon and fatal for a probe, which
    # would sit there until its deadline instead of reporting what is wrong.
    try:
        driver_class = gnocchi_utils.get_driver_class("gnocchi.incoming", conf.incoming)
    except Exception as exc:
        sys.exit(
            "gnocchi probe: gnocchi has no incoming driver named %r: %s"
            % (driver_name, describe(exc))
        )

    driver = None
    try:
        driver = driver_class(conf.incoming, conf.metricd.greedy)
    except Exception as exc:
        sys.exit(
            "gnocchi probe: cannot open the %r measure store (%s): %s.\n"
            "  Nothing can be aggregated or stored without it."
            % (driver_name, driver_class.__name__, describe(exc))
        )
    try:
        # NUM_SACKS reads the CFG_PREFIX object through the driver and raises
        # SackDetectionError if it cannot. One real read, whatever the backend is.
        sacks = driver.NUM_SACKS
    except Exception as exc:
        sys.exit(
            "gnocchi probe: cannot read %r from the %r measure store: %s.\n"
            "  gnocchi-upgrade writes that object; without it metricd cannot locate a single "
            "sack." % (SACKS_OBJECT, driver_name, describe(exc))
        )
    finally:
        try:
            driver.stop()
        except Exception:
            pass
    if sacks < 1:
        sys.exit(
            "gnocchi probe: the %r measure store reports %r sacks, so no measure can be "
            "routed to a sack." % (driver_name, sacks)
        )
    return "%s measure store (%d sacks)" % (driver_name, sacks)


def check_coordination(conf):
    url = coordination_url(conf)
    if not url:
        sys.exit(
            "gnocchi probe: no coordination_url is configured and none can be derived. "
            "metricd's MetricProcessBase._configure retries forever without one, processing "
            "nothing."
        )
    try:
        from tooz import coordination
    except ImportError as exc:
        sys.exit(
            "gnocchi probe: cannot import tooz, so coordination was never checked: %s" % exc
        )
    # Derived from the hostname so that repeated probes reuse one member id instead of adding
    # a fresh one every run. It never joins gnocchi-processing, so it cannot disturb the sack
    # partitioning metricd depends on.
    member_id = ("gnocchi-probe-%s" % socket.gethostname()).encode()
    coordinator = None
    try:
        coordinator = coordination.get_coordinator(url, member_id)
        coordinator.start(start_heart=False)
        # start() alone can be satisfied by a driver that connects lazily. A heartbeat is a
        # round trip, which is not.
        coordinator.heartbeat()
    except Exception as exc:
        sys.exit(
            "gnocchi probe: coordination backend %s is unusable: %s.\n"
            "  metricd cannot claim a sack and the API builds a coordinator for every "
            "request, so gnocchi does nothing at all without it."
            % (redact(url), describe(exc))
        )
    finally:
        if coordinator is not None:
            try:
                coordinator.stop()
            except Exception:
                pass
    return "coordination %s" % redact(url)


def arm_deadline(seconds):
    """Make the probe fail itself rather than hang. A probe that waits reports nothing."""

    def expired(signum, frame):
        sys.exit(
            "gnocchi probe: checks did not finish within %ds, which is treated as a failure."
            % seconds
        )

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check that this gnocchi process can reach what it needs to work."
    )
    parser.add_argument("--config-file", default=CONFIG_FILE)
    parser.add_argument(
        "--deadline",
        type=int,
        default=8,
        help="seconds after which the probe fails itself; keep below the probe timeoutSeconds",
    )
    parser.add_argument("--http", metavar="URL", help="fetch URL and require 200")
    parser.add_argument("--indexer", action="store_true")
    parser.add_argument("--storage", action="store_true")
    parser.add_argument("--coordination", action="store_true")
    args = parser.parse_args(argv)

    if not (args.http or args.indexer or args.storage or args.coordination):
        # The very failure this script exists to prevent, one level up: a probe that measures
        # nothing and succeeds.
        sys.exit(
            "gnocchi probe: no check was requested, so this probe would report Ready without "
            "measuring anything. Pass at least one of --http, --indexer, --storage, "
            "--coordination."
        )
    if args.deadline < 1:
        sys.exit("gnocchi probe: --deadline must be at least 1 second")

    arm_deadline(args.deadline)

    passed = []
    # --http first, and before the config is even read: it is the cheapest check and the one
    # whose failure is most specific.
    if args.http:
        passed.append(check_http(args.http, min(HTTP_TIMEOUT, args.deadline)))

    if args.indexer or args.storage or args.coordination:
        conf = load_config(args.config_file, args.deadline)
        if args.indexer:
            passed.append(check_indexer(conf))
        if args.storage:
            passed.append(check_storage(conf))
        if args.coordination:
            passed.append(check_coordination(conf))

    signal.alarm(0)
    print("OK: " + "; ".join(passed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
