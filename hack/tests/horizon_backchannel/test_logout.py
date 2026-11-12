"""The logout path end to end through Django: the URL Horizon mounts, CSRF exemption, the
in-memory session table, Keystone revocation and the forward to mod_auth_openidc (both mocked),
and the login signal that records the subject."""
import http.server
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse

import support

support.setup_django()

from django.conf import settings  # noqa: E402
from django.contrib.auth.signals import user_logged_in  # noqa: E402
from django.contrib.sessions.backends.db import SessionStore  # noqa: E402
from django.contrib.sessions.models import Session  # noqa: E402
from django.test import Client  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from django.test import override_settings  # noqa: E402

from krateo_backchannel import logout  # noqa: E402

URL = "/auth/backchannel-logout/"
HOST = "horizon-int.openstack.svc"
TOKENS_URL = support.KEYSTONE_URL + "/auth/tokens"
SERVICE_TOKEN = "service-token-value"


def make_session(sub=None, scoped="scoped-1", unscoped="unscoped-1", record_unscoped=None):
    s = SessionStore()
    s["token"] = support.FakeToken(scoped, unscoped)
    s["user_id"] = "u-1"
    if sub is not None:
        s[logout.SESSION_KEY] = {"subs": [sub], "user_id": "u-1",
                                 "unscoped_token": record_unscoped or unscoped}
    s.create()
    return s.session_key


def live_keys():
    return set(Session.objects.values_list("session_key", flat=True))


class Base(unittest.TestCase):

    def setUp(self):
        Session.objects.all().delete()
        logout._JWKS.clear()
        self.transport = support.FakeTransport()
        self.saved_transport = logout.TRANSPORT
        logout.TRANSPORT = self.transport
        self.transport.route("GET", support.JWKS_URL, body=json.dumps(support.JWKS).encode())
        self.transport.route("POST", TOKENS_URL, status=201,
                             headers={"X-Subject-Token": SERVICE_TOKEN})
        self.transport.route("DELETE", TOKENS_URL, status=204)
        self.transport.route("POST", support.FORWARD_URL, status=200)
        self.creds = tempfile.mkdtemp()
        for k, v in {"OS_USERNAME": "admin", "OS_PASSWORD": "pw-not-real",
                     "OS_USER_DOMAIN_NAME": "default", "OS_PROJECT_NAME": "admin",
                     "OS_PROJECT_DOMAIN_NAME": "default"}.items():
            with open(os.path.join(self.creds, k), "w") as f:
                f.write(v + "\n")
        cfg = dict(settings.KRATEO_BACKCHANNEL)
        cfg["service_credentials_dir"] = self.creds
        self.override = override_settings(KRATEO_BACKCHANNEL=cfg)
        self.override.enable()
        # enforce_csrf_checks: the receiver must be reachable without a CSRF token.
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST=HOST)

    def tearDown(self):
        self.override.disable()
        logout.TRANSPORT = self.saved_transport
        shutil.rmtree(self.creds)

    def post(self, token):
        return self.client.post(URL, {"logout_token": token})


