from django.urls import path
from .views import AgentAnalysisView, AgentEvidenceView, AgentRunView, AgentRunEventsView, AgentCancelView, DialogueMemoryView, MaintenanceProposalView, MaintenanceReviewView, ProviderListView, ProviderModelsView, SendDialogueMessageView

urlpatterns = [
    path("workspaces/<uuid:workspace_id>/maintenance-review/", MaintenanceReviewView.as_view(), name="maintenance-review"),
    path("workspaces/<uuid:workspace_id>/maintenance-proposals/", MaintenanceProposalView.as_view(), name="maintenance-proposal"),
    path("ai/providers/", ProviderListView.as_view(), name="ai-providers"),
    path("ai/providers/<str:provider_id>/models/", ProviderModelsView.as_view(), name="ai-provider-models"),
    path("dialogue/sessions/<uuid:session_id>/messages/", SendDialogueMessageView.as_view(), name="send-dialogue-message"),
    path("dialogue/sessions/<uuid:session_id>/memory/", DialogueMemoryView.as_view(), name="dialogue-memory"),
    path("dialogue/sessions/<uuid:session_id>/analysis/", AgentAnalysisView.as_view(), name="agent-analysis"),
    path("agent/runs/<uuid:run_id>/", AgentRunView.as_view(), name="agent-run"),
    path("agent/runs/<uuid:run_id>/evidence/", AgentEvidenceView.as_view(), name="agent-evidence"),
    path("agent/runs/<uuid:run_id>/events/", AgentRunEventsView.as_view(), name="agent-events"),
    path("agent/runs/<uuid:run_id>/cancel/", AgentCancelView.as_view(), name="agent-cancel"),
]
