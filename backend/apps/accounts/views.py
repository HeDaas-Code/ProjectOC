import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import WorldWorkspace
from .models import SetupState, WorkspaceInvite, WorkspaceMembership
from .serializers import InviteSerializer, MembershipSerializer, UserSerializer


def _invite_payload(request, invite, raw_token=None):
    payload = InviteSerializer(invite).data
    payload["workspace_name"] = invite.workspace.name
    payload["invite_url"] = f"/invite/{raw_token}" if raw_token else None
    # Keep the legacy API contract for existing clients/tests; the UI uses invite_url.
    payload["token"] = raw_token if raw_token else None
    return payload


def _owner_membership(user, workspace_id):
    return WorkspaceMembership.objects.filter(
        workspace_id=workspace_id, user=user, role=WorkspaceMembership.Role.OWNER
    ).exists()


class BootstrapView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        state = SetupState.objects.filter(pk=1).first()
        return Response({"owner_exists": bool(state and state.owner_created) or get_user_model().objects.exists()})

    def post(self, request):
        username = str(request.data.get("username", "")).strip()
        password = str(request.data.get("password", ""))
        email = str(request.data.get("email", "")).strip()
        if not username or len(password) < 12:
            return Response({"detail": "用户名必填，密码至少 12 位"}, status=400)
        with transaction.atomic():
            state, _ = SetupState.objects.select_for_update().get_or_create(pk=1)
            if state.owner_created or get_user_model().objects.exists():
                return Response({"detail": "首个 owner 已创建，公开注册已关闭"}, status=409)
            user = get_user_model().objects.create_user(username=username, email=email, password=password)
            state.owner_created = True
            state.save(update_fields=["owner_created"])
            for workspace in WorldWorkspace.objects.all():
                WorkspaceMembership.objects.get_or_create(workspace=workspace, user=user, defaults={"role": "owner"})
        login(request, user)
        return Response({"user": UserSerializer(user).data}, status=201)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        user = authenticate(request, username=request.data.get("username", ""), password=request.data.get("password", ""))
        if not user:
            return Response({"detail": "用户名或密码错误"}, status=400)
        login(request, user)
        return Response({"user": UserSerializer(user).data})


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response(status=204)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class InviteDetailView(APIView):
    """Public preview used before the invite recipient creates an account."""

    permission_classes = [AllowAny]

    def get(self, request, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        invite = WorkspaceInvite.objects.filter(
            token_hash=digest, accepted_at__isnull=True, expires_at__gt=timezone.now()
        ).select_related("workspace").first()
        if not invite:
            return Response({"detail": "邀请无效或已过期"}, status=404)
        return Response(_invite_payload(request, invite))


class InviteAcceptView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        raw = str(request.data.get("token", ""))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with transaction.atomic():
            invite = WorkspaceInvite.objects.select_for_update().filter(
                token_hash=digest, accepted_at__isnull=True, expires_at__gt=timezone.now()
            ).select_related("workspace").first()
            if not invite:
                return Response({"detail": "邀请无效或已过期"}, status=400)
            if invite.email and invite.email.lower() != request.user.email.lower():
                return Response({"detail": "邀请邮箱与当前账户不匹配"}, status=403)
            WorkspaceMembership.objects.update_or_create(
                workspace=invite.workspace, user=request.user, defaults={"role": invite.role}
            )
            invite.accepted_at = timezone.now()
            invite.save(update_fields=["accepted_at"])
        return Response({"workspace": str(invite.workspace_id), "role": invite.role}, status=201)


class WorkspaceMembersViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def list(self, request):
        wid = request.query_params.get("workspace")
        if not WorkspaceMembership.objects.filter(workspace_id=wid, user=request.user).exists():
            return Response(status=404)
        return Response(MembershipSerializer(
            WorkspaceMembership.objects.filter(workspace_id=wid).select_related("user"), many=True
        ).data)

    def create(self, request):
        """Create a shareable invite link. Email is optional by design."""
        wid = request.data.get("workspace")
        if not _owner_membership(request.user, wid):
            return Response({"detail": "只有 owner 可以创建邀请"}, status=403)
        email = str(request.data.get("email", "")).strip().lower()
        role = request.data.get("role", "reader")
        try:
            expires_days = int(request.data.get("expires_days", 7))
        except (TypeError, ValueError):
            expires_days = 7
        if role not in ("reader", "editor"):
            return Response({"detail": "角色必须是 reader 或 editor"}, status=400)
        if not 1 <= expires_days <= 30:
            return Response({"detail": "邀请有效期必须在 1 到 30 天之间"}, status=400)
        raw = secrets.token_urlsafe(32)
        invite = WorkspaceInvite.objects.create(
            workspace_id=wid,
            email=email,
            role=role,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=expires_days),
            created_by=request.user,
        )
        return Response(_invite_payload(request, invite, raw), status=201)

    @action(detail=False, methods=["get"], url_path="invites")
    def invites(self, request):
        wid = request.query_params.get("workspace")
        if not _owner_membership(request.user, wid):
            return Response(status=403)
        qs = WorkspaceInvite.objects.filter(workspace_id=wid).select_related("workspace").order_by("-created_at")
        return Response([_invite_payload(request, invite) for invite in qs])

    @action(detail=False, methods=["delete"], url_path=r"invites/(?P<invite_id>[^/.]+)")
    def revoke_invite(self, request, invite_id=None):
        invite = WorkspaceInvite.objects.filter(id=invite_id).first()
        if not invite:
            return Response(status=404)
        if not _owner_membership(request.user, invite.workspace_id):
            return Response(status=403)
        invite.delete()
        return Response(status=204)

    @action(detail=True, methods=["patch", "delete"], url_path=r"(?P<member_id>[^/.]+)")
    def member(self, request, pk=None, member_id=None):
        m = WorkspaceMembership.objects.filter(workspace_id=pk, user_id=member_id).first()
        if not m:
            return Response(status=404)
        if not _owner_membership(request.user, pk):
            return Response(status=403)
        if request.method == "DELETE":
            if m.role == "owner" and WorkspaceMembership.objects.filter(workspace_id=pk, role="owner").count() <= 1:
                return Response({"detail": "不能移除最后一位 owner"}, status=400)
            m.delete()
            return Response(status=204)
        role = request.data.get("role")
        if role not in ("owner", "editor", "reader"):
            return Response({"detail": "角色无效"}, status=400)
        m.role = role
        m.save(update_fields=["role"])
        return Response(MembershipSerializer(m).data)


