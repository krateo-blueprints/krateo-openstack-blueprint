"""Appended to AUTHENTICATION_URLS, which Horizon mounts under ^auth/."""
from django.urls import path

from . import views

urlpatterns = [
    path("backchannel-logout/", views.backchannel_logout, name="krateo_backchannel_logout"),
]
