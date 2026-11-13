"""Appended to AUTHENTICATION_URLS, which Horizon mounts under ^auth/."""
from django.urls import path

from . import views

urlpatterns = [
    path("home/", views.home, name="krateo_home"),
]
