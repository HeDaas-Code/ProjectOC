import uuid
from django.db import models
from django.utils.text import slugify


class WorldWorkspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True)
    description = models.TextField(blank=True, default="")
    # Workspace-level configuration is intentionally JSON so personal installs
    # can evolve without a migration for every UI preference. Secrets are never
    # stored here; provider credentials remain server-side environment values.
    settings = models.JSONField(default=dict, blank=True)
    repo_path = models.CharField(max_length=1000, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = (slugify(self.name, allow_unicode=True)[:180] or "world") + "-" + str(self.id)[:8]
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class WorkspaceAISecret(models.Model):
    """Encrypted, owner-managed AI credential for one workspace."""
    workspace = models.OneToOneField(
        WorldWorkspace, on_delete=models.CASCADE, related_name="ai_secret"
    )
    api_key_encrypted = models.TextField(blank=True, default="")
    api_key_last4 = models.CharField(max_length=4, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AI credentials for {self.workspace.name}"


class WorldBranch(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        MERGED = "merged", "Merged"
        ARCHIVED = "archived", "Archived"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=120)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    base_snapshot = models.JSONField(default=dict, blank=True)
    base_commit = models.CharField(max_length=64, blank=True, default="")
    git_ref = models.CharField(max_length=160, blank=True, default="")
    merged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["workspace", "name"], name="unique_workspace_branch_name")]
    def __str__(self): return f"{self.workspace.name}:{self.name}"


class Entity(models.Model):
    class EntityType(models.TextChoices):
        CANONICAL_SETTING = "canonical_setting", "设定系"
        CHARACTER = "character", "人物"
        TIMELINE = "timeline", "时间线"
        EVENT = "event", "事件"
        ITEM = "item", "物品"
        LOCATION = "location", "地点"
        FACTION = "faction", "势力"
        FLOATING_TIP = "floating_tip", "游离设定"

    class Status(models.TextChoices):
        ACTIVE = "active", "有效"
        ARCHIVED = "archived", "归档"

    class SyncStatus(models.TextChoices):
        PENDING = "pending", "待同步"
        SYNCED = "synced", "已同步"
        FAILED = "failed", "同步失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="entities")
    branch = models.ForeignKey(WorldBranch, on_delete=models.CASCADE, null=True, blank=True, related_name="entities")
    # A branch override is a copy-on-write version of a main entity.  A null
    # base means this row is a branch-local addition.
    base_entity = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="branch_overrides")
    type = models.CharField(max_length=50, choices=EntityType.choices)
    title = models.CharField(max_length=500)
    content = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    git_path = models.CharField(max_length=1000, blank=True, default="")
    commit_hash = models.CharField(max_length=64, blank=True, default="")
    sync_status = models.CharField(max_length=20, choices=SyncStatus.choices, default=SyncStatus.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title", "created_at"]
        indexes = [
            models.Index(fields=["workspace", "type"]),
            models.Index(fields=["workspace", "status"]),
            models.Index(fields=["workspace", "title"]),
            models.Index(fields=["workspace", "branch", "base_entity"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "base_entity"],
                condition=models.Q(branch__isnull=False, base_entity__isnull=False),
                name="unique_branch_base_entity_override",
            ),
        ]

    def __str__(self) -> str:
        return self.title


class Relation(models.Model):
    class RelationType(models.TextChoices):
        DERIVES_FROM = "DERIVES_FROM", "衍生自"
        BELONGS_TO = "BELONGS_TO", "归属于"
        LINKED_TO = "LINKED_TO", "关联到"
        INFLUENCES = "INFLUENCES", "影响"
        CONFLICTS_WITH = "CONFLICTS_WITH", "冲突于"
        EVOLVES_TO = "EVOLVES_TO", "演变为"
        KNOWS = "KNOWS", "认识"
        LOCATED_AT = "LOCATED_AT", "位于"
        OWNS = "OWNS", "拥有"
        PARTICIPATES_IN = "PARTICIPATES_IN", "参与"
        PARENT_OF = "PARENT_OF", "父级"
        INHERITS_FROM = "INHERITS_FROM", "继承自"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="relations")
    branch = models.ForeignKey(WorldBranch, on_delete=models.CASCADE, null=True, blank=True, related_name="relations")
    # A branch override is a copy-on-write version of a main relation.
    base_relation = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="branch_overrides")
    # Relation tombstones hide an inherited main relation without mutating it.
    archived = models.BooleanField(default=False)
    time_system = models.ForeignKey("TimeSystem", on_delete=models.SET_NULL, null=True, blank=True, related_name="relations")
    source = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="outgoing_relations")
    target = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="incoming_relations")
    relation_type = models.CharField(max_length=50, choices=RelationType.choices)
    properties = models.JSONField(default=dict, blank=True)
    weight = models.FloatField(default=1.0)
    valid_from = models.BigIntegerField(null=True, blank=True)
    valid_to = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["relation_type", "created_at"]
        indexes = [
            models.Index(fields=["workspace", "source"]),
            models.Index(fields=["workspace", "target"]),
            models.Index(fields=["workspace", "relation_type"]),
            models.Index(fields=["workspace", "branch", "base_relation"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True) | models.Q(valid_from__isnull=True) | models.Q(valid_to__gte=models.F("valid_from")),
                name="relation_valid_range",
            ),
            models.UniqueConstraint(
                fields=["branch", "base_relation"],
                condition=models.Q(branch__isnull=False, base_relation__isnull=False),
                name="unique_branch_base_relation_override",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source} -[{self.relation_type}]-> {self.target}"


class CommitJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        DATABASE_COMMITTED = "database_committed", "数据库已提交"
        GIT_SYNCING = "git_syncing", "Git同步中"
        SYNCED = "synced", "已同步"
        GIT_SYNC_FAILED = "git_sync_failed", "Git同步失败"
        PROJECTION_SYNC_FAILED = "projection_sync_failed", "图谱同步失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="commit_jobs")
    canvas_id = models.UUIDField(null=True, blank=True)
    branch = models.ForeignKey(WorldBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name="commit_jobs")
    idempotency_key = models.CharField(max_length=255)
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.PENDING)
    entity_ids = models.JSONField(default=list, blank=True)
    relation_ids = models.JSONField(default=list, blank=True)
    commit_hash = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    request_data = models.JSONField(default=dict)
    export_data = models.JSONField(default=dict)
    actor = models.CharField(max_length=100, default="local-user")
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at", "lease_until"], name="commit_outbox_ready_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "idempotency_key"], name="workspace_idempotency_key"),
        ]

    def __str__(self) -> str:
        return f"{self.workspace.name}: {self.status}"


class GraphProjectionJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SYNCED = "synced", "Synced"
        FAILED = "failed", "Failed"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="projection_jobs")
    # A projection job may be the second half of a confirmed commit. Keeping
    # this durable link lets retries move only the projection state without
    # replaying the already-synced Git commit.
    commit_job = models.OneToOneField(
        "CommitJob", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="projection_job",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at", "lease_until"], name="graph_outbox_ready_idx"),
        ]


class TimeSystem(models.Model):
    """A world's own sortable chronology and its human-facing display rules."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="time_systems")
    name = models.CharField(max_length=160)
    epoch_label = models.CharField(max_length=160, blank=True, default="")
    unit_name = models.CharField(max_length=80, default="tick")
    units = models.JSONField(default=list, blank=True, help_text="Ordered display units, e.g. [{name: 'year', ticks: 360}]")
    # Optional deterministic calendar rules for worlds whose months/years do
    # not have a fixed number of base units. The normalized integer tick
    # remains the source of truth; this field only controls interpretation.
    calendar_rules = models.JSONField(default=dict, blank=True)
    display_format = models.CharField(max_length=160, default="{value} {unit}")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["workspace", "name"], name="unique_time_system_name")]

    def __str__(self):
        return f"{self.workspace.name}: {self.name}"


class TimeSystemConversion(models.Model):
    """A directed affine conversion between two chronology systems.

    Values are kept as exact integers in their source/target systems. The
    conversion is intentionally explicit and directed: worlds may have
    calendars that cannot be safely inverted or that use a different epoch.
    The display/query layer can compose several declared edges when needed.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="time_system_conversions")
    source_system = models.ForeignKey(TimeSystem, on_delete=models.CASCADE, related_name="outgoing_conversions")
    target_system = models.ForeignKey(TimeSystem, on_delete=models.CASCADE, related_name="incoming_conversions")
    numerator = models.PositiveBigIntegerField(default=1)
    denominator = models.PositiveBigIntegerField(default=1)
    offset = models.BigIntegerField(default=0)
    label = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_system__name", "target_system__name"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(source_system=models.F("target_system")),
                name="time_conversion_distinct_systems",
            ),
            models.CheckConstraint(
                condition=models.Q(numerator__gt=0) & models.Q(denominator__gt=0),
                name="time_conversion_positive_scale",
            ),
            models.UniqueConstraint(
                fields=["workspace", "source_system", "target_system"],
                name="unique_time_conversion_route",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "source_system"], name="time_conv_source_idx"),
            models.Index(fields=["workspace", "target_system"], name="time_conv_target_idx"),
        ]

    def __str__(self):
        return f"{self.source_system.name} → {self.target_system.name}"


