from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    BootstrapView,
    InviteAcceptView,
    InviteDetailView,
    InviteSignupView,
    LoginView,
    LogoutView,
    MeView,
    WorkspaceMembersViewSet,
    csrf_cookie,
)

router = DefaultRouter()
router.register("members", WorkspaceMembersViewSet, basename="members")

urlpatterns = [
    path("auth/csrf/", csrf_cookie),
    path("auth/bootstrap/", BootstrapView.as_view()),
    path("auth/login/", LoginView.as_view()),
    path("auth/logout/", LogoutView.as_view()),
    path("auth/me/", MeView.as_view()),
    path("auth/invites/accept/", InviteAcceptView.as_view()),
    path("auth/invites/signup/", InviteSignupView.as_view()),
    path("auth/invites/<str:token>/", InviteDetailView.as_view()),
    # Backward-compatible invite endpoints used by older clients.
    path("invites/accept/", InviteAcceptView.as_view()),
    path("invites/signup/", InviteSignupView.as_view()),
    path("invites/<str:token>/", InviteDetailView.as_view()),
    path("", include(router.urls)),
]
