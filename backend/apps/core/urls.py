from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (CharacterLifespanViewSet, CommitDiffView, CommitHistoryView, GitStatusView, CommitJobViewSet, EntityLinksView, EntityViewSet, GraphView, RelationViewSet, WorkspaceViewSet, WorldBranchViewSet, GraphPathView, GraphImpactView, ProjectionStatusView, ConsistencyReportView, TemporalDiffView, TemporalGraphSliceView, TimeSystemViewSet, TimeSystemConversionViewSet, TimelineEntryViewSet)

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("branches", WorldBranchViewSet, basename="branch")
router.register("entities", EntityViewSet, basename="entity")
router.register("relations", RelationViewSet, basename="relation")
router.register("commit-jobs", CommitJobViewSet, basename="commit-job")
router.register("time-systems", TimeSystemViewSet, basename="time-system")
router.register("time-system-conversions", TimeSystemConversionViewSet, basename="time-system-conversion")
router.register("timeline-entries", TimelineEntryViewSet, basename="timeline-entry")
router.register("character-lifespans", CharacterLifespanViewSet, basename="character-lifespan")

urlpatterns = [
    path("graph/", GraphView.as_view(), name="graph"),
    path("graph/path/", GraphPathView.as_view(), name="graph-path"),
    path("graph/impact/", GraphImpactView.as_view(), name="graph-impact"),
    path("graph/slice/", TemporalGraphSliceView.as_view(), name="graph-slice"),
    path("workspaces/<uuid:workspace_id>/graph/projection/", ProjectionStatusView.as_view(), name="graph-projection"),
    path("workspaces/<uuid:workspace_id>/consistency/", ConsistencyReportView.as_view(), name="consistency-report"),
    path("entities/<uuid:entity_id>/links/", EntityLinksView.as_view(), name="entity-links"),
    path("entities/<uuid:entity_id>/backlinks/", EntityLinksView.as_view(), name="entity-backlinks"),
    path("workspaces/<uuid:workspace_id>/git/history/", CommitHistoryView.as_view(), name="git-history"),
    path("workspaces/<uuid:workspace_id>/git/status/", GitStatusView.as_view(), name="git-status"),
    path("workspaces/<uuid:workspace_id>/git/diff/", CommitDiffView.as_view(), name="git-diff"),
    path("workspaces/<uuid:workspace_id>/git/temporal/", TemporalDiffView.as_view(), name="git-temporal"),
    path("workspaces/<uuid:workspace_id>/timelines/<uuid:timeline_id>/diff/", TemporalDiffView.as_view(), name="timeline-diff"),
    path("", include(router.urls)),
]
