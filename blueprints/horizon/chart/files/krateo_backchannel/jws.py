"""Validate an OpenID Connect back-channel logout token.

OpenID Connect Back-Channel Logout 1.0, section 2.6, as Keycloak 26 sends it:

    header  {"alg": "RS256", "typ": "logout+jwt", "kid": "..."}
    claims  iss, aud (a string), sub, sid, typ "Logout", iat, exp (iat + 120), jti,
            events {"http://schemas.openid.net/event/backchannel-logout": {}}; no nonce

Implemented with `cryptography` only. The Horizon image is guaranteed to carry it (horizon ->
openstacksdk -> cryptography); PyJWT is not guaranteed, and a missing import here would only
show up as a 500 at the moment a user is being logged out.

Nothing in this module touches Django, the network or any state: a token is either returned as
its claims or rejected with InvalidLogoutToken, and rejection changes nothing.
"""
import base64
import binascii
import json
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

BACKCHANNEL_EVENT = "http://schemas.openid.net/event/backchannel-logout"
LOGOUT_TYP = "logout+jwt"

# Asymmetric algorithms only. "none" and the HS* family are rejected: an HMAC "verified" with a
# public key from the JWKS is the classic algorithm-confusion forgery.
ALLOWED_ALGS = (
    "RS256", "RS384", "RS512",
    "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512",
)

_HASHES = {
    "256": hashes.SHA256,
    "384": hashes.SHA384,
    "512": hashes.SHA512,
}

# JWK crv -> (curve, coordinate size in bytes, the one ES alg that uses it)
_EC_CURVES = {
    "P-256": (ec.SECP256R1, 32, "ES256"),
    "P-384": (ec.SECP384R1, 48, "ES384"),
    "P-521": (ec.SECP521R1, 66, "ES512"),
}

_B64URL = re.compile(r"^[A-Za-z0-9_-]*$")


class InvalidLogoutToken(Exception):
    """The token is not a valid logout token for this receiver. Answer 400, change nothing."""


class KeysUnavailable(Exception):
    """The realm's signing keys could not be fetched, so nothing can be validated."""


def b64url_decode(segment):
    if not isinstance(segment, str) or not _B64URL.match(segment):
        raise InvalidLogoutToken("segment is not base64url")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError):
        raise InvalidLogoutToken("segment is not base64url")


def _b64url_int(value):
    return int.from_bytes(b64url_decode(value), "big")


def public_key_from_jwk(jwk):
    """Build a verification key from one JWK, or raise InvalidLogoutToken."""
    kty = jwk.get("kty")
    try:
        if kty == "RSA":
            return rsa.RSAPublicNumbers(_b64url_int(jwk["e"]), _b64url_int(jwk["n"])).public_key()
        if kty == "EC":
            curve = _EC_CURVES.get(jwk.get("crv"))
            if curve is None:
                raise InvalidLogoutToken("unsupported EC curve %r" % jwk.get("crv"))
            numbers = ec.EllipticCurvePublicNumbers(
                _b64url_int(jwk["x"]), _b64url_int(jwk["y"]), curve[0]())
            return numbers.public_key()
    except (KeyError, TypeError, ValueError) as e:
        raise InvalidLogoutToken("malformed JWK: %s" % e.__class__.__name__)
    raise InvalidLogoutToken("unsupported JWK kty %r" % kty)


def _verify(alg, key, signing_input, signature):
    digest = _HASHES[alg[2:]]()
    try:
        if alg.startswith("RS"):
            if not isinstance(key, rsa.RSAPublicKey):
                return False
            key.verify(signature, signing_input, padding.PKCS1v15(), digest)
            return True
        if alg.startswith("PS"):
            if not isinstance(key, rsa.RSAPublicKey):
                return False
            key.verify(signature, signing_input,
                       padding.PSS(mgf=padding.MGF1(digest), salt_length=digest.digest_size),
                       digest)
            return True
        # ES*: JWS carries the raw r||s concatenation, cryptography wants DER.
        if not isinstance(key, ec.EllipticCurvePublicKey):
            return False
        curve_cls, size = next((c, s) for c, s, a in _EC_CURVES.values() if a == alg)
        if not isinstance(key.curve, curve_cls) or len(signature) != 2 * size:
            return False
        r = int.from_bytes(signature[:size], "big")
        s = int.from_bytes(signature[size:], "big")
        key.verify(asym_utils.encode_dss_signature(r, s), signing_input, ec.ECDSA(digest))
        return True
    except InvalidSignature:
        return False


