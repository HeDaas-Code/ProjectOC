from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import CanvasContainerViewSet, CanvasViewSet, DialogueMessageViewSet, DialogueSessionViewSet, EntityCanvasReferenceViewSet, EntityProposalViewSet, RelationProposalViewSet

router = DefaultRouter()
router.register("canvases", CanvasViewSet, basename="canvas")
router.register("canvas-containers", CanvasContainerViewSet, basename="canvas-container")
router.register("entity-canvas-references", EntityCanvasReferenceViewSet, basename="entity-canvas-reference")
router.register("proposals", EntityProposalViewSet, basename="proposal")
router.register("relation-proposals", RelationProposalViewSet, basename="relation-proposal")
router.register("dialogue/sessions", DialogueSessionViewSet, basename="dialogue-session")
router.register("dialogue/messages", DialogueMessageViewSet, basename="dialogue-message")

urlpatterns = [path("", include(router.urls))]
