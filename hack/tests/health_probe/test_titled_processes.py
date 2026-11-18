"""The worker readiness probe against processes as the 2026.1 images start them (#231; 0.3.74's defect B).

oslo.service's threading backend and cotyledon (used directly by aodh, ceilometer and cloudkitty) call setproctitle,
which rewrites a process's /proc cmdline into ONE title string -- `designate-central: master process [...]`,
`designate-central: ServiceWrapper worker(0)` -- and its environ with it. The probe used to find the program by an argv
element's basename and read its --config-file arguments from that argv, so on every titled service it found nothing:
14 workers on a GKE run of 0.3.74 never became Ready. The earlier tests started their process with plain argv, which is
why that passed CI. These start real processes through the real launchers, at the versions in the images
(oslo.service 4.5.1, cotyledon 2.2.0, setproctitle 1.3.7, eventlet 0.40.4; hack/gates.d/validate-health-probe.yaml):

  (a) oslo.service THREADING backend, workers=1   designate's shape       titled
  (b) oslo.service THREADING backend, workers=2                           titled
  (c) cotyledon ServiceManager used directly       aodh, ceilometer, cloudkitty   titled
  (d) oslo.service EVENTLET backend                blazar's, cinder's shape   untitled
  (e) a plain process                                                     untitled

Each holds a TCP connection to a local listener that the probe's configuration names as its RabbitMQ host, and the
rendered probe runs end to end in --broker mode with the configuration passed explicitly: ready; killed: not ready.

NATIVE LINUX ONLY, and checked rather than assumed: under an emulating runtime (Rosetta, so kind on Apple Silicon) the
title is applied but /proc cmdline is not rewritten, so the titled shape would silently not be measured. A test asserts
that (a)'s master really reads as one `<name>: master process [` element and FAILS otherwise -- it never skips.
GitHub's amd64 runners are native.

Mutants of the rendered probe, each of which must be reported as its own defect:
  M1 the program found by argv name again (as before)          -> (a), (b), (c) not ready
  M2 the configuration read from the program's argv (as before) -> (a), (b), (c) not ready
  M3 the mount-namespace filter dropped                        -> a process outside the probe's namespace counts.
     Needs a second mount namespace: `sudo -n unshare -m` where the runner allows it; otherwise a stub that gives the
     fixture's pid another namespace in-process, and the test SAYS it used the stub (never a silent skip).
The precondition itself is mutated too: with setproctitle made unimportable, (a)'s cmdline is plain and the
precondition check must report it.
"""
import importlib.util
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

PROBE = os.environ["HEALTH_PROBE_SCRIPT"]
NAME = "gate-titled-worker"

FIXTURE = r'''
import socket, sys, time
shape, port = sys.argv[1], int(sys.argv[2])


def hold():
    return socket.create_connection(("127.0.0.1", port))


if shape in ("threading", "threading2", "eventlet"):
    from oslo_service import backend
    backend.init_backend(backend.BackendType.EVENTLET if shape == "eventlet" else backend.BackendType.THREADING)
    from oslo_config import cfg
    from oslo_service import service

    class Svc(service.Service):
        def start(self):
            super().start()
            self.sock = hold()
            print("up", flush=True)

    conf = cfg.ConfigOpts()
    conf([], project="gate", default_config_files=[], default_config_dirs=[])
    service.launch(conf, Svc(), workers=2 if shape == "threading2" else 1).wait()
elif shape == "cotyledon":
    import cotyledon

    class Svc(cotyledon.Service):
        def __init__(self, worker_id):
            super().__init__(worker_id)
            self.sock = hold()
            print("up", flush=True)

        def run(self):
            while True:
                time.sleep(1)

    sm = cotyledon.ServiceManager()
    sm.add(Svc, workers=1)
    sm.run()
else:
    s = hold()
    print("up", flush=True)
    time.sleep(600)
'''

WORKERS = {"threading": 1, "threading2": 2, "cotyledon": 1, "eventlet": 1, "plain": 1}
TITLED = ("threading", "threading2", "cotyledon")


def cmdline(pid):
    with open("/proc/%d/cmdline" % pid, "rb") as f:
        return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]


