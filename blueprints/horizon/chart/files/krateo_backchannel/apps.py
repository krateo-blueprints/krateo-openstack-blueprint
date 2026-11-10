"""Records, at every Horizon login, which federated user the session belongs to."""
import logging

from django.apps import AppConfig

LOG = logging.getLogger(__name__)


def _on_login(sender, request, user, **kwargs):
    from . import logout
    try:
        logout.record_subject(logout.from_django_settings(), request.session, user)
    except Exception as e:
        # Never block a login. A session recorded without its subject is not reachable by a
        # back-channel logout, so this is logged as an error, not a warning.
        LOG.error("back-channel logout: could not record the subject at login (%s: %s)",
                  e.__class__.__name__, e)


class KrateoBackchannelConfig(AppConfig):
    default = True
    # The package is importable as openstack_dashboard.local.krateo_backchannel in Horizon and
    # as krateo_backchannel in the tests; derive the name instead of hard-coding either.
    name = __name__.rpartition(".")[0]
    label = "krateo_backchannel"
    verbose_name = "OIDC back-channel logout"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in
        user_logged_in.connect(_on_login, dispatch_uid="krateo_backchannel_record_subject")
