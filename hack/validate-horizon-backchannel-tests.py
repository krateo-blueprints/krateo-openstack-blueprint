#!/usr/bin/env python3
"""Run the unit tests of Horizon's OIDC back-channel logout receiver.

The receiver ships as plain Python in blueprints/horizon/chart/files/krateo_backchannel/ and is
installed into Horizon at pod start. Horizon executes its local_settings.d snippets inside a
try/except that only logs, and a failing receiver is only visible at the moment a user is being
logged out -- so the tests here are the place its behaviour is established:

  - test_jws.py      the validator: a Keycloak-shaped token (header and claims captured from a
                     live Keycloak 26.4 back-channel logout) and every rejection;
  - test_logout.py   the logout path through Django: Horizon's ^auth/ URL mount, CSRF exemption,
                     the database session table (in-memory sqlite), Keystone revocation and the
                     forward to mod_auth_openidc (mocked), the forward-refused case returning
                     non-200, and the login signal that records the subject.

The tests import exactly the files the chart ships, not a copy.

Fails when the dependencies are missing, when no test ran, and when any test was skipped: a
skipped test here is a behaviour nobody checked.

Usage: hack/validate-horizon-backchannel-tests.py [repo-root]
"""
import os
import sys
import unittest

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else
                       os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACKAGE_PARENT = os.path.join(ROOT, "blueprints/horizon/chart/files")
TESTS = os.path.join(ROOT, "hack/tests/horizon_backchannel")

try:
    import cryptography  # noqa: F401
    import django  # noqa: F401
except ImportError as e:
    print("FAILED MEASUREMENT: %s, so no receiver test ran (pip install Django cryptography)" % e)
    sys.exit(2)

if not os.path.isfile(os.path.join(PACKAGE_PARENT, "krateo_backchannel/logout.py")):
    print("FAILED MEASUREMENT: no receiver package under %s" % PACKAGE_PARENT)
    sys.exit(2)

sys.path[0:0] = [TESTS, PACKAGE_PARENT]
suite = unittest.defaultTestLoader.discover(TESTS, pattern="test_*.py", top_level_dir=TESTS)
result = unittest.TextTestRunner(verbosity=1).run(suite)

if result.testsRun == 0:
    print("FAILED MEASUREMENT: discovered 0 tests under %s -- a gate over nothing passes "
          "everything" % TESTS)
    sys.exit(2)
if result.skipped:
    print("FAIL: %d test(s) skipped; every behaviour here must be checked" % len(result.skipped))
    sys.exit(1)
if not result.wasSuccessful():
    print("FAIL: %d failure(s), %d error(s) in %d receiver tests"
          % (len(result.failures), len(result.errors), result.testsRun))
    sys.exit(1)
print("OK: %d back-channel logout receiver tests passed" % result.testsRun)