class ValidLogout(Base):

    def test_ends_every_session_of_the_user_and_forwards(self):
        a1 = make_session(support.USER_SUB, scoped="a1-scoped", unscoped="a-unscoped")
        a2 = make_session(support.USER_SUB, scoped="a2-scoped", unscoped="a2-unscoped")
        b = make_session("someone-else", scoped="b-scoped", unscoped="b-unscoped")
        local = make_session(None, scoped="l-scoped", unscoped="l-unscoped")
        token = support.keycloak_token()
        with self.assertLogs("krateo_backchannel", level="INFO") as logs:
            resp = self.post(token)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("no-store", resp["Cache-Control"])
        self.assertEqual(live_keys(), {b, local})
        self.assertNotIn(a1, live_keys())
        self.assertNotIn(a2, live_keys())

        revoked = [c["headers"]["X-Subject-Token"] for c in self.transport.made("DELETE", TOKENS_URL)]
        self.assertEqual(sorted(revoked), sorted(["a1-scoped", "a-unscoped", "a2-scoped", "a2-unscoped"]))
        for c in self.transport.made("DELETE", TOKENS_URL):
            self.assertEqual(c["headers"]["X-Auth-Token"], SERVICE_TOKEN)

        auth = self.transport.made("POST", TOKENS_URL)
        self.assertEqual(len(auth), 1)
        body = json.loads(auth[0]["body"])
        self.assertEqual(body["auth"]["identity"]["password"]["user"]["name"], "admin")

        fwd = self.transport.made("POST", support.FORWARD_URL)
        self.assertEqual(len(fwd), 1)
        self.assertEqual(fwd[0]["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(urllib.parse.parse_qs(fwd[0]["body"].decode()), {"logout_token": [token]})

        text = "\n".join(logs.output)
        for secret in (token, "a1-scoped", "a-unscoped", SERVICE_TOKEN, "pw-not-real"):
            self.assertNotIn(secret, text)

    def test_scoped_token_after_project_switch_is_revoked(self):
        # The session's current scoped token is read at logout, not at login.
        make_session(support.USER_SUB, scoped="switched-scoped", unscoped="u0",
                     record_unscoped="u0")
        self.assertEqual(self.post(support.keycloak_token()).status_code, 200)
        revoked = {c["headers"]["X-Subject-Token"] for c in self.transport.made("DELETE", TOKENS_URL)}
        self.assertEqual(revoked, {"switched-scoped", "u0"})

    def test_quoted_unique_id_matches(self):
        # Keystone stores parse.quote(<mapping user.id>) as the federated unique id.
        sub = "jane@example.test"
        k = make_session(urllib.parse.quote(sub))
        self.assertEqual(self.post(support.keycloak_token(sub=sub)).status_code, 200)
        self.assertNotIn(k, live_keys())

    def test_no_matching_session_still_forwards(self):
        keep = make_session("someone-else")
        self.assertEqual(self.post(support.keycloak_token()).status_code, 200)
        self.assertEqual(live_keys(), {keep})
        self.assertEqual(self.transport.made("POST", TOKENS_URL), [])
        self.assertEqual(len(self.transport.made("POST", support.FORWARD_URL)), 1)

    def test_already_invalid_token_counts_as_revoked(self):
        self.transport.route("DELETE", TOKENS_URL, status=404)
        make_session(support.USER_SUB)
        self.assertEqual(self.post(support.keycloak_token()).status_code, 200)


class ForwardFailure(Base):

    def test_forward_refused_returns_non_200(self):
        self.transport.route("POST", support.FORWARD_URL, status=400)
        k = make_session(support.USER_SUB)
        resp = self.post(support.keycloak_token())
        self.assertNotEqual(resp.status_code, 200)
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn(k, live_keys())   # Horizon's part is done regardless

    def test_forward_unreachable_returns_non_200(self):
        import urllib.error
        self.transport.route("POST", support.FORWARD_URL,
                             raises=urllib.error.URLError("connection refused"))
        make_session(support.USER_SUB)
        self.assertEqual(self.post(support.keycloak_token()).status_code, 502)

    def test_forward_redirect_is_not_success(self):
        self.transport.route("POST", support.FORWARD_URL, status=302,
                             headers={"Location": "https://login.example.test/"})
        self.assertEqual(self.post(support.keycloak_token()).status_code, 502)


class KeystoneFailure(Base):

    def test_revoke_error_returns_non_200_but_session_ends_and_forward_runs(self):
        self.transport.route("DELETE", TOKENS_URL, status=500)
        k = make_session(support.USER_SUB)
        self.assertEqual(self.post(support.keycloak_token()).status_code, 502)
        self.assertNotIn(k, live_keys())
        self.assertEqual(len(self.transport.made("POST", support.FORWARD_URL)), 1)

    def test_service_auth_failure_returns_non_200_but_session_ends(self):
        self.transport.route("POST", TOKENS_URL, status=401)
        k = make_session(support.USER_SUB)
        self.assertEqual(self.post(support.keycloak_token()).status_code, 502)
        self.assertNotIn(k, live_keys())
        self.assertEqual(self.transport.made("DELETE", TOKENS_URL), [])

    def test_missing_service_credentials_returns_non_200(self):
        os.remove(os.path.join(self.creds, "OS_PASSWORD"))
        make_session(support.USER_SUB)
        self.assertEqual(self.post(support.keycloak_token()).status_code, 502)


class InvalidChangesNothing(Base):

    def assertNothingChanged(self, resp, before, status=400):
        self.assertEqual(resp.status_code, status)
        self.assertEqual(live_keys(), before)
        for method, url in (("POST", TOKENS_URL), ("DELETE", TOKENS_URL),
                            ("POST", support.FORWARD_URL)):
            self.assertEqual(self.transport.made(method, url), [])

    def test_bad_signature(self):
        make_session(support.USER_SUB)
        before = live_keys()
        self.assertNothingChanged(self.post(support.keycloak_token(key=support.OTHER_RSA_KEY)), before)

    def test_reflected_issuer(self):
        make_session(support.USER_SUB)
        before = live_keys()
        resp = self.post(support.keycloak_token(iss="http://127.0.0.1:8080/realms/openstack"))
        self.assertNothingChanged(resp, before)

    def test_expired(self):
        make_session(support.USER_SUB)
        before = live_keys()
        resp = self.post(support.keycloak_token(now=time.time() - 600))
        self.assertNothingChanged(resp, before)

    def test_missing_form_field(self):
        make_session(support.USER_SUB)
        before = live_keys()
        self.assertNothingChanged(self.client.post(URL, {"token": "x"}), before)

    def test_jwks_unavailable_is_503_and_changes_nothing(self):
        self.transport.route("GET", support.JWKS_URL, status=500)
        make_session(support.USER_SUB)
        before = live_keys()
        self.assertNothingChanged(self.post(support.keycloak_token()), before, status=503)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(URL).status_code, 405)

    def test_not_configured_is_503(self):
        with override_settings(KRATEO_BACKCHANNEL={"issuer": support.REALM}):
            self.assertEqual(self.post(support.keycloak_token()).status_code, 503)


class FakeUser(object):
    def __init__(self, id, unscoped_token="user-unscoped", token=None):
        self.id = id
        self.unscoped_token = unscoped_token
        self.token = token

    def save(self, *args, **kwargs):
        # openstack_auth.user.User.save is a no-op too; django.contrib.auth's update_last_login
        # receiver calls it on every login.
        pass


class LoginRecordsSubject(Base):

    def login(self, user):
        request = RequestFactory().get("/auth/websso/")
        request.session = SessionStore()
        user_logged_in.send(sender=FakeUser, request=request, user=user)
        return request.session

    def user_route(self, user_id, federated, status=200):
        self.transport.route("GET", "%s/users/%s" % (support.KEYSTONE_URL, user_id), status=status,
                             body=json.dumps(
                                 {"user": {"id": user_id, "federated": federated}}).encode())

    def test_federated_user_is_recorded(self):
        self.user_route("u-42", [{"idp_id": "keycloak", "protocols": [
            {"protocol_id": "openid", "unique_id": support.USER_SUB}]}])
        session = self.login(FakeUser("u-42"))
        self.assertEqual(session[logout.SESSION_KEY], {
            "subs": [support.USER_SUB], "user_id": "u-42", "unscoped_token": "user-unscoped"})
        get = self.transport.made("GET")[-1]
        self.assertEqual(get["headers"]["X-Auth-Token"], "user-unscoped")

    def test_identity_provider_filter(self):
        self.user_route("u-43", [
            {"idp_id": "other", "protocols": [{"protocol_id": "openid", "unique_id": "x"}]},
            {"idp_id": "keycloak", "protocols": [{"protocol_id": "openid", "unique_id": "y"}]}])
        cfg = dict(self.override.options["KRATEO_BACKCHANNEL"], identity_provider="keycloak")
        with override_settings(KRATEO_BACKCHANNEL=cfg):
            session = self.login(FakeUser("u-43"))
        self.assertEqual(session[logout.SESSION_KEY]["subs"], ["y"])

    def test_local_user_is_not_recorded(self):
        self.user_route("u-admin", [])
        session = self.login(FakeUser("u-admin"))
        self.assertNotIn(logout.SESSION_KEY, session)

    def test_keystone_error_does_not_break_login(self):
        self.user_route("u-44", [], status=500)
        logging.disable(logging.CRITICAL)
        try:
            session = self.login(FakeUser("u-44"))
        finally:
            logging.disable(logging.NOTSET)
        self.assertNotIn(logout.SESSION_KEY, session)

    def test_recorded_login_is_then_logged_out(self):
        self.user_route("u-45", [{"idp_id": "keycloak", "protocols": [
            {"protocol_id": "openid", "unique_id": support.USER_SUB}]}])
        session = self.login(FakeUser("u-45", token=support.FakeToken("s-45", "user-unscoped")))
        session["token"] = support.FakeToken("s-45", "user-unscoped")
        session.save()
        self.assertIn(session.session_key, live_keys())
        self.assertEqual(self.post(support.keycloak_token()).status_code, 200)
        self.assertNotIn(session.session_key, live_keys())


class _Redirector(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(302)
        self.send_header("Location", "/landed")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class RealTransport(unittest.TestCase):

    def test_redirect_is_reported_not_followed(self):
        # urllib would turn the redirected POST into a GET of /landed and report its 200 as a
        # successful forward.
        server = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            r = logout.urllib_transport("POST", "http://127.0.0.1:%d/redirect?logout=backchannel"
                                        % server.server_port, body=b"logout_token=x", timeout=5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(r.status, 302)


if __name__ == "__main__":
    unittest.main()
