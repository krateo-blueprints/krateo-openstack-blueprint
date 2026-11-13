"""End a user's Horizon sessions on an OIDC back-channel logout, then forward it to Keystone.

Matching is per user: the logout token's `sub` is compared with the federated unique ids that
were recorded in each session at login, so ONE back-channel logout ends EVERY Horizon session
of that user, not only the one tied to the Keycloak session (`sid`) that was logged out.

For each matching session: revoke its Keystone tokens (DELETE /v3/auth/tokens with
X-Subject-Token, authenticated with Horizon's service credentials), then delete the session
row. Those are the session's scoped and unscoped tokens AND the WebSSO token Keystone handed the
browser: Horizon does not keep that one, it authenticates with it to get its own unscoped token
(openstack_auth's TokenPlugin), and revoking a token never revokes the one it was made from, so
without the recorded WebSSO token it would outlive the logout. Then forward the raw logout token, as `logout_token=<jwt>`, to mod_auth_openidc's
back-channel endpoint on Keystone. Success is reported only if every step succeeded.

Tokens are never logged: not the logout token, not a Keystone token, not the service password.
"""
import collections
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import jws

LOG = logging.getLogger(__name__)

# The session key written at login and read on logout.
SESSION_KEY = "krateo_backchannel"

REQUIRED = ("issuer", "audience", "jwks_url", "forward_url")
DEFAULTS = {
    "identity_provider": "",
    "iat_skew_seconds": 60,
    "max_token_age_seconds": 300,
    "require_typ": True,
    "timeout_seconds": 10,
    "ca_file": "",
    "jwks_cache_seconds": 300,
    "service_credentials_dir": "/etc/openstack-dashboard/backchannel-service-auth",
    "keystone_url": "",
}


class NotConfigured(Exception):
    pass


def config(raw):
    """Merge the KRATEO_BACKCHANNEL settings dict over the defaults; refuse missing keys."""
    if not isinstance(raw, dict):
        raise NotConfigured("KRATEO_BACKCHANNEL is not set")
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        raise NotConfigured("KRATEO_BACKCHANNEL is missing %s" % ", ".join(missing))
    return cfg


def from_django_settings():
    """The receiver's config from Django settings; Keystone defaults to OPENSTACK_KEYSTONE_URL."""
    from django.conf import settings
    cfg = config(getattr(settings, "KRATEO_BACKCHANNEL", None))
    if not cfg.get("keystone_url"):
        cfg["keystone_url"] = getattr(settings, "OPENSTACK_KEYSTONE_URL", "")
    return cfg


# --- HTTP ---------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib turns a redirected POST into a GET of the new location. For the forward that would
    # report the 200 of whatever page the redirect lands on as a successful logout. A redirect
    # is a misconfiguration here; surface it as the 3xx it is.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


Response = collections.namedtuple("Response", "status headers body")


def urllib_transport(method, url, headers=None, body=None, timeout=10, ca_file=""):
    """The one place that touches the network, so tests replace exactly this."""
    handlers = [_NoRedirect()]
    if url.startswith("https:"):
        handlers.append(urllib.request.HTTPSHandler(
            context=ssl.create_default_context(cafile=ca_file or None)))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with opener.open(req, timeout=timeout) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        return Response(e.code, hdrs, e.read() if e.fp else b"")


# Replaced by the tests; production uses urllib.
TRANSPORT = urllib_transport


def _call(cfg, method, url, headers=None, body=None):
    return TRANSPORT(method, url, headers=headers, body=body,
                     timeout=cfg["timeout_seconds"], ca_file=cfg["ca_file"])


# --- JWKS ---------------------------------------------------------------------------------

class JWKSCache(object):
    """The realm's keys, refetched when stale or when a token names a kid we do not hold.

    An unknown kid refetches at most once per `min_refresh` seconds, so a stream of forged
    tokens with random kids cannot turn the receiver into a load generator against Keycloak.
    """

    def __init__(self, cfg, min_refresh=10.0):
        self.cfg = cfg
        self.min_refresh = min_refresh
        self._jwks = None
        self._fetched = 0.0
        self._lock = threading.Lock()

    def _fetch(self):
        try:
            r = _call(self.cfg, "GET", self.cfg["jwks_url"], headers={"Accept": "application/json"})
        except (OSError, ValueError) as e:
            raise jws.KeysUnavailable("JWKS fetch failed: %s" % e.__class__.__name__)
        if r.status != 200:
            raise jws.KeysUnavailable("JWKS fetch returned HTTP %s" % r.status)
        try:
            self._jwks = json.loads(r.body)
        except ValueError:
            raise jws.KeysUnavailable("JWKS is not JSON")
        self._fetched = time.monotonic()

    def __call__(self, kid):
        with self._lock:
            age = time.monotonic() - self._fetched
            if self._jwks is None or age > self.cfg["jwks_cache_seconds"]:
                self._fetch()
                age = 0.0
            keys = jws.select_keys(self._jwks, kid)
            if not keys and kid is not None and age > self.min_refresh:
                self._fetch()
                keys = jws.select_keys(self._jwks, kid)
            return keys


