"""Auditable structured Agent runs.

The models intentionally store conclusions and evidence references, never hidden
model chain-of-thought.  They are branch-scoped so a report cannot leak facts
between a work branch and main.
"""
from __future__ import annotations

import uuid
from django.conf import settings
from django.db import models
from apps.core.models import WorldBranch, WorldWorkspace
from apps.canvas.models import DialogueSession, StagingCanvas


class AgentRun(models.Model):
    class Mode(models.TextChoices):
        CONSISTENCY = "consistency", "一致性"
        TIMELINE = "timeline", "时间线"
        MAINTENANCE = "maintenance", "维护"

    class Status(models.TextChoices):
        QUEUED = "queued", "排队"
        RUNNING = "running", "运行中"
        COMPLETED = "completed", "完成"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="agent_runs")
    branch = models.ForeignKey(WorldBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name="agent_runs")
    dialogue_session = models.ForeignKey(DialogueSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="agent_runs")
    canvas = models.ForeignKey(StagingCanvas, on_delete=models.SET_NULL, null=True, blank=True, related_name="agent_runs")
    mode = models.CharField(max_length=24, choices=Mode.choices)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    model = models.CharField(max_length=200, blank=True, default="")
    prompt_hash = models.CharField(max_length=64, blank=True, default="")
    input_hash = models.CharField(max_length=64, blank=True, default="")
    context_revision = models.PositiveIntegerField(default=0)
    output_json = models.JSONField(default=dict, blank=True)
    validation_errors = models.JSONField(default=list, blank=True)
    token_budget = models.PositiveIntegerField(default=6000)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    fallback_used = models.BooleanField(default=False)
    error_code = models.CharField(max_length=100, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="agent_runs_created")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "branch", "created_at"], name="ai_run_ws_branch_created_idx")]


class AgentEvidence(models.Model):
    class SourceType(models.TextChoices):
        ENTITY = "entity", "实体"
        RELATION = "relation", "关系"
        TIMELINE = "timeline", "时间线"
        EVENT = "event", "事件"
        LIFESPAN = "lifespan", "生命周期"
        GIT_COMMIT = "git_commit", "Git提交"
        SNAPSHOT = "snapshot", "快照"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="evidence")
    source_type = models.CharField(max_length=24, choices=SourceType.choices)
    source_id = models.CharField(max_length=128, blank=True, default="")
    field_path = models.CharField(max_length=300, blank=True, default="")
    source_revision = models.CharField(max_length=128, blank=True, default="")
    excerpt = models.TextField(blank=True, default="")
    relevance = models.FloatField(default=1.0)


class AgentToolCallAudit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="tool_calls")
    tool_name = models.CharField(max_length=120)
    arguments_hash = models.CharField(max_length=64, blank=True, default="")
    result_hash = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=24, default="completed")
    latency_ms = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
