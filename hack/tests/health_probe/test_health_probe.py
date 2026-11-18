"""The worker readiness probe (helm-toolkit.scripts.health_probe), run as rendered (#231, #151).

Loaded from the path in HEALTH_PROBE_SCRIPT, which hack/validate-health-probe.py writes from a
rendered ConfigMap: the text the interpreter in the pod runs, not the template.

  * rpc_ping against a real oslo.messaging RPC server on the in-process fake driver: answered by
    the server -> ready; no server, the right topic under another server name, or another topic
    -> not ready. Covers servers whose endpoints are all at a major version other than 1, which
    answer UnsupportedVersion, and the rpc_ping_enabled case.
  * reached() on the exception as it crosses RabbitMQ: serialized by the server and rebuilt by a
    client whose allowed_remote_exmods does not include the dispatcher, so it is a RemoteError.
  * container_processes, config_args and established_to against real processes and sockets in /proc;
    test_titled_processes.py starts them through the real launchers, titled and untitled.
"""
import importlib.util
import io
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

os.environ["RPC_PROBE_TIMEOUT"] = "2"
_spec = importlib.util.spec_from_file_location("health_probe", os.environ["HEALTH_PROBE_SCRIPT"])
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)

from oslo_config import cfg  # noqa: E402
import oslo_messaging as om  # noqa: E402
from oslo_messaging._drivers import common as rpc_common  # noqa: E402
from oslo_messaging.rpc import dispatcher  # noqa: E402

om.set_transport_defaults("gate")
cfg.CONF([], project="gate", default_config_files=[], default_config_dirs=[])


class Endpoint(object):
    def __init__(self, version):
        self.target = om.Target(version=version)

    def real_method(self, ctxt):
        return "x"


class RpcPing(unittest.TestCase):
    TOPIC = "gate.worker.control"

    def setUp(self):
        self.log = io.StringIO()
        self.handler = logging.StreamHandler(self.log)
        self.handler.setLevel(logging.ERROR)
        logging.getLogger("oslo_messaging").addHandler(self.handler)
        self.transport = om.get_rpc_transport(cfg.CONF, url="fake:///")
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.stop()
            self.server.wait()
        self.transport.cleanup()
        logging.getLogger("oslo_messaging").removeHandler(self.handler)
        cfg.CONF.clear_override("rpc_ping_enabled")

    def serve(self, topic, server, version, ping=False):
        if ping:
            cfg.CONF.set_override("rpc_ping_enabled", True)
        self.server = om.get_rpc_server(self.transport, om.Target(topic=topic, server=server), [Endpoint(version)],
                                        executor="threading")
        self.server.start()

    def test_major_1_server_answers(self):
        self.serve(self.TOPIC, "pod-0", "1.5")
        self.assertIsNone(hp.rpc_ping(self.transport, self.TOPIC, "pod-0"))
        self.assertNotIn("Exception during message handling", self.log.getvalue())

    def test_other_major_server_answers(self):
        self.serve(self.TOPIC, "pod-0", "3.2")
        self.assertIsNone(hp.rpc_ping(self.transport, self.TOPIC, "pod-0"))

    def test_rpc_ping_enabled_keeps_the_server_log_clean(self):
        # Without rpc_ping_enabled a server with no major-1 endpoint answers UnsupportedVersion,
        # which oslo.messaging logs at ERROR on every probe; the ping endpoint makes it NoSuchMethod.
        self.serve(self.TOPIC, "pod-0", "3.2", ping=True)
        self.assertIsNone(hp.rpc_ping(self.transport, self.TOPIC, "pod-0"))
        self.assertNotIn("Exception during message handling", self.log.getvalue())

    def test_topic_only_reaches_a_server_of_any_name(self):
        # magnum-conductor's server name is random per start: the probe addresses the topic.
        self.serve(self.TOPIC, "a1b2c3d4e5f6", "1.0")
        self.assertIsNone(hp.rpc_ping(self.transport, self.TOPIC, None))

    def test_topic_only_with_no_server_is_not_ready(self):
        self.assertIn("nothing consumed", hp.rpc_ping(self.transport, self.TOPIC, None))

    def test_no_server_is_not_ready(self):
        self.assertIn("nothing consumed", hp.rpc_ping(self.transport, self.TOPIC, "pod-0"))

    def test_another_server_name_is_not_ready(self):
        self.serve(self.TOPIC, "pod-1", "1.0")
        self.assertIn("nothing consumed", hp.rpc_ping(self.transport, self.TOPIC, "pod-0"))

    def test_another_topic_is_not_ready(self):
        self.serve("gate.other.control", "pod-0", "1.0")
        self.assertIn("nothing consumed", hp.rpc_ping(self.transport, self.TOPIC, "pod-0"))


