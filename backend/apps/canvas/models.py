import uuid
from django.conf import settings
from django.db import models
from apps.core.models import Entity, Relation, TimeSystem, WorldBranch, WorldWorkspace


class StagingCanvas(models.Model):
    class Purpose(models.TextChoices):
        STAGING = "staging", "灵感暂存"
        GRAPH = "graph", "世界观图谱"

    class Status(models.TextChoices):
        ACTIVE = "active", "活跃"
        ARCHIVED = "archived", "归档"
        COMMITTED = "committed", "已提交"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purpose = models.CharField(max_length=16, choices=Purpose.choices, default=Purpose.STAGING)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="canvases")
    branch = models.ForeignKey(WorldBranch, on_delete=models.PROTECT, null=True, blank=True, related_name="canvases")
    name = models.CharField(max_length=200, default="未命名暂存区")
    snapshot = models.JSONField(default=dict, blank=True)
    sync_metadata = models.JSONField(default=dict, blank=True)
    snapshot_version = models.PositiveIntegerField(default=1)
    operation_sequence = models.PositiveBigIntegerField(default=0)
    # Highest durable operation cursor included in ``snapshot``. Keeping this
    # separate from ``operation_sequence`` makes log compaction safe: a
    # snapshot may lag behind operations that arrived while it was saving.
    snapshot_operation_cursor = models.PositiveBigIntegerField(default=0)
    operation_compacted_through = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    last_saved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "branch"],
                condition=models.Q(purpose="graph"),
                name="unique_graph_canvas_workspace_branch",
            ),
    ]

    def __str__(self) -> str:
        return self.name


class CanvasContainer(models.Model):
    """A stable, nested workspace container owning one editable canvas."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="canvas_containers")
    branch = models.ForeignKey(WorldBranch, on_delete=models.CASCADE, null=True, blank=True, related_name="canvas_containers")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="children")
    canvas = models.OneToOneField(StagingCanvas, on_delete=models.CASCADE, related_name="container")
    name = models.CharField(max_length=200)
    sort_order = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=StagingCanvas.Status.choices, default=StagingCanvas.Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name", "created_at"]
        constraints = [models.UniqueConstraint(fields=["workspace", "parent", "name"], name="unique_container_name_under_parent")]


class EntityCanvasReference(models.Model):
    """A semantic entity may be placed on multiple container canvases."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="entity_canvas_references")
    branch = models.ForeignKey(WorldBranch, on_delete=models.CASCADE, related_name="entity_canvas_references")
    container = models.ForeignKey(CanvasContainer, on_delete=models.CASCADE, related_name="entity_references")
    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="canvas_references")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["container", "entity"], name="unique_entity_container_reference")]


class DialogueSession(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "活跃"
        ARCHIVED = "archived", "归档"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="dialogue_sessions")
    canvas = models.ForeignKey(StagingCanvas, on_delete=models.SET_NULL, null=True, blank=True, related_name="dialogue_sessions")
    provider_id = models.CharField(max_length=100, default="default")
    model = models.CharField(max_length=200, blank=True, default="")
    title = models.CharField(max_length=200, default="产婆式构建对话")
    context = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class DialogueMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "用户"
        ASSISTANT = "assistant", "助手"
        SYSTEM = "system", "系统"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(DialogueSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=Role.choices)
    content = models.TextField()
    intent = models.CharField(max_length=50, blank=True, default="")
    extracted_data = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class DialogueMemory(models.Model):
    """Bounded, branch-scoped long-term memory for the worldbuilding agent.

    Formal entities and relations are the only source of ``confirmed_facts``.
    Pending proposals and conversation-derived notes stay in separate fields so
    the agent can remember work in progress without treating it as canon.
    ``scope_key`` is explicit because nullable foreign-key uniqueness differs
    between database engines and main-branch memories have no branch row.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="dialogue_memories")
    branch = models.ForeignKey(
        "core.WorldBranch", on_delete=models.CASCADE, null=True, blank=True, related_name="dialogue_memories"
    )
    scope_key = models.CharField(max_length=180, unique=True)
    summary = models.TextField(blank=True, default="")
    confirmed_facts = models.JSONField(default=list, blank=True)
    working_notes = models.JSONField(default=list, blank=True)
    open_questions = models.JSONField(default=list, blank=True)
    recent_session_summary = models.TextField(blank=True, default="")
    # User-authored context is deliberately separate from derived facts and
    # pending proposals. It survives deterministic refreshes and is never
    # promoted to canon automatically.
    manual_notes = models.JSONField(default=list, blank=True)
    archived_questions = models.JSONField(default=list, blank=True)
    revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["workspace", "branch"])]


class DialogueMemoryAudit(models.Model):
    """Append-only audit record for user-visible memory edits."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    memory = models.ForeignKey(DialogueMemory, on_delete=models.CASCADE, related_name="audits")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="dialogue_memory_audits"
    )
    action = models.CharField(max_length=40)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["memory", "created_at"])]


