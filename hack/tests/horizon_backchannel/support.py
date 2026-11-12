"""Shared fixtures: a local signing key and JWKS, Keycloak-shaped logout tokens, a recorded fake
transport, and a Django configured the way Horizon is (DB sessions, auth URLs under ^auth/)."""
import base64
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hmac
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

REALM = "https://keycloak.example.test/realms/openstack"
JWKS_URL = REALM + "/protocol/openid-connect/certs"
CLIENT_ID = "keystone"
FORWARD_URL = ("http://keystone-api.openstack.svc.cluster.local:5000/v3/auth/OS-FEDERATION/"
               "websso/openid/redirect?logout=backchannel")
KEYSTONE_URL = "http://keystone-api.openstack.svc.cluster.local:5000/v3"
EVENT = "http://schemas.openid.net/event/backchannel-logout"
KID = "kc-rsa-1"
EC_KID = "kc-ec-1"
USER_SUB = "6f1c2c7e-3b1d-4a8e-9a55-0d6b5f9b2e11"


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64(n, size=None):
    size = size or (n.bit_length() + 7) // 8
    return b64url(n.to_bytes(size, "big"))


RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())


def rsa_jwk(key, kid, **extra):
    pub = key.public_key().public_numbers()
    jwk = {"kid": kid, "kty": "RSA", "alg": "RS256", "use": "sig",
           "n": _int_b64(pub.n), "e": _int_b64(pub.e)}
    jwk.update(extra)
    return jwk


def ec_jwk(key, kid):
    pub = key.public_key().public_numbers()
    return {"kid": kid, "kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig",
            "x": _int_b64(pub.x, 32), "y": _int_b64(pub.y, 32)}


# The realm JWKS as Keycloak publishes it: a signing key plus an encryption key (use=enc).
JWKS = {"keys": [
    rsa_jwk(RSA_KEY, KID),
    ec_jwk(EC_KEY, EC_KID),
    rsa_jwk(OTHER_RSA_KEY, "kc-rsa-enc", use="enc", alg="RSA-OAEP"),
]}


def keycloak_claims(now=None, **overrides):
    """Exactly the claim set captured from a live Keycloak 26.4 back-channel logout."""
    now = int(time.time() if now is None else now)
    claims = {
        "iat": now,
        "exp": now + 120,
        "jti": "4f1d9a7e-0c55-4a51-8f0e-2d9a1d3c7b20",
        "iss": REALM,
        "aud": CLIENT_ID,
        "sub": USER_SUB,
        "typ": "Logout",
        "sid": "b3f0a3e2-6c11-4e0f-a9e5-61a2b0a9c4d7",
        "events": {EVENT: {}},
    }
    for k, v in overrides.items():
        if v is DROP:
            claims.pop(k, None)
        else:
            claims[k] = v
    return claims


DROP = object()


def sign(claims, header=None, key=RSA_KEY, alg="RS256"):
    hdr = {"alg": alg, "typ": "logout+jwt", "kid": KID}
    for k, v in (header or {}).items():
        if v is DROP:
            hdr.pop(k, None)
        else:
            hdr[k] = v
    signing_input = (b64url(json.dumps(hdr).encode()) + "." +
                     b64url(json.dumps(claims).encode())).encode("ascii")
    if alg == "none":
        sig = b""
    elif alg.startswith("HS"):
        # The algorithm-confusion forgery: HMAC keyed with the realm's PUBLIC key.
        pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
        h = hmac.HMAC(pem, hashes.SHA256())
        h.update(signing_input)
        sig = h.finalize()
    elif alg.startswith("ES"):
        r, s = asym_utils.decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    else:
        sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + b64url(sig)


def keycloak_token(now=None, header=None, key=RSA_KEY, alg="RS256", **claim_overrides):
    return sign(keycloak_claims(now, **claim_overrides), header=header, key=key, alg=alg)


def static_keys(jwks=None):
    from krateo_backchannel import jws
    doc = JWKS if jwks is None else jwks
    return lambda kid: jws.select_keys(doc, kid)


class FakeTransport(object):
    """Stands in for logout.TRANSPORT. Routes by (method, url without query) to a response
    or an exception, and records every call."""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def route(self, method, url, status=200, headers=None, body=b"", raises=None):
        self.routes[(method, url)] = (status, headers or {}, body, raises)

    def __call__(self, method, url, headers=None, body=None, timeout=10, ca_file=""):
        from krateo_backchannel import logout
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "body": body})
        status, hdrs, rbody, raises = self.routes.get((method, url), (599, {}, b"", None))
        if raises is not None:
            raise raises
        return logout.Response(status, {k.lower(): v for k, v in hdrs.items()}, rbody)

    def made(self, method, url=None):
        return [c for c in self.calls
                if c["method"] == method and (url is None or c["url"] == url)]


class FakeToken(object):
    """The shape of openstack_auth.user.Token that matters here: .id and .unscoped_token."""

    def __init__(self, id, unscoped_token):
        self.id = id
        self.unscoped_token = unscoped_token


class TokenAwareSerializer(object):
    """Like openstack_auth.serializers.HorizonSerializer: JSON, with Token objects rebuilt on
    load, so the receiver reads `session['token']` as an object exactly as it does in Horizon."""

    def dumps(self, obj):
        def default(o):
            if isinstance(o, FakeToken):
                return {"__token__": {"id": o.id, "unscoped_token": o.unscoped_token}}
            raise TypeError(type(o))
        return json.dumps(obj, separators=(",", ":"), default=default).encode("latin-1")

    def loads(self, data):
        def hook(d):
            if set(d) == {"__token__"}:
                return FakeToken(**d["__token__"])
            return d
        return json.loads(data.decode("latin-1"), object_hook=hook)


urlpatterns = []

_CONFIGURED = []


def setup_django():
    if _CONFIGURED:
        return
    import django
    from django.conf import settings
    from django.core.management import call_command
    settings.configure(
        DEBUG=False,
        SECRET_KEY="unit-test-only",
        ALLOWED_HOSTS=["horizon-int.openstack.svc"],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        INSTALLED_APPS=[
            "krateo_backchannel",
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
        ],
        MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
        ],
        ROOT_URLCONF="support",
        SESSION_ENGINE="django.contrib.sessions.backends.db",
        SESSION_SERIALIZER="support.TokenAwareSerializer",
        USE_TZ=True,
        OPENSTACK_KEYSTONE_URL=KEYSTONE_URL,
        KRATEO_BACKCHANNEL={
            "issuer": REALM,
            "audience": CLIENT_ID,
            "jwks_url": JWKS_URL,
            "forward_url": FORWARD_URL,
            "iat_skew_seconds": 60,
            "max_token_age_seconds": 300,
            "require_typ": True,
            "timeout_seconds": 5,
        },
    )
    django.setup()
    # Horizon mounts every AUTHENTICATION_URLS module with exactly this pattern
    # (openstack_dashboard/urls.py).
    from django.urls import include, re_path
    urlpatterns.append(re_path(r"^auth/", include("krateo_backchannel.urls")))
    call_command("migrate", verbosity=0, interactive=False)
    _CONFIGURED.append(True)
