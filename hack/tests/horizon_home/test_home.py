"""GET /auth/home/ through Django, with Horizon's dashboard registry faked: the redirect is the
first dashboard whose default page the user may open by Horizon's dispatch checks (require_perms on
dashboard + panel permissions, then dashboard.can_access and panel.can_access), default first."""
import sys
import types
import unittest

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="gate-not-a-secret",
        ROOT_URLCONF=__name__,
        LOGIN_URL="/auth/login/",
        ALLOWED_HOSTS=["*"],
        INSTALLED_APPS=[],
    )
    django.setup()

from django.test import RequestFactory  # noqa: E402
from django.urls import include, path, resolve  # noqa: E402

# Horizon mounts AUTHENTICATION_URLS under ^auth/ (openstack_dashboard/urls.py).
urlpatterns = [path("auth/", include("krateo_home.urls"))]

from krateo_home import views  # noqa: E402


class NotAuthorized(Exception):
    pass


class Component(object):
    def __init__(self, slug, permissions=(), accessible=True):
        self.slug = slug
        self.permissions = tuple(permissions)
        self.accessible = accessible
        self.contexts = []

    def can_access(self, context):
        self.contexts.append(context)
        return self.accessible


class Dashboard(Component):
    def __init__(self, slug, panel, permissions=(), accessible=True, registered=True):
        super().__init__(slug, permissions, accessible)
        self.panel = panel
        self.default_panel = panel.slug
        self.registered = registered

    def get_panel(self, slug):
        if not self.registered or slug != self.panel.slug:
            raise LookupError("panel %s not registered" % slug)
        return self.panel

    def get_absolute_url(self):
        return "/%s/" % self.slug


class User(object):
    def __init__(self, perms, authenticated=True):
        self.perms = set(perms)
        self.is_authenticated = authenticated
        self.id = "u-1"
        self.asked = []

    def has_perms(self, required):
        self.asked.append(set(required))
        return set(required) <= self.perms


def fake_horizon(dashboards, default=None):
    mod = types.ModuleType("horizon")
    mod.get_dashboards = lambda: list(dashboards)
    mod.get_default_dashboard = lambda: default or dashboards[0]
    mod.exceptions = types.SimpleNamespace(NotAuthorized=NotAuthorized)
    exc = types.ModuleType("horizon.exceptions")
    exc.NotAuthorized = NotAuthorized
    return mod, exc


COMPUTE = "openstack.services.compute"
IDENTITY = "openstack.services.identity"
ADMIN = "openstack.roles.admin"


def standard(project_scoped=True):
    """Horizon's stock order: Project (Overview needs compute), Admin, Identity, Settings."""
    return [
        Dashboard("project", Component("overview", [COMPUTE]), accessible=project_scoped),
        Dashboard("admin", Component("overview", [COMPUTE]), permissions=[ADMIN]),
        Dashboard("identity", Component("projects", [IDENTITY])),
        Dashboard("settings", Component("user")),
    ]


class Home(unittest.TestCase):

    def setUp(self):
        self.saved = {k: sys.modules.get(k) for k in ("horizon", "horizon.exceptions")}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def get(self, dashboards, user, default=None):
        mod, exc = fake_horizon(dashboards, default)
        sys.modules["horizon"], sys.modules["horizon.exceptions"] = mod, exc
        request = RequestFactory().get("/auth/home/")
        request.user = user
        return views.home(request)

    def assertRedirect(self, resp, url):
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], url)

    def test_routed_under_auth(self):
        self.assertIs(resolve("/auth/home/").func, views.home)

    def test_with_compute_the_home_is_unchanged(self):
        self.assertRedirect(self.get(standard(), User([COMPUTE, IDENTITY])), "/project/")

    def test_without_compute_the_home_is_identity(self):
        # The 403 of #247: profile identity, a member with a project-scoped token.
        self.assertRedirect(self.get(standard(), User([IDENTITY])), "/identity/")

    def test_permissions_of_dashboard_and_panel_are_both_required(self):
        user = User([COMPUTE, IDENTITY])
        self.get(standard(), user)
        self.assertEqual(user.asked[0], {COMPUTE})
        admin_first = [standard()[1]] + standard()
        user = User([COMPUTE, IDENTITY])
        self.assertRedirect(self.get(admin_first, user, default=admin_first[0]), "/project/")
        self.assertEqual(user.asked[0], {ADMIN, COMPUTE})

    def test_component_access_is_checked_with_the_request(self):
        dashboards = standard(project_scoped=False)   # e.g. a domain-scoped token
        self.assertRedirect(self.get(dashboards, User([COMPUTE, IDENTITY])), "/identity/")
        self.assertIn("request", dashboards[0].contexts[0])

    def test_panel_access_is_checked(self):
        dashboards = standard()
        dashboards[2].panel.accessible = False          # policy denies Identity > Projects
        self.assertRedirect(self.get(dashboards, User([IDENTITY])), "/settings/")

    def test_unregistered_default_panel_is_skipped(self):
        dashboards = standard()
        dashboards[2].registered = False
        self.assertRedirect(self.get(dashboards, User([IDENTITY])), "/settings/")

    def test_default_dashboard_is_tried_first(self):
        dashboards = standard()
        self.assertRedirect(self.get(dashboards, User([COMPUTE, IDENTITY]), default=dashboards[2]),
                            "/identity/")

    def test_nothing_openable_is_not_authorized(self):
        dashboards = [Dashboard("project", Component("overview", [COMPUTE]))]
        with self.assertRaises(NotAuthorized):
            self.get(dashboards, User([IDENTITY]))

    def test_anonymous_goes_to_login(self):
        self.assertRedirect(self.get(standard(), User([], authenticated=False)), "/auth/login/")


if __name__ == "__main__":
    unittest.main()