def _number(claims, name):
    value = claims.get(name)
    # bool is an int subclass; `"iat": true` is not a time.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidLogoutToken("%s missing or not a number" % name)
    return value


def _typ_is_logout(value):
    if not isinstance(value, str):
        return False
    value = value.lower()
    # RFC 7515 4.1.9: "application/" may be omitted and the comparison is case-insensitive.
    return value in (LOGOUT_TYP, "application/" + LOGOUT_TYP)


def validate(token, issuer, audience, keys_for, now=None, skew_seconds=60,
             max_age_seconds=300, require_typ=True):
    """Return the claims of a valid logout token, or raise InvalidLogoutToken.

    issuer      the CONFIGURED issuer. Never derived from the token or the request: Keycloak
                reflects the triggering request's host into `iss` when it has no fixed
                hostname, so a token minted via another host must not match.
    audience    the client id the token is addressed to (a string, or a list of accepted ids).
    keys_for    callable(kid or None) -> list of JWK dicts to try; may raise KeysUnavailable.
    """
    if not isinstance(token, str) or token.count(".") != 2:
        raise InvalidLogoutToken("not a compact JWS")
    header_b64, payload_b64, sig_b64 = token.split(".")
    try:
        header = json.loads(b64url_decode(header_b64))
        claims = json.loads(b64url_decode(payload_b64))
    except (ValueError, UnicodeDecodeError):
        raise InvalidLogoutToken("header or payload is not JSON")
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise InvalidLogoutToken("header or payload is not a JSON object")

    alg = header.get("alg")
    if alg not in ALLOWED_ALGS:
        raise InvalidLogoutToken("alg %r not accepted" % (alg,))
    if "crit" in header:
        raise InvalidLogoutToken("crit header present; no extensions are supported")
    # A typ that is present must say logout+jwt even when not required: an ID token (typ JWT)
    # from the same realm, same key and same audience must never be accepted as a logout.
    if "typ" in header:
        if not _typ_is_logout(header["typ"]):
            raise InvalidLogoutToken("header typ %r is not %s" % (header["typ"], LOGOUT_TYP))
    elif require_typ:
        raise InvalidLogoutToken("header typ missing; %s required" % LOGOUT_TYP)
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise InvalidLogoutToken("kid is not a string")

    signature = b64url_decode(sig_b64)
    signing_input = (header_b64 + "." + payload_b64).encode("ascii")
    candidates = keys_for(kid)
    if not candidates:
        raise InvalidLogoutToken("no signing key for kid %r" % (kid,))
    verified = False
    for jwk in candidates:
        if jwk.get("use") not in (None, "sig"):
            continue
        if jwk.get("alg") not in (None, alg):
            continue
        try:
            key = public_key_from_jwk(jwk)
        except InvalidLogoutToken:
            continue
        if _verify(alg, key, signing_input, signature):
            verified = True
            break
    if not verified:
        raise InvalidLogoutToken("signature does not verify")

    if claims.get("iss") != issuer:
        raise InvalidLogoutToken("iss does not match the configured issuer")

    accepted = [audience] if isinstance(audience, str) else list(audience)
    aud = claims.get("aud")
    if isinstance(aud, str):
        aud = [aud]
    if not isinstance(aud, list) or not any(a in accepted for a in aud if isinstance(a, str)):
        raise InvalidLogoutToken("aud does not include the configured client id")

    events = claims.get("events")
    if not isinstance(events, dict) or not isinstance(events.get(BACKCHANNEL_EVENT), dict):
        raise InvalidLogoutToken("events does not carry the back-channel logout event")

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidLogoutToken("sub missing")
    if "sid" in claims and not isinstance(claims["sid"], str):
        raise InvalidLogoutToken("sid is not a string")
    if "nonce" in claims:
        raise InvalidLogoutToken("nonce present; a logout token must not carry one")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        raise InvalidLogoutToken("jti missing")

    now = time.time() if now is None else now
    iat = _number(claims, "iat")
    exp = _number(claims, "exp")
    if iat > now + skew_seconds:
        raise InvalidLogoutToken("iat is in the future")
    if now - iat > max_age_seconds + skew_seconds:
        raise InvalidLogoutToken("iat is too old")
    if now >= exp + skew_seconds:
        raise InvalidLogoutToken("token expired")
    return claims


def select_keys(jwks, kid):
    """The JWKs in a JWKS document that may have signed a token carrying this kid."""
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list):
        raise KeysUnavailable("JWKS has no keys array")
    keys = [k for k in keys if isinstance(k, dict)]
    if kid is None:
        return keys
    return [k for k in keys if k.get("kid") == kid]
