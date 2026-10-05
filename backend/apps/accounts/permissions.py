from rest_framework.permissions import BasePermission, SAFE_METHODS
from .models import WorkspaceMembership

def membership(user, workspace):
    if not user or not user.is_authenticated:
        return None
    return WorkspaceMembership.objects.filter(user=user, workspace=workspace).first()

def accessible_workspace_ids(user, roles=None):
    qs = WorkspaceMembership.objects.filter(user=user)
    if roles:
        qs = qs.filter(role__in=roles)
    return qs.values_list("workspace_id", flat=True)

class WorkspaceAccessMixin:
    """Filter all workspace-owned records before lookup; deny cross-workspace ids."""
    required_roles = None
    def workspace_allowed(self, workspace):
        m = membership(self.request.user, workspace)
        if not m:
            return False
        if self.required_roles and m.role not in self.required_roles:
            return False
        return True
