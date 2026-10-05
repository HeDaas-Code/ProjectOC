from rest_framework import serializers
from .models import Entity, Relation, WorldWorkspace, CommitJob
from .services.workspace_credentials import set_workspace_api_key, workspace_api_key_status


class WorldWorkspaceSerializer(serializers.ModelSerializer):
    entity_count = serializers.IntegerField(source="entities.count", read_only=True)
    canvas_count = serializers.IntegerField(source="canvases.count", read_only=True)
    # Write-only by design: the plaintext credential must never be returned.
    ai_api_key = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    ai_api_key_configured = serializers.SerializerMethodField()
    ai_api_key_last4 = serializers.SerializerMethodField()

    def get_ai_api_key_configured(self, obj):
        return bool(workspace_api_key_status(obj)["configured"])

    def get_ai_api_key_last4(self, obj):
        return workspace_api_key_status(obj)["last4"]

    def create(self, validated_data):
        api_key_marker = object()
        api_key = validated_data.pop("ai_api_key", api_key_marker)
        instance = super().create(validated_data)
        if api_key is not api_key_marker:
            set_workspace_api_key(instance, api_key)
        return instance

    def update(self, instance, validated_data):
        api_key_marker = object()
        api_key = validated_data.pop("ai_api_key", api_key_marker)
        instance = super().update(instance, validated_data)
        if api_key is not api_key_marker:
            set_workspace_api_key(instance, api_key)
        return instance

    class Meta:
        model = WorldWorkspace
        fields = [
            "id", "name", "slug", "description", "settings", "repo_path",
            "entity_count", "canvas_count", "ai_api_key", "ai_api_key_configured", "ai_api_key_last4",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "repo_path", "entity_count", "canvas_count", "ai_api_key_configured", "ai_api_key_last4", "created_at", "updated_at"]
        extra_kwargs = {
            "settings": {"required": False},
            "slug": {"required": False, "allow_blank": True},
        }


class EntitySerializer(serializers.ModelSerializer):
    type_label = serializers.CharField(source="get_type_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Entity
        fields = [
            "id", "workspace", "branch", "base_entity", "type", "type_label", "title", "content", "metadata",
            "status", "status_label", "git_path", "commit_hash", "sync_status",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "branch", "base_entity", "git_path", "commit_hash", "sync_status",
            "created_at", "updated_at",
        ]


class RelationSerializer(serializers.ModelSerializer):
    relation_type_label = serializers.CharField(source="get_relation_type_display", read_only=True)
    source_title = serializers.CharField(source="source.title", read_only=True)
    target_title = serializers.CharField(source="target.title", read_only=True)

    class Meta:
        model = Relation
        fields = [
            "id", "workspace", "branch", "base_relation", "archived", "source", "source_title", "target", "target_title",
            "relation_type", "relation_type_label", "properties", "weight", "time_system",
            "valid_from", "valid_to", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "workspace", "branch", "base_relation", "archived", "source_title", "target_title",
            "created_at", "updated_at",
        ]


class CommitJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommitJob
        fields = [
            "id", "workspace", "canvas_id", "branch", "idempotency_key", "status",
            "entity_ids", "relation_ids", "commit_hash", "error_message",
            "actor", "request_data", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "status", "entity_ids", "relation_ids", "commit_hash", "error_message", "created_at", "updated_at"]
