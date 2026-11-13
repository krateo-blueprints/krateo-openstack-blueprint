"""Krateo blueprint addition to Horizon: a home page the user can open (issue #247).

Installed by the chart into openstack_dashboard/local/krateo_home. Horizon sends a user to the
default dashboard's default panel after login, whether or not the deployment runs what that panel
needs: on a deployment without compute, the Project dashboard's Overview panel requires
`openstack.services.compute` and every login landed on 403. HORIZON_CONFIG['user_home'] points at
GET /auth/home/ instead, which redirects to the first dashboard whose default page this user can
open, by Horizon's own checks. See the horizon chart's templates/_krateo_home.tpl.
"""
