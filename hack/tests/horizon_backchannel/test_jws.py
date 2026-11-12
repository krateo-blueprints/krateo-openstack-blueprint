"""The logout-token validator: one valid Keycloak-shaped token, and every rejection."""
import time
import unittest

import support
from support import DROP

from krateo_backchannel import jws

NOW = 1_800_000_000


def check(token, now=NOW, keys=None, **kw):
    args = {
        "issuer": support.REALM,
        "audience": support.CLIENT_ID,
        "keys_for": keys or support.static_keys(),
        "now": now,
        "skew_seconds": 60,
        "max_age_seconds": 300,
        "require_typ": True,
    }
    args.update(kw)
    return jws.validate(token, **args)


class Valid(unittest.TestCase):

    def test_keycloak_shaped_token_is_accepted(self):
        claims = check(support.keycloak_token(now=NOW))
        self.assertEqual(claims["sub"], support.USER_SUB)
        self.assertEqual(claims["aud"], support.CLIENT_ID)  # a string, as Keycloak sends it
        self.assertIn("sid", claims)
        self.assertEqual(claims["exp"] - claims["iat"], 120)

    def test_aud_as_a_list_is_accepted(self):
        check(support.keycloak_token(now=NOW, aud=["other", support.CLIENT_ID]))

    def test_sid_is_optional(self):
        check(support.keycloak_token(now=NOW, sid=DROP))

    def test_typ_with_media_type_prefix_is_accepted(self):
        check(support.keycloak_token(now=NOW, header={"typ": "application/logout+JWT"}))

    def test_missing_typ_is_accepted_only_when_not_required(self):
        token = support.keycloak_token(now=NOW, header={"typ": DROP})
        check(token, require_typ=False)
        with self.assertRaises(jws.InvalidLogoutToken):
            check(token)

    def test_es256_token_is_accepted(self):
        check(support.keycloak_token(now=NOW, header={"kid": support.EC_KID},
                                     key=support.EC_KEY, alg="ES256"))

    def test_token_without_kid_is_tried_against_every_signing_key(self):
        check(support.keycloak_token(now=NOW, header={"kid": DROP}))

    def test_within_skew_is_accepted(self):
        check(support.keycloak_token(now=NOW + 50))           # iat 50s in the future
        check(support.keycloak_token(now=NOW - 170))          # exp passed 50s ago


