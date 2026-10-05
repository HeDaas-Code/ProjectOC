from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import WorkspaceInvite, WorkspaceMembership


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        fields = ["id", "username", "email"]


class MembershipSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = WorkspaceMembership
        fields = ["id", "workspace", "user", "username", "email", "role", "created_at"]
        read_only_fields = ["id", "user", "username", "email", "created_at"]


class InviteSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(source="created_by.username", read_only=True)
    class Meta:
        model = WorkspaceInvite
        fields = [
            "id", "workspace", "email", "role", "expires_at", "accepted_at",
            "created_at", "created_by_username",
        ]
        read_only_fields = fields
