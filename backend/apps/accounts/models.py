import uuid
from django.conf import settings
from django.db import models

class SetupState(models.Model):
    """Singleton row used to serialize the one-time owner bootstrap."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    owner_created = models.BooleanField(default=False)

class WorkspaceMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        EDITOR = "editor", "Editor"
        READER = "reader", "Reader"
    workspace = models.ForeignKey("core.WorldWorkspace", on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="workspace_memberships")
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.READER)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "user"], name="unique_workspace_member")]

class WorkspaceInvite(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("core.WorldWorkspace", on_delete=models.CASCADE, related_name="invites")
    # Optional email makes this a shareable invite link; when present it
    # still acts as an additional identity guard.
    email = models.EmailField(blank=True, default="")
    role = models.CharField(max_length=12, choices=[("editor", "Editor"), ("reader", "Reader")])
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