class EntityProposal(models.Model):
    class Source(models.TextChoices):
        USER = "user", "用户"
        AI = "ai", "AI"

    class Status(models.TextChoices):
        PENDING = "pending", "待审核"
        ACCEPTED = "accepted", "已接受"
        REJECTED = "rejected", "已拒绝"
        SUPERSEDED = "superseded", "已替代"

    class Operation(models.TextChoices):
        CREATE = "create", "创建"
        UPDATE = "update", "修改"
        ARCHIVE = "archive", "归档"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="entity_proposals")
    canvas = models.ForeignKey(StagingCanvas, on_delete=models.CASCADE, related_name="entity_proposals")
    dialogue_session = models.ForeignKey(DialogueSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="entity_proposals")
    source_message = models.ForeignKey(DialogueMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name="entity_proposals")
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.AI)
    operation = models.CharField(max_length=20, choices=Operation.choices, default=Operation.CREATE)
    target_entity = models.ForeignKey(Entity, on_delete=models.SET_NULL, null=True, blank=True, related_name="edit_proposals")
    entity_type = models.CharField(max_length=50, choices=Entity.EntityType.choices)
    title = models.CharField(max_length=500)
    content = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    conflicts = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_entity = models.ForeignKey(Entity, on_delete=models.SET_NULL, null=True, blank=True, related_name="source_proposals")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["canvas", "status"])]


class RelationProposal(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待审核"
        ACCEPTED = "accepted", "已接受"
        REJECTED = "rejected", "已拒绝"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="relation_proposals")
    canvas = models.ForeignKey(StagingCanvas, on_delete=models.CASCADE, related_name="relation_proposals")
    source_entity = models.ForeignKey(Entity, on_delete=models.SET_NULL, null=True, blank=True, related_name="outgoing_relation_proposals")
    source_proposal = models.ForeignKey(EntityProposal, on_delete=models.CASCADE, null=True, blank=True, related_name="suggested_relations")
    target_proposal = models.ForeignKey(EntityProposal, on_delete=models.SET_NULL, null=True, blank=True, related_name="incoming_relation_proposals")
    target_entity = models.ForeignKey(Entity, on_delete=models.SET_NULL, null=True, blank=True, related_name="incoming_relation_proposals")
    relation_type = models.CharField(max_length=50, choices=Relation.RelationType.choices)
    time_system = models.ForeignKey(TimeSystem, on_delete=models.SET_NULL, null=True, blank=True, related_name="relation_proposals")
    valid_from = models.BigIntegerField(null=True, blank=True)
    valid_to = models.BigIntegerField(null=True, blank=True)
    properties = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(default=0.0)
    reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_relation = models.ForeignKey(Relation, on_delete=models.SET_NULL, null=True, blank=True, related_name="source_proposal")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-confidence", "created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(source_entity__isnull=False) ^ models.Q(source_proposal__isnull=False)),
                name="relation_proposal_exactly_one_source",
            ),
        ]


class CanvasRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    canvas = models.ForeignKey(StagingCanvas, on_delete=models.CASCADE, related_name="revisions")
    version = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict, blank=True)
    sync_metadata = models.JSONField(default=dict, blank=True)
    operation_cursor = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["canvas", "version"], name="canvas_revision_version"),
        ]


class CanvasSyncEvent(models.Model):
    """Durable official TLDraw sync commit metadata.

    This is an append-only replay/audit log, not the world-model source of
    truth. The materialized ``StagingCanvas.snapshot`` remains authoritative
    for restoration while this row lets the sync service verify clocks and
    hashes after a restart.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    canvas = models.ForeignKey(StagingCanvas, on_delete=models.CASCADE, related_name="sync_events")
    branch = models.ForeignKey(WorldBranch, on_delete=models.PROTECT, null=True, blank=True, related_name="canvas_sync_events")
    room_key = models.CharField(max_length=500)
    document_clock = models.PositiveBigIntegerField(default=0)
    snapshot_hash = models.CharField(max_length=64)
    diff = models.JSONField(default=dict, blank=True)
    protocol_version = models.CharField(max_length=80, default="tldraw-sync-v2")
    schema_version = models.CharField(max_length=120, default="oc-tldraw-2")
    source = models.CharField(max_length=80, default="sync-service")
    client_session = models.CharField(max_length=160, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document_clock", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["canvas", "document_clock", "snapshot_hash"], name="canvas_sync_event_clock_hash"),
        ]
        indexes = [
            models.Index(fields=["canvas", "document_clock"], name="canvas_sync_event_cursor_idx"),
            models.Index(fields=["room_key", "created_at"], name="canvas_sync_event_room_idx"),
        ]


class CanvasOperation(models.Model):
    """Durable record-level operation log for reconnect/replay.

    The materialized canvas snapshot remains the runtime fallback, while this
    append-only log gives the sync service a cursor it can replay after a
    connection interruption.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    canvas = models.ForeignKey(StagingCanvas, on_delete=models.CASCADE, related_name="operations")
    sequence = models.PositiveBigIntegerField()
    op_id = models.CharField(max_length=256)
    client_id = models.CharField(max_length=128)
    clock = models.PositiveBigIntegerField()
    operation = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["canvas", "sequence"], name="canvas_operation_sequence"),
            models.UniqueConstraint(fields=["canvas", "op_id"], name="canvas_operation_op_id"),
        ]
        indexes = [models.Index(fields=["canvas", "sequence"])]
