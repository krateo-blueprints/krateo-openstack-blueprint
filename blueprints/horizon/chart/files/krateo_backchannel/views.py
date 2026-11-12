"""POST /auth/backchannel-logout/ -- the OIDC back-channel logout endpoint."""
import logging

from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import jws
from . import logout

LOG = logging.getLogger(__name__)


def _reply(status, text):
    resp = HttpResponse(text + "\n", status=status, content_type="text/plain")
    resp["Cache-Control"] = "no-store"
    return resp


@csrf_exempt
@never_cache
@require_POST
def backchannel_logout(request):
    try:
        cfg = logout.from_django_settings()
    except logout.NotConfigured as e:
        LOG.error("back-channel logout: %s", e)
        return _reply(503, "not configured")
    token = request.POST.get("logout_token")
    if not token:
        LOG.warning("back-channel logout rejected: no logout_token form field")
        return _reply(400, "invalid_request")
    try:
        claims = logout.validate(cfg, token)
    except jws.InvalidLogoutToken as e:
        LOG.warning("back-channel logout rejected: %s", e)
        return _reply(400, "invalid_request")
    except jws.KeysUnavailable as e:
        LOG.error("back-channel logout: cannot validate, %s", e)
        return _reply(503, "keys unavailable")
    LOG.info("back-channel logout: valid token, jti %s", claims.get("jti"))
    outcome = logout.end_sessions_and_forward(cfg, claims, token, logout.DjangoSessions())
    return _reply(outcome.status, "ok" if outcome.status == 200 else "logout incomplete")