class OverTheWire(unittest.TestCase):
    def roundtrip(self, exc):
        try:
            raise exc
        except Exception:
            wire = rpc_common.serialize_remote_exception(sys.exc_info())
        return rpc_common.deserialize_remote_exception(wire, [])

    def test_no_such_method_is_reached(self):
        back = self.roundtrip(dispatcher.NoSuchMethod(hp.PROBE_METHOD))
        self.assertIsInstance(back, om.RemoteError)
        self.assertTrue(hp.reached(back))

    def test_unsupported_version_is_reached(self):
        back = self.roundtrip(dispatcher.UnsupportedVersion("1.0", method=hp.PROBE_METHOD))
        self.assertIsInstance(back, om.RemoteError)
        self.assertTrue(hp.reached(back))

    def test_any_other_failure_is_not(self):
        self.assertFalse(hp.reached(self.roundtrip(ValueError("boom"))))
        self.assertFalse(hp.reached(om.MessageDeliveryFailure("broker gone")))


class ConfigArgs(unittest.TestCase):
    """The configuration comes from the probe's own arguments (the chart's list), in the order given."""

    def test_order_is_kept_across_flags(self):
        args = hp_args("--process", "svc", "--broker", "--config-file", "/etc/x/x.conf", "--config-dir", "/etc/x/x.conf.d",
                       "--config-file", "/etc/x/extra.conf")
        self.assertEqual(hp.config_args(args.config), ["--config-file", "/etc/x/x.conf", "--config-dir", "/etc/x/x.conf.d",
                                                       "--config-file", "/etc/x/extra.conf"])

    def test_dir_if_present(self):
        tmp = tempfile.mkdtemp()
        try:
            args = hp_args("--process", "svc", "--broker", "--config-file", "/etc/x/x.conf",
                           "--config-dir-if-present", tmp, "--config-dir-if-present", os.path.join(tmp, "absent"))
            self.assertEqual(hp.config_args(args.config), ["--config-file", "/etc/x/x.conf", "--config-dir", tmp])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ServerNames(unittest.TestCase):
    """The server names the probe addresses, computed from the program's config as the program does."""

    def names(self, conf_text, *argv):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "svc.conf")
            open(path, "w").write(conf_text)
            conf = cfg.ConfigOpts()
            args = hp_args("--process", "svc", "--rpc-topic", "t", "--exchange", "x", *argv)
            hp.register(conf, args)
            conf(["--config-file", path], project=None, default_config_files=[], default_config_dirs=[])
            return hp.server_names(conf, args)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_host_option_wins(self):
        self.assertEqual(self.names("[DEFAULT]\nhost = heat-engine\n"), ["heat-engine"])

    def test_unset_host_is_the_hostname(self):
        self.assertEqual(self.names("[DEFAULT]\n"), [socket.gethostname()])

    def test_unset_host_with_a_literal_default(self):
        self.assertEqual(self.names("[DEFAULT]\n", "--rpc-server-default", "0.0.0.0"), ["0.0.0.0"])

    def test_option_in_a_group(self):
        self.assertEqual(self.names("[engine]\nhost = e1\n", "--rpc-server-opt", "engine.host"), ["e1"])

    def test_any_server(self):
        self.assertEqual(self.names("[DEFAULT]\nhost = h\n", "--rpc-any-server"), [None])

    def test_one_server_per_backend(self):
        conf = "[DEFAULT]\nhost = cinder-volume-worker\nenabled_backends = rbd1, lvm\n[lvm]\nbackend_host = shared\n"
        self.assertEqual(self.names(conf, "--rpc-backends", "DEFAULT.enabled_backends",
                                    "--rpc-backend-host-opt", "backend_host"),
                         ["cinder-volume-worker@rbd1", "shared@lvm"])

    def test_no_backend_is_not_ready(self):
        with self.assertRaises(SystemExit) as e:
            self.names("[DEFAULT]\nhost = h\n", "--rpc-backends", "DEFAULT.enabled_backends")
        self.assertEqual(e.exception.code, 1)


