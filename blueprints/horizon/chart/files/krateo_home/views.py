"""GET /auth/home/ -- redirect to the first dashboard page this user can open.

"Can open" is exactly what Horizon enforces when the page is requested, applied ahead of time:
  - require_perms: the user has every permission of the dashboard AND of its default panel
    (`openstack.services.<type>` against the user's service catalog, `openstack.roles.<role>`);
  - require_component_access: dashboard.can_access and panel.can_access for this request (policy
    rules, and dashboard-specific rules such as the Project dashboard needing a project scope).
The default dashboard is tried first, so a deployment where it is reachable is unchanged; the
others follow in Horizon's own order (HORIZON_CONFIG['dashboards']).
"""
import logging

from django import shortcuts
from django.conf import settings

LOG = logging.getLogger(__name__)


def _permissions(component):
    return set(getattr(component, "permissions", None) or ())


def can_open(request, dashboard):
    """True if the user may open `dashboard`'s default page, by Horizon's dispatch checks."""
    try:
        panel = dashboard.get_panel(dashboard.default_panel)
    except Exception:
        # A dashboard whose default panel is not registered has no page to open.
        return False
    if not request.user.has_perms(_permissions(dashboard) | _permissions(panel)):
        return False
    context = {"request": request}
    return bool(dashboard.can_access(context) and panel.can_access(context))


def candidates(horizon):
    default = horizon.get_default_dashboard()
    return [default] + [d for d in horizon.get_dashboards() if d is not default]


def first_openable_url(request, dashboards):
    for dashboard in dashboards:
        if can_open(request, dashboard):
            return dashboard.get_absolute_url()
    return None


def home(request):
    if not request.user.is_authenticated:
        return shortcuts.redirect(settings.LOGIN_URL)
    import horizon
    url = first_openable_url(request, candidates(horizon))
    if url is None:
        # Nothing this user may open: say so, as Horizon does for a forbidden page.
        from horizon import exceptions
        LOG.warning("home page: user %s can open no dashboard's default page", request.user.id)
        raise exceptions.NotAuthorized("You are not authorized to open any dashboard.")
    return shortcuts.redirect(url)