class TimelineEntry(models.Model):
    """A dated event placed on a timeline in one world's normalized time axis."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="timeline_entries")
    branch = models.ForeignKey(WorldBranch, on_delete=models.CASCADE, null=True, blank=True, related_name="timeline_entries")
    timeline = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="timeline_entries")
    event = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="event_occurrences")
    time_system = models.ForeignKey(TimeSystem, on_delete=models.CASCADE, related_name="entries")
    start_value = models.BigIntegerField()
    end_value = models.BigIntegerField(null=True, blank=True)
    sequence = models.FloatField(default=0)
    summary = models.TextField(blank=True, default="")
    properties = models.JSONField(default=dict, blank=True)
    # A branch tombstone hides the inherited main occurrence without deleting
    # the main row. Main records are still hard-deleted by the API.
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_value", "sequence", "created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(end_value__isnull=True) | models.Q(end_value__gte=models.F("start_value")), name="timeline_entry_valid_range"),
            models.UniqueConstraint(fields=["timeline", "event", "time_system"], condition=models.Q(branch__isnull=True), name="unique_timeline_event_axis_main"),
            models.UniqueConstraint(fields=["timeline", "event", "time_system", "branch"], condition=models.Q(branch__isnull=False), name="unique_timeline_event_axis_branch"),
        ]
        indexes = [models.Index(fields=["workspace", "time_system", "start_value"])]


class TimelineParticipation(models.Model):
    entry = models.ForeignKey(TimelineEntry, on_delete=models.CASCADE, related_name="participations")
    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="timeline_participations")
    role = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["entry", "entity"], name="unique_timeline_participant")]


class CharacterLifespan(models.Model):
    """Explicit existence interval for a character on a particular time axis."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(WorldWorkspace, on_delete=models.CASCADE, related_name="character_lifespans")
    branch = models.ForeignKey(WorldBranch, on_delete=models.CASCADE, null=True, blank=True, related_name="character_lifespans")
    character = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="lifespans")
    time_system = models.ForeignKey(TimeSystem, on_delete=models.CASCADE, related_name="lifespans")
    start_value = models.BigIntegerField(null=True, blank=True)
    end_value = models.BigIntegerField(null=True, blank=True)
    properties = models.JSONField(default=dict, blank=True)
    # A branch tombstone hides an inherited main lifespan without mutating it.
    archived = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(end_value__isnull=True) | models.Q(start_value__isnull=True) | models.Q(end_value__gte=models.F("start_value")), name="character_lifespan_valid_range"),
            models.UniqueConstraint(fields=["character", "time_system"], condition=models.Q(branch__isnull=True), name="unique_character_lifespan_axis_main"),
            models.UniqueConstraint(fields=["character", "time_system", "branch"], condition=models.Q(branch__isnull=False), name="unique_character_lifespan_axis_branch"),
        ]