def hp_args(*argv):
    return hp.parser().parse_args(list(argv))


@unittest.skipUnless(os.path.isdir("/proc/self/fd"), "needs /proc")
class Proc(unittest.TestCase):
    """A real process, started under a program name, holding a real TCP connection."""

    def setUp(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.tmp = tempfile.mkdtemp()
        self.prog = os.path.join(self.tmp, "gate-worker")
        with open(self.prog, "w") as f:
            f.write("import socket, sys, time\n"
                    "s = socket.create_connection(('127.0.0.1', int(sys.argv[1])))\n"
                    "print('up', flush=True)\n"
                    "time.sleep(60)\n")
        self.child = subprocess.Popen([sys.executable, self.prog, str(self.port), "--config-file", "/etc/gate.conf"],
                                      stdout=subprocess.PIPE)
        self.assertEqual(self.child.stdout.readline().strip(), b"up")
        self.conn, _ = self.listener.accept()

    def tearDown(self):
        self.child.kill()
        self.child.wait()
        self.conn.close()
        self.listener.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_container_processes_include_the_program_and_not_the_probe(self):
        # The probe's mount namespace: here, the test's. Identity by namespace, never by name.
        found = hp.container_processes()
        self.assertIn(self.child.pid, found)
        self.assertNotIn(os.getpid(), found)

    def test_an_earlier_probe_run_is_not_the_program(self):
        self.assertTrue(hp.is_probe(["python", "/tmp/" + hp.SELF, "--process", "x"]))
        self.assertFalse(hp.is_probe(["gate-worker: master process [/x/gate-worker --config-file /c]"]))

    def test_established_to_the_port(self):
        self.assertTrue(hp.established_to([self.child.pid], {self.port}))

    def test_not_established_to_another_port(self):
        self.assertFalse(hp.established_to([self.child.pid], {self.port + 1}))

    def test_another_process_socket_does_not_count(self):
        # /proc/net/tcp is the whole network namespace (the pod): a row from any process matches the
        # port. Only the program's own socket inodes may count.
        other = subprocess.Popen(["sleep", "30"])
        own = socket.create_connection(("127.0.0.1", self.port))
        try:
            self.assertTrue(hp.established_to([os.getpid()], {self.port}))
            self.assertFalse(hp.established_to([other.pid], {self.port}))
        finally:
            own.close()
            other.kill()
            other.wait()

    def test_listening_tcp_socket_is_attributed_to_its_owner(self):
        self.assertTrue(hp.listening([os.getpid()], "tcp:%d" % self.port))
        self.assertFalse(hp.listening([self.child.pid], "tcp:%d" % self.port))

    def test_bound_udp_socket(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.bind(("127.0.0.1", 0))
        try:
            port = u.getsockname()[1]
            self.assertTrue(hp.listening([os.getpid()], "udp:%d" % port))
            self.assertFalse(hp.listening([os.getpid()], "udp:%d" % (port + 1)))
        finally:
            u.close()

    def test_listening_unix_socket(self):
        path = os.path.join(self.tmp, "get.sock")
        u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        u.bind(path)
        try:
            self.assertFalse(hp.listening([os.getpid()], "unix:" + path))  # bound, not yet listening
            u.listen(1)
            self.assertTrue(hp.listening([os.getpid()], "unix:" + path))
            self.assertFalse(hp.listening([self.child.pid], "unix:" + path))
        finally:
            u.close()

    def test_closed_connection_is_not_established(self):
        self.child.kill()
        self.child.wait()
        time.sleep(0.2)
        self.assertFalse(hp.established_to([self.child.pid], {self.port}))


if __name__ == "__main__":
    unittest.main()