class Rejected(unittest.TestCase):

    def assertRejected(self, token, **kw):
        with self.assertRaises(jws.InvalidLogoutToken):
            check(token, **kw)

    # -- issuer
    def test_iss_reflected_from_another_host(self):
        # Keycloak reflects the triggering request's host into iss when it has no fixed
        # hostname; a token minted via 127.0.0.1 must not match the configured issuer.
        self.assertRejected(support.keycloak_token(
            now=NOW, iss="http://127.0.0.1:8080/realms/openstack"))

    def test_iss_other_realm(self):
        self.assertRejected(support.keycloak_token(
            now=NOW, iss="https://keycloak.example.test/realms/master"))

    def test_iss_missing(self):
        self.assertRejected(support.keycloak_token(now=NOW, iss=DROP))

    # -- audience
    def test_aud_other_client(self):
        self.assertRejected(support.keycloak_token(now=NOW, aud="horizon"))

    def test_aud_list_without_client(self):
        self.assertRejected(support.keycloak_token(now=NOW, aud=["a", "b"]))

    def test_aud_missing(self):
        self.assertRejected(support.keycloak_token(now=NOW, aud=DROP))

    # -- events, sub, nonce, jti
    def test_events_missing(self):
        self.assertRejected(support.keycloak_token(now=NOW, events=DROP))

    def test_events_without_backchannel_event(self):
        self.assertRejected(support.keycloak_token(
            now=NOW, events={"http://schemas.openid.net/event/other": {}}))

    def test_events_not_an_object(self):
        self.assertRejected(support.keycloak_token(now=NOW, events=[support.EVENT]))

    def test_sub_missing(self):
        self.assertRejected(support.keycloak_token(now=NOW, sub=DROP))

    def test_sub_empty(self):
        self.assertRejected(support.keycloak_token(now=NOW, sub=""))

    def test_nonce_present(self):
        self.assertRejected(support.keycloak_token(now=NOW, nonce="n-0S6_WzA2Mj"))

    def test_jti_missing(self):
        self.assertRejected(support.keycloak_token(now=NOW, jti=DROP))

    def test_sid_not_a_string(self):
        self.assertRejected(support.keycloak_token(now=NOW, sid=7))

    # -- time
    def test_exp_expired(self):
        # Issued 190s ago with Keycloak's 120s lifetime: exp passed 70s ago, beyond 60s skew.
        self.assertRejected(support.keycloak_token(now=NOW - 190))

    def test_exp_missing(self):
        self.assertRejected(support.keycloak_token(now=NOW, exp=DROP))

    def test_iat_in_the_future(self):
        self.assertRejected(support.keycloak_token(now=NOW + 61, exp=NOW + 600))

    def test_iat_too_old_even_with_long_exp(self):
        self.assertRejected(support.keycloak_token(now=NOW - 400, exp=NOW + 600))

    def test_iat_missing(self):
        self.assertRejected(support.keycloak_token(now=NOW, iat=DROP))

    def test_iat_boolean(self):
        self.assertRejected(support.keycloak_token(now=NOW, iat=True))

    # -- header
    def test_header_typ_jwt(self):
        # An ID token from the same realm, key and audience carries typ JWT.
        self.assertRejected(support.keycloak_token(now=NOW, header={"typ": "JWT"}))

    def test_header_typ_jwt_rejected_even_when_typ_not_required(self):
        self.assertRejected(support.keycloak_token(now=NOW, header={"typ": "JWT"}),
                            require_typ=False)

    def test_alg_none(self):
        self.assertRejected(support.keycloak_token(now=NOW, header={"alg": "none"}, alg="none"))

    def test_alg_hs256_keyed_with_public_key(self):
        self.assertRejected(support.keycloak_token(now=NOW, header={"alg": "HS256"}, alg="HS256"))

    def test_crit_header(self):
        self.assertRejected(support.keycloak_token(now=NOW, header={"crit": ["exp"]}))

    # -- signature and keys
    def test_signed_by_another_key(self):
        self.assertRejected(support.keycloak_token(now=NOW, key=support.OTHER_RSA_KEY))

    def test_unknown_kid(self):
        self.assertRejected(support.keycloak_token(now=NOW, header={"kid": "rotated-away"}))

    def test_key_marked_for_encryption_is_not_used(self):
        self.assertRejected(support.keycloak_token(
            now=NOW, header={"kid": "kc-rsa-enc"}, key=support.OTHER_RSA_KEY))

    def test_rs256_header_on_ec_key(self):
        self.assertRejected(support.keycloak_token(
            now=NOW, header={"kid": support.EC_KID}, key=support.RSA_KEY))

    def test_tampered_claims(self):
        good = support.keycloak_token(now=NOW).split(".")
        forged = support.keycloak_token(now=NOW, sub="someone-else").split(".")
        self.assertRejected(".".join([good[0], forged[1], good[2]]))

    def test_empty_jwks(self):
        self.assertRejected(support.keycloak_token(now=NOW),
                            keys=support.static_keys({"keys": []}))

    # -- shape
    def test_not_three_segments(self):
        self.assertRejected("a.b")

    def test_payload_not_json(self):
        token = support.keycloak_token(now=NOW).split(".")
        self.assertRejected(".".join([token[0], support.b64url(b"not json"), token[2]]))

    def test_not_base64url(self):
        token = support.keycloak_token(now=NOW).split(".")
        self.assertRejected(".".join([token[0] + "+/", token[1], token[2]]))

    def test_not_a_string(self):
        self.assertRejected(None)


class JwksDocument(unittest.TestCase):

    def test_jwks_without_keys_array_is_unavailable_not_invalid(self):
        with self.assertRaises(jws.KeysUnavailable):
            jws.select_keys({"nope": []}, support.KID)

    def test_real_clock_default(self):
        check(support.keycloak_token(), now=None)
        self.assertLess(abs(time.time() - support.keycloak_claims()["iat"]), 5)


if __name__ == "__main__":
    unittest.main()
