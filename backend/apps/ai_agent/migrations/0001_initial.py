# Generated manually for the initial auditable Agent run tables.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid

class Migration(migrations.Migration):
    initial = True
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0014_timesystem_calendar_rules"),
        ("canvas", "0009_dialoguememory_manual_fields_and_audit"),
    ]
    operations = [
        migrations.CreateModel(name="AgentRun", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("mode", models.CharField(choices=[("consistency", "一致性"), ("timeline", "时间线"), ("maintenance", "维护")], max_length=24)),
            ("status", models.CharField(choices=[("queued", "排队"), ("running", "运行中"), ("completed", "完成"), ("failed", "失败"), ("cancelled", "已取消")], default="queued", max_length=24)),
            ("model", models.CharField(blank=True, default="", max_length=200)), ("prompt_hash", models.CharField(blank=True, default="", max_length=64)),
            ("input_hash", models.CharField(blank=True, default="", max_length=64)), ("context_revision", models.PositiveIntegerField(default=0)),
            ("output_json", models.JSONField(blank=True, default=dict)), ("validation_errors", models.JSONField(blank=True, default=list)),
            ("token_budget", models.PositiveIntegerField(default=6000)), ("input_tokens", models.PositiveIntegerField(default=0)),
            ("output_tokens", models.PositiveIntegerField(default=0)), ("latency_ms", models.PositiveIntegerField(default=0)),
            ("fallback_used", models.BooleanField(default=False)), ("error_code", models.CharField(blank=True, default="", max_length=100)),
            ("created_at", models.DateTimeField(auto_now_add=True)), ("completed_at", models.DateTimeField(blank=True, null=True)),
            ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="agent_runs", to="core.worldbranch")),
            ("canvas", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="agent_runs", to="canvas.stagingcanvas")),
            ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="agent_runs_created", to=settings.AUTH_USER_MODEL)),
            ("dialogue_session", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="agent_runs", to="canvas.dialoguesession")),
            ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="agent_runs", to="core.worldworkspace")),
        ], options={"ordering": ["-created_at"]}),
        migrations.CreateModel(name="AgentEvidence", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("source_type", models.CharField(choices=[("entity", "实体"), ("relation", "关系"), ("timeline", "时间线"), ("event", "事件"), ("lifespan", "生命周期"), ("git_commit", "Git提交"), ("snapshot", "快照")], max_length=24)),
            ("source_id", models.CharField(blank=True, default="", max_length=128)), ("field_path", models.CharField(blank=True, default="", max_length=300)),
            ("source_revision", models.CharField(blank=True, default="", max_length=128)), ("excerpt", models.TextField(blank=True, default="")), ("relevance", models.FloatField(default=1.0)),
            ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="evidence", to="ai_agent.agentrun")),
        ]),
        migrations.CreateModel(name="AgentToolCallAudit", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)), ("tool_name", models.CharField(max_length=120)),
            ("arguments_hash", models.CharField(blank=True, default="", max_length=64)), ("result_hash", models.CharField(blank=True, default="", max_length=64)),
            ("status", models.CharField(default="completed", max_length=24)), ("latency_ms", models.PositiveIntegerField(default=0)),
            ("error_code", models.CharField(blank=True, default="", max_length=100)), ("created_at", models.DateTimeField(auto_now_add=True)),
            ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tool_calls", to="ai_agent.agentrun")),
        ]),
        migrations.AddIndex(model_name="agentrun", index=models.Index(fields=["workspace", "branch", "created_at"], name="ai_run_ws_branch_created_idx")),
    ]