_JWKS = {}
_JWKS_LOCK = threading.Lock()


def jwks_for(cfg):
    with _JWKS_LOCK:
        cache = _JWKS.get(cfg["jwks_url"])
        if cache is None or cache.cfg != cfg:
            cache = _JWKS[cfg["jwks_url"]] = JWKSCache(cfg)
        return cache


def validate(cfg, token, now=None):
    return jws.validate(
        token,
        issuer=cfg["issuer"],
        audience=cfg["audience"],
        keys_for=jwks_for(cfg),
        now=now,
        skew_seconds=cfg["iat_skew_seconds"],
        max_age_seconds=cfg["max_token_age_seconds"],
        require_typ=cfg["require_typ"],
    )


# --- Keystone -----------------------------------------------------------------------------

class KeystoneError(Exception):
    pass


def _keystone_url(cfg):
    url = cfg.get("keystone_url")
    if not url:
        raise NotConfigured("no Keystone URL")
    return url.rstrip("/")


def _read_credentials(cfg):
    """Horizon's service credentials: the keys of the chart's horizon-keystone-admin Secret,
    mounted as files. Read per call, so a rotated Secret is picked up without a restart."""
    d = cfg["service_credentials_dir"]
    out = {}
    for name in ("OS_USERNAME", "OS_PASSWORD", "OS_USER_DOMAIN_NAME", "OS_PROJECT_NAME",
                 "OS_PROJECT_DOMAIN_NAME"):
        try:
            with open("%s/%s" % (d, name), encoding="utf-8") as f:
                out[name] = f.read().strip()
        except OSError:
            raise KeystoneError("service credential %s not readable under %s" % (name, d))
    return out


def service_token(cfg):
    c = _read_credentials(cfg)
    body = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": c["OS_USERNAME"],
                        "domain": {"name": c["OS_USER_DOMAIN_NAME"]},
                        "password": c["OS_PASSWORD"],
                    },
                },
            },
            "scope": {
                "project": {
                    "name": c["OS_PROJECT_NAME"],
                    "domain": {"name": c["OS_PROJECT_DOMAIN_NAME"]},
                },
            },
        },
    }
    r = _call(cfg, "POST", _keystone_url(cfg) + "/auth/tokens",
              headers={"Content-Type": "application/json"}, body=json.dumps(body).encode())
    token = r.headers.get("x-subject-token")
    if r.status != 201 or not token:
        raise KeystoneError("service authentication returned HTTP %s" % r.status)
    return token


def revoke(cfg, service, token_id):
    """True if the token is now unusable: revoked (204) or already invalid (404)."""
    r = _call(cfg, "DELETE", _keystone_url(cfg) + "/auth/tokens",
              headers={"X-Auth-Token": service, "X-Subject-Token": token_id})
    return r.status in (200, 204, 404)


def federated_unique_ids(cfg, user_id, token_id):
    """The user's federated unique ids from GET /v3/users/{id} (`federated[].protocols[]`).

    Keystone's mapped auth stores `quote(<mapping user.id>)` as the unique id, so with the
    mapping taking user.id from the OIDC `sub`, this is the (quoted) Keycloak user id.
    The user's own token is enough: identity:get_user allows a user to read itself.
    """
    url = "%s/users/%s" % (_keystone_url(cfg), urllib.parse.quote(user_id, safe=""))
    r = _call(cfg, "GET", url, headers={"X-Auth-Token": token_id, "Accept": "application/json"})
    if r.status != 200:
        raise KeystoneError("GET /v3/users/{id} returned HTTP %s" % r.status)
    user = json.loads(r.body).get("user") or {}
    idp = cfg.get("identity_provider")
    ids = []
    for fed in user.get("federated") or []:
        if idp and fed.get("idp_id") != idp:
            continue
        for proto in fed.get("protocols") or []:
            uid = proto.get("unique_id")
            if isinstance(uid, str) and uid and uid not in ids:
                ids.append(uid)
    return ids


# --- Sessions -----------------------------------------------------------------------------

def subject_candidates(sub):
    # Keystone stores parse.quote(user.id); a Keycloak id is a UUID, for which both are equal.
    return {sub, urllib.parse.quote(sub)}


def _token_ids(data):
    ids = []
    rec = data.get(SESSION_KEY) or {}
    tok = data.get("token")
    for value in (
        getattr(tok, "id", None) if not isinstance(tok, dict) else tok.get("id"),
        getattr(tok, "unscoped_token", None) if not isinstance(tok, dict) else tok.get("unscoped_token"),
        rec.get("unscoped_token"),
        rec.get("websso_token"),
    ):
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)
    return ids


