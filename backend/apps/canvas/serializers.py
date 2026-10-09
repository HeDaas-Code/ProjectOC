from rest_framework import serializers
from apps.core.models import Entity, Relation
from .models import CanvasContainer, DialogueMessage, DialogueSession, EntityCanvasReference, EntityProposal, RelationProposal, StagingCanvas


class CanvasContainerSerializer(serializers.ModelSerializer):
    children = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    canvas_name = serializers.CharField(source="canvas.name", read_only=True)

    class Meta:
        model = CanvasContainer
        fields = ["id", "workspace", "branch", "parent", "canvas", "canvas_name", "name", "sort_order", "status", "children", "created_at", "updated_at"]
        read_only_fields = ["id", "branch", "canvas", "canvas_name", "children", "created_at", "updated_at"]


class EntityCanvasReferenceSerializer(serializers.ModelSerializer):
    entity_title = serializers.CharField(source="entity.title", read_only=True)
    container_name = serializers.CharField(source="container.name", read_only=True)

    class Meta:
        model = EntityCanvasReference
        fields = ["id", "workspace", "branch", "container", "container_name", "entity", "entity_title", "created_at"]
        read_only_fields = ["id", "workspace", "branch", "container_name", "entity_title", "created_at"]


class StagingCanvasSerializer(serializers.ModelSerializer):
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    proposal_count = serializers.SerializerMethodField()

    def validate(self, attrs):
        workspace = attrs.get("workspace", getattr(self.instance, "workspace", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        if branch and workspace and branch.workspace_id != workspace.id:
            raise serializers.ValidationError({"branch": "分支必须属于同一世界观"})
        return attrs

    def get_proposal_count(self, obj):
        return obj.entity_proposals.filter(status=EntityProposal.Status.PENDING).count()

    class Meta:
        model = StagingCanvas
        fields = [
            "id", "purpose", "workspace", "branch", "workspace_name", "name", "snapshot", "sync_metadata", "snapshot_version",
            "snapshot_operation_cursor", "operation_compacted_through",
            "status", "last_saved_at", "proposal_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "purpose", "sync_metadata", "snapshot_version", "snapshot_operation_cursor", "operation_compacted_through", "last_saved_at", "proposal_count", "created_at", "updated_at"]


class RelationProposalSerializer(serializers.ModelSerializer):
    source_entity_title = serializers.CharField(source="source_entity.title", read_only=True)
    source_title = serializers.SerializerMethodField()
    target_title = serializers.SerializerMethodField()
    relation_type_label = serializers.CharField(source="get_relation_type_display", read_only=True)

    class Meta:
        model = RelationProposal
        fields = [
            "id", "source_entity", "source_entity_title", "source_proposal", "source_title", "target_proposal", "target_entity", "target_title",
            "relation_type", "relation_type_label", "properties", "confidence", "reason",
            "time_system", "valid_from", "valid_to",
            "status", "created_relation", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "source_entity_title", "source_title", "target_title", "created_relation", "created_at", "updated_at"]

    def get_source_title(self, obj):
        return obj.source_entity.title if obj.source_entity_id else (obj.source_proposal.title if obj.source_proposal_id else None)

    def get_target_title(self, obj):
        if obj.target_proposal_id:
            return obj.target_proposal.title
        if obj.target_entity_id:
            return obj.target_entity.title
        return None

    def validate(self, attrs):
        """Keep a proposal's two endpoints in one canvas and one workspace.

        This belongs in the serializer rather than a post-class monkey patch so
        create and partial-update requests use the same validation path.  For a
        PATCH, an explicitly supplied null clears the other target; omitted
        fields keep their existing value.
        """
        instance = self.instance
        source_proposal = attrs.get("source_proposal", getattr(instance, "source_proposal", None))
        source_entity = attrs.get("source_entity", getattr(instance, "source_entity", None))
        if bool(source_proposal) == bool(source_entity):
            raise serializers.ValidationError({"source_proposal": "必须且只能指定一个源提案或正式实体", "source_entity": "必须且只能指定一个源提案或正式实体"})
        source = source_proposal or source_entity

        target_proposal_supplied = "target_proposal" in attrs
        target_entity_supplied = "target_entity" in attrs
        if target_proposal_supplied or target_entity_supplied:
            target_proposal = attrs.get("target_proposal") if target_proposal_supplied else getattr(instance, "target_proposal", None)
            target_entity = attrs.get("target_entity") if target_entity_supplied else getattr(instance, "target_entity", None)
        else:
            target_proposal = getattr(instance, "target_proposal", None)
            target_entity = getattr(instance, "target_entity", None)

        # On PATCH, changing one endpoint should clear the other endpoint in
        # the view before save.  A payload that explicitly sends both is never
        # valid, even when one is null.
        if bool(target_proposal) == bool(target_entity):
            raise serializers.ValidationError({
                "target_proposal": "必须且只能指定一个目标提案或正式实体",
                "target_entity": "必须且只能指定一个目标提案或正式实体",
            })

        target = target_proposal or target_entity
        if target.workspace_id != source.workspace_id:
            raise serializers.ValidationError("关系两端必须属于同一世界观")
        source_canvas_id = getattr(source, "canvas_id", None)
        target_canvas_id = getattr(target, "canvas_id", None)
        if target_proposal and source_proposal and target_canvas_id != source_canvas_id:
            raise serializers.ValidationError("关系两端的提案必须属于同一画布")
        if source_entity and source_entity.workspace_id != target.workspace_id:
            raise serializers.ValidationError("关系两端必须属于同一世界观")

        time_system = attrs.get("time_system", getattr(instance, "time_system", None))
        if time_system and time_system.workspace_id != source.workspace_id:
            raise serializers.ValidationError({"time_system": "必须属于关系所属的世界观"})
        valid_from = attrs.get("valid_from", getattr(instance, "valid_from", None))
        valid_to = attrs.get("valid_to", getattr(instance, "valid_to", None))
        if valid_from is not None and valid_to is not None and valid_to < valid_from:
            raise serializers.ValidationError({"valid_to": "必须大于或等于 valid_from"})

        if attrs.get("status") == RelationProposal.Status.ACCEPTED:
            raise serializers.ValidationError({"status": "接受关系请通过预览提交"})
        return attrs


class EntityProposalSerializer(serializers.ModelSerializer):
    entity_type_label = serializers.CharField(source="get_entity_type_display", read_only=True)
    source_label = serializers.CharField(source="get_source_display", read_only=True)
    suggested_relations = RelationProposalSerializer(many=True, read_only=True)
    canvas_name = serializers.CharField(source="canvas.name", read_only=True)

    class Meta:
        model = EntityProposal
        fields = [
            "id", "workspace", "canvas", "canvas_name", "dialogue_session", "source_message",
            "source", "source_label", "operation", "target_entity", "entity_type", "entity_type_label", "title", "content",
            "metadata", "conflicts", "status", "created_entity", "suggested_relations",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "canvas_name", "source_label", "entity_type_label", "created_entity",
            "suggested_relations", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        canvas = attrs.get("canvas", getattr(self.instance, "canvas", None))
        workspace = attrs.get("workspace", getattr(self.instance, "workspace", None))
        if canvas is None or workspace is None:
            raise serializers.ValidationError("提案必须绑定世界观和画布")
        if canvas.workspace_id != workspace.id:
            raise serializers.ValidationError("画布与世界观不匹配")
        if canvas.purpose != StagingCanvas.Purpose.STAGING:
            raise serializers.ValidationError("世界观图谱不能绑定灵感对话")
        return attrs


class DialogueMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = DialogueMessage
        fields = ["id", "session", "role", "content", "intent", "extracted_data", "created_at"]
        read_only_fields = ["id", "created_at"]


class DialogueSessionSerializer(serializers.ModelSerializer):
    messages = DialogueMessageSerializer(many=True, read_only=True)
    message_count = serializers.IntegerField(source="messages.count", read_only=True)

    class Meta:
        model = DialogueSession
        fields = [
            "id", "workspace", "canvas", "provider_id", "model", "title", "context",
            "status", "message_count", "messages", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "message_count", "messages", "created_at", "updated_at"]

    def validate(self, attrs):
        canvas = attrs.get("canvas", getattr(self.instance, "canvas", None))
        workspace = attrs.get("workspace", getattr(self.instance, "workspace", None))
        if canvas is None or workspace is None:
            raise serializers.ValidationError("对话必须绑定同一世界观的画布")
        if canvas.workspace_id != workspace.id:
            raise serializers.ValidationError("对话画布与世界观不匹配")
        if attrs.get("provider_id", getattr(self.instance, "provider_id", "default")) != "default":
            raise serializers.ValidationError({"provider_id": "未知 provider"})
        return attrs