@ensure_csrf_cookie
def csrf_cookie(request):
    return JsonResponse({"csrfToken": get_token(request)})


class InviteSignupView(APIView):
    """Create an account only against a live invitation link."""

    permission_classes = [AllowAny]

    def post(self, request):
        raw = str(request.data.get("token", ""))
        username = str(request.data.get("username", "")).strip()
        email = str(request.data.get("email", "")).strip().lower()
        password = str(request.data.get("password", ""))
        if not username or len(password) < 12:
            return Response({"detail": "用户名必填，密码至少 12 位"}, status=400)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with transaction.atomic():
            invite = WorkspaceInvite.objects.select_for_update().filter(
                token_hash=digest, accepted_at__isnull=True, expires_at__gt=timezone.now()
            ).first()
            if not invite or (invite.email and invite.email.lower() != email):
                return Response({"detail": "邀请无效、邮箱不匹配或已过期"}, status=400)
            User = get_user_model()
            if User.objects.filter(username=username).exists() or (email and User.objects.filter(email__iexact=email).exists()):
                return Response({"detail": "用户名或邮箱已注册"}, status=409)
            user = User.objects.create_user(username=username, email=email, password=password)
            WorkspaceMembership.objects.create(workspace=invite.workspace, user=user, role=invite.role)
            invite.accepted_at = timezone.now()
            invite.save(update_fields=["accepted_at"])
        login(request, user)
        return Response({"user": UserSerializer(user).data, "workspace": str(invite.workspace_id), "role": invite.role}, status=201)