class DjangoSessions(object):
    """Horizon's sessions in Django's database session table."""

    def __init__(self):
        from importlib import import_module

        from django.conf import settings
        self.store_cls = import_module(settings.SESSION_ENGINE).SessionStore
        if not hasattr(self.store_cls, "get_model_class"):
            raise NotConfigured("SESSION_ENGINE %s is not database-backed" % settings.SESSION_ENGINE)
        self.model = self.store_cls.get_model_class()

    def matching(self, sub):
        """[(session_key, [keystone token ids])] for every live session recorded for `sub`."""
        from django.utils import timezone
        wanted = subject_candidates(sub)
        found = []
        decoder = self.store_cls()
        for row in self.model.objects.filter(expire_date__gt=timezone.now()).iterator():
            data = decoder.decode(row.session_data)
            rec = data.get(SESSION_KEY)
            if isinstance(rec, dict) and wanted.intersection(rec.get("subs") or []):
                found.append((row.session_key, _token_ids(data)))
        return found

    def delete(self, session_key):
        self.model.objects.filter(session_key=session_key).delete()

    def clear_expired(self):
        self.store_cls.clear_expired()


Outcome = collections.namedtuple("Outcome", "status sessions revoke_failures forward_status")


def end_sessions_and_forward(cfg, claims, raw_token, sessions):
    """Run the logout for an ALREADY VALIDATED token. Returns an Outcome; never raises."""
    sub = claims["sub"]
    try:
        matches = sessions.matching(sub)
    except Exception:
        LOG.exception("back-channel logout: listing Horizon sessions failed")
        return Outcome(500, 0, 0, None)
    LOG.info("back-channel logout: sub matched %d Horizon session(s)", len(matches))

    revoke_failures = 0
    service = None
    if any(tokens for _key, tokens in matches):
        try:
            service = service_token(cfg)
        except Exception as e:
            LOG.error("back-channel logout: service authentication failed (%s); "
                      "sessions are still deleted, their tokens are NOT revoked", e)
    for key, tokens in matches:
        for token_id in tokens:
            ok = False
            if service is not None:
                try:
                    ok = revoke(cfg, service, token_id)
                except Exception as e:
                    LOG.error("back-channel logout: token revocation errored (%s)", e.__class__.__name__)
            if not ok:
                revoke_failures += 1
        try:
            sessions.delete(key)
        except Exception:
            LOG.exception("back-channel logout: deleting a Horizon session failed")
            return Outcome(500, len(matches), revoke_failures, None)
    LOG.info("back-channel logout: %d session(s) deleted, %d token revocation(s) failed",
             len(matches), revoke_failures)
    try:
        sessions.clear_expired()
    except Exception:
        LOG.warning("back-channel logout: clearing expired sessions failed", exc_info=True)

    forward_status = None
    try:
        r = _call(cfg, "POST", cfg["forward_url"],
                  headers={"Content-Type": "application/x-www-form-urlencoded"},
                  body=urllib.parse.urlencode({"logout_token": raw_token}).encode())
        forward_status = r.status
    except Exception as e:
        LOG.error("back-channel logout: forward to Keystone failed (%s)", e.__class__.__name__)
    LOG.info("back-channel logout: forward to Keystone returned %s", forward_status)

    if forward_status is None or not 200 <= forward_status < 300:
        return Outcome(502, len(matches), revoke_failures, forward_status)
    if revoke_failures:
        return Outcome(502, len(matches), revoke_failures, forward_status)
    return Outcome(200, len(matches), revoke_failures, forward_status)


# --- Login --------------------------------------------------------------------------------

def record_subject(cfg, session, user, websso_token=None):
    """At login, store the user's federated unique ids, user id, unscoped token id and the WebSSO
    token the login was made with (if any) in the session, so a back-channel logout can find the
    session and revoke every token it holds. Non-federated users get nothing recorded."""
    token = getattr(user, "unscoped_token", None) or getattr(getattr(user, "token", None), "id", None)
    if not token:
        LOG.error("back-channel logout: no Keystone token on the logged-in user; not recorded")
        session.pop(SESSION_KEY, None)
        return None
    ids = federated_unique_ids(cfg, user.id, token)
    if not ids:
        LOG.info("back-channel logout: user %s has no federated identity; not recorded", user.id)
        session.pop(SESSION_KEY, None)
        return None
    rec = {"subs": ids, "user_id": user.id, "unscoped_token": token}
    if isinstance(websso_token, str) and websso_token:
        rec["websso_token"] = websso_token
    session[SESSION_KEY] = rec
    LOG.info("back-channel logout: recorded user %s for back-channel logout", user.id)
    return ids