def descendants(pid):
    """Every descendant of pid, from each process's parent in /proc/<pid>/stat (task/*/children needs a kernel
    option some kernels lack)."""
    parent = {}
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                parent[int(p)] = int(open("/proc/%s/stat" % p).read().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        for k, pp in parent.items():
            if pp == p:
                out.append(k)
                todo.append(k)
    return out


def title_problem(pid):
    """None if pid's cmdline is the cotyledon master title, else what it is instead."""
    argv = cmdline(pid)
    if len(argv) == 1 and argv[0].startswith(NAME + ": master process ["):
        return None
    return "cmdline is %r, not one `%s: master process [` element" % (argv, NAME)


class Fixture(object):
    def __init__(self, shape, tmp, env=None):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.prog = os.path.join(tmp, NAME)
        open(self.prog, "w").write(FIXTURE)
        self.conf = os.path.join(tmp, "gate.conf")
        open(self.conf, "w").write("[DEFAULT]\ntransport_url = rabbit://gate:gate@127.0.0.1:%d/\n" % self.port)
        self.child = subprocess.Popen([sys.executable, self.prog, shape, str(self.port), "--config-file", self.conf],
                                      stdout=subprocess.PIPE, start_new_session=True, env=env)
        self.conns = []
        self.listener.settimeout(30)
        for _ in range(WORKERS[shape]):
            line = self.child.stdout.readline().strip()
            if line != b"up":
                self.close()
                raise AssertionError("fixture %s did not come up (read %r)" % (shape, line))
            self.conns.append(self.listener.accept()[0])
        time.sleep(0.3)  # cotyledon titles the worker right after it starts

    def close(self):
        try:
            os.killpg(self.child.pid, signal.SIGKILL)
        except OSError:
            pass
        self.child.wait()
        for c in self.conns:
            c.close()
        self.listener.close()


def probe(script, fixture, *extra, prefix=()):
    r = subprocess.run([*prefix, sys.executable, script, "--process", NAME, "--broker", "--config-file", fixture.conf,
                        *extra], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stderr.strip()


def mutant(src, which):
    """The rendered probe with one change. Each replacement must apply exactly once."""
    old_find = '''
def _by_name(name):
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\\0") if a]
        except OSError:
            continue
        if is_probe(argv):
            continue
        for i, a in enumerate(argv):
            if os.path.basename(a) == name:
                found.append((int(pid), argv[i + 1:]))
                break
    return found


def _argv_config(procs):
    out, argv = [], (procs[0][1] if procs else [])
    for i, a in enumerate(argv):
        for flag in ("--config-file", "--config-dir"):
            if a == flag and i + 1 < len(argv):
                out += [flag, argv[i + 1]]
            elif a.startswith(flag + "="):
                out += [flag, a.split("=", 1)[1]]
    if "--config-file" not in out:
        not_ready("started without --config-file, so its configuration cannot be read")
    return out


def main():'''
    if which == "M1":
        reps = [("\ndef main():", old_find),
                ("    pids = container_processes()\n", "    pids = [p for p, _ in _by_name(args.process)]\n")]
    elif which == "M2":
        reps = [("\ndef main():", old_find),
                ("    conf_args = config_args(args.config)\n", "    conf_args = _argv_config(_by_name(args.process))\n")]
    elif which == "M3":
        reps = [("        if mount_ns(pid) != ns:\n            continue\n", "")]
    else:
        raise ValueError(which)
    for a, b in reps:
        if src.count(a) != 1:
            raise AssertionError("mutant %s: %r occurs %d times in the rendered probe" % (which, a, src.count(a)))
        src = src.replace(a, b)
    return src


@unittest.skipUnless(os.path.isdir("/proc/self/fd"), "needs /proc")
class TitledProcesses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import cotyledon, setproctitle  # noqa: F401  (pinned in the gate's setup; a missing one is a failure)
        from oslo_service import backend  # noqa: F401
        cls.tmp = tempfile.mkdtemp()
        cls.src = open(PROBE).read()
        cls.mutants = {}
        for m in ("M1", "M2", "M3"):
            p = os.path.join(cls.tmp, "worker-health-probe-%s.py" % m)
            open(p, "w").write(mutant(cls.src, m))
            cls.mutants[m] = p

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fixture(self, shape, env=None):
        d = tempfile.mkdtemp(dir=self.tmp)
        f = Fixture(shape, d, env=env)
        self.addCleanup(f.close)
        return f

    # -- the precondition: this host really rewrites cmdline -------------------------------------------------
    def test_precondition_cmdline_is_rewritten(self):
        f = self.fixture("threading")
        problem = title_problem(f.child.pid)
        if problem:
            self.fail("this host does not rewrite /proc cmdline (an emulating runtime such as Rosetta?): %s. The titled "
                      "shape the probe must handle was NOT measured; run on native Linux." % problem)
        workers = descendants(f.child.pid)
        self.assertTrue(workers, "the threading launcher started no worker")
        self.assertTrue(all(len(cmdline(w)) == 1 and cmdline(w)[0].startswith(NAME + ": ") for w in workers),
                        [cmdline(w) for w in workers])

    def test_precondition_check_reports_an_untitled_process(self):
        # The precondition mutated: setproctitle made unimportable, so cotyledon sets no title.
        shim = os.path.join(self.tmp, "nospt")
        os.makedirs(shim, exist_ok=True)
        open(os.path.join(shim, "setproctitle.py"), "w").write("raise ImportError('disabled for the gate')\n")
        env = dict(os.environ, PYTHONPATH=shim + os.pathsep + os.environ.get("PYTHONPATH", ""))
        f = self.fixture("threading", env=env)
        self.assertIsNotNone(title_problem(f.child.pid), "the precondition check passes on an untitled process")

    # -- every shape: ready while connected, not ready once gone ----------------------------------------------
    def check_shape(self, shape):
        f = self.fixture(shape)
        if shape in TITLED:
            self.assertIsNone(title_problem(f.child.pid))
        rc, err = probe(PROBE, f)
        self.assertEqual(rc, 0, "%s: not ready: %s" % (shape, err))
        f.close()
        rc, err = probe(PROBE, f)
        self.assertEqual(rc, 1, "%s: still ready after the program was killed" % shape)
        self.assertIn("ESTABLISHED", err)

    def test_a_threading_workers_1(self):
        self.check_shape("threading")

    def test_b_threading_workers_2(self):
        self.check_shape("threading2")

    def test_c_cotyledon_direct(self):
        self.check_shape("cotyledon")

    def test_d_eventlet_backend(self):
        self.check_shape("eventlet")

    def test_e_plain_process(self):
        self.check_shape("plain")

    def test_config_must_be_given(self):
        f = self.fixture("plain")
        r = subprocess.run([sys.executable, PROBE, "--process", NAME, "--broker"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--config-file", r.stderr)

    def test_config_dir_if_present(self):
        f = self.fixture("plain")
        rc, err = probe(PROBE, f, "--config-dir-if-present", os.path.join(self.tmp, "absent.conf.d"))
        self.assertEqual(rc, 0, err)
        rc, err = probe(PROBE, f, "--config-dir", os.path.join(self.tmp, "absent.conf.d"))
        self.assertEqual(rc, 1, "a missing --config-dir must not load")
        self.assertIn("does not load", err)

    # -- mutants ----------------------------------------------------------------------------------------------
    def test_m1_name_matching_is_killed(self):
        for shape in TITLED:
            f = self.fixture(shape)
            rc, err = probe(self.mutants["M1"], f)
            self.assertEqual(rc, 1, "M1 (argv name matching) passed on %s" % shape)
            self.assertIn("no process other than this probe", err)
        f = self.fixture("plain")
        self.assertEqual(probe(self.mutants["M1"], f)[0], 0, "M1 must still pass the plain shape (a fair mutant)")

    def test_m2_config_from_argv_is_killed(self):
        for shape in TITLED:
            f = self.fixture(shape)
            rc, err = probe(self.mutants["M2"], f)
            self.assertEqual(rc, 1, "M2 (config from argv) passed on %s" % shape)
            self.assertIn("--config-file", err)
        f = self.fixture("plain")
        self.assertEqual(probe(self.mutants["M2"], f)[0], 0, "M2 must still pass the plain shape (a fair mutant)")

    def test_m3_mount_namespace_filter(self):
        f = self.fixture("threading")
        unshare = shutil.which("unshare")
        prefix = None
        if unshare and subprocess.run(["sudo", "-n", unshare, "-m", "--propagation", "private", "true"],
                                      capture_output=True).returncode == 0:
            prefix = ("sudo", "-n", unshare, "-m", "--propagation", "private")
        if prefix:
            # The probe runs in a new mount namespace: the fixture is another container's process.
            rc, err = probe(PROBE, f, prefix=prefix)
            self.assertEqual(rc, 1, "a process outside the probe's mount namespace counted")
            rc, err = probe(self.mutants["M3"], f, prefix=prefix)
            self.assertEqual(rc, 0, "M3 (filter dropped) was not detected: %s" % err)
            return
        sys.stderr.write("\nNOTE test_m3_mount_namespace_filter used a STUB: `sudo -n unshare -m` is not available here, "
                         "so the fixture's pid is given another mount namespace in-process.\n")
        for path, want_found in ((PROBE, False), (self.mutants["M3"], True)):
            spec = importlib.util.spec_from_file_location("probe_stub_%d" % want_found, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            real = mod.mount_ns
            mod.mount_ns = lambda pid, real=real, fpid=f.child.pid: "mnt:[other]" if str(pid) == str(fpid) else real(pid)
            self.assertEqual(f.child.pid in mod.container_processes(), want_found,
                             "%s: the fixture in another mount namespace %s" % (
                                 os.path.basename(path), "was not counted" if want_found else "was counted"))


if __name__ == "__main__":
    unittest.main()
