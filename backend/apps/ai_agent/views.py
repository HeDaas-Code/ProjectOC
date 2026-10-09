"""Provider discovery and streamed dialogue API views."""
from __future__ import annotations

import json
import logging
from uuid import UUID, uuid4

from apps.accounts.permissions import membership, accessible_workspace_ids
from apps.canvas.models import (
    DialogueMemoryAudit,
    DialogueSession,
    EntityProposal,
    StagingCanvas,
)
from apps.core.models import Entity, WorldBranch, WorldWorkspace
from apps.core.services.branching import effective_entities
from apps.core.services.consistency import build_consistency_report
from django.conf import settings
from django.db import transaction
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AgentEvidence, AgentRun
from .providers import ModelInfo, ProviderError, configured_provider
from .services.analysis import AgentBusyError, run_analysis
from .services.dialogue import DialogueOrchestrator
from .services.maintenance import MaintenanceReviewService
from .services.memory import (
    MAX_OPEN_QUESTIONS,
    _question_from_session,
    get_memory,
    memory_editable_payload,
    memory_payload,
    refresh_memory,
)

logger = logging.getLogger(__name__)


def _provider_workspace(request):
    workspace_id = request.query_params.get("workspace")
    if not workspace_id:
        return None
    return WorldWorkspace.objects.filter(id=workspace_id, id__in=accessible_workspace_ids(request.user)).first()


class ProviderListView(APIView):

    def get(self, request):
        workspace = _provider_workspace(request)
        provider = configured_provider(workspace)
        return Response(
            [
                {
                    "id": "default",
                    "name": "OpenAI-compatible provider",
                    "base_url": provider.base_url,
                    "configured": bool(provider.api_key),
                    "credential_source": provider.credential_source,
                    "model": provider.default_model,
                }
            ]
        )


class ProviderModelsView(APIView):

    def get(self, request, provider_id):
        if provider_id != "default":
            return Response({"detail": "provider not found"}, status=status.HTTP_404_NOT_FOUND)
        workspace = _provider_workspace(request)
        provider = configured_provider(workspace)
        try:
            models = provider.list_models()
            source = "upstream"
            error = ""
        except ProviderError as exc:
            models = [ModelInfo(id=provider.default_model)]
            source = "configured_fallback"
            error = str(exc)
        return Response(
            {
                "provider": provider_id,
                "source": source,
                "error": error,
                "models": [model.as_dict() for model in models],
            }
        )


class SendDialogueMessageView(APIView):

    def post(self, request, session_id):
        try:
            session = DialogueSession.objects.select_related("workspace", "canvas").get(id=session_id)
        except DialogueSession.DoesNotExist:
            return Response({"detail": "dialogue session not found"}, status=status.HTTP_404_NOT_FOUND)
        text = str(request.data.get("content", "")).strip()
        if not text:
            return Response({"detail": "content is required"}, status=status.HTTP_400_BAD_REQUEST)
        member = membership(request.user, session.workspace)
        if not member:
            return Response({"detail":"workspace not found"}, status=404)
        if member.role == "reader":
            return Response({"detail":"需要 editor 权限"}, status=403)
        if session.canvas_id is None:
            return Response({"detail": "dialogue session has no active canvas"}, status=status.HTTP_400_BAD_REQUEST)
        if session.canvas.workspace_id != session.workspace_id:
            return Response({"detail": "dialogue canvas and workspace do not match"}, status=status.HTTP_400_BAD_REQUEST)

        def stream():
            try:
                for event, payload in DialogueOrchestrator(session).stream(text):
                    yield (
                        f"event: {event}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    )
            except Exception:
                logger.exception("Dialogue failed for session %s", session.id)
                yield (
                    "event: error\n"
                    'data: {"detail":"上游或结构化提取失败；输入已保留，'
                    '没有创建未校验的提案。请检查连接后重试。"}\n\n'
                )

        response = StreamingHttpResponse(
            stream(),
            content_type="text/event-stream; charset=utf-8",
        )
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class DialogueToolView(APIView):
    """Small allowlisted copilot tools; formal facts are never written here."""
    def get(self, request, session_id):
        session = DialogueSession.objects.filter(id=session_id).first()
        if not session or not membership(request.user, session.workspace):
            return Response({"detail": "dialogue session not found"}, status=404)
        return Response({"events": list((session.context or {}).get("copilot_events", []))})

    def post(self, request, session_id):
        session = DialogueSession.objects.select_related("workspace", "canvas", "canvas__branch").filter(id=session_id).first()
        if not session or not membership(request.user, session.workspace):
            return Response({"detail": "dialogue session not found"}, status=404)
        member = membership(request.user, session.workspace)
        if member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        tool = str(request.data.get("tool", ""))
        args = request.data.get("arguments") if isinstance(request.data.get("arguments"), dict) else {}
        if tool == "cancel_event":
            event_id = str(args.get("event_id", ""))
            context = dict(session.context or {})
            events = list(context.get("copilot_events", []))
            found = next((event for event in events if event.get("id") == event_id), None)
            if not found:
                return Response({"detail": "copilot event not found"}, status=404)
            if found.get("status") == "completed":
                return Response({"detail": "已完成的工具不能取消"}, status=409)
            found["status"] = "cancelled"
            found["cancelled_at"] = timezone.now().isoformat()
            session.context = {**context, "copilot_events": events[-50:]}
            session.save(update_fields=["context", "updated_at"])
            return Response({"tool": tool, "status": "cancelled", "event": found})
        def record(status_value, result=None, error=None):
            context = dict(session.context or {})
            events = list(context.get("copilot_events", []))[-49:]
            event = {"id": str(uuid4()), "tool": tool, "status": status_value, "arguments": args, "created_at": timezone.now().isoformat()}
            if result is not None: event["result"] = result
            if error is not None: event["error"] = error
            events.append(event); context["copilot_events"] = events
            session.context = context; session.save(update_fields=["context", "updated_at"])
            return event
        if tool == "search_entities":
            query = str(args.get("query", "")).strip()
            rows = effective_entities(session.workspace, session.canvas.branch if session.canvas else None)
            rows = [row for row in rows if not query or query.lower() in f"{row.title}\n{row.content}".lower()][:50]
            result = [{"id": str(row.id), "title": row.title, "type": row.type, "content": row.content} for row in rows]
            return Response({"tool": tool, "status": "completed", "result": result, "event": record("completed", result)})
        if tool == "create_draft":
            if not session.canvas or session.canvas.purpose != StagingCanvas.Purpose.STAGING:
                return Response({"detail": "副驾驶必须绑定灵感子画布"}, status=400)
            title = str(args.get("title", "")).strip()
            if not title:
                return Response({"detail": "title is required"}, status=400)
            proposal = EntityProposal.objects.create(workspace=session.workspace, canvas=session.canvas, dialogue_session=session, source=EntityProposal.Source.AI, entity_type=str(args.get("entity_type", Entity.EntityType.FLOATING_TIP)), title=title, content=str(args.get("content", "")), metadata={"copilot_tool": tool})
            result = {"proposal_id": str(proposal.id), "status": proposal.status, "requires_review": True}
            return Response({"tool": tool, "status": "completed", "result": result, "event": record("completed", result)}, status=201)
        if tool == "consistency_check":
            result = build_consistency_report(session.workspace, session.canvas.branch if session.canvas else None)
            return Response({"tool": tool, "status": "completed", "result": result, "event": record("completed", result)})
        return Response({"detail": "unknown or disallowed copilot tool"}, status=400)


class DialogueMemoryView(APIView):
    """Inspect and edit the durable memory for a session's branch scope.

    Derived facts, proposals and the generated summary are read-only. Editors
    may maintain bounded notes and archive non-blocking questions; every such
    change is version-checked and appended to an audit log.
    """

    def get(self, request, session_id):
        try:
            session = DialogueSession.objects.select_related("workspace", "canvas", "canvas__branch").get(id=session_id)
        except DialogueSession.DoesNotExist:
            return Response({"detail": "dialogue session not found"}, status=status.HTTP_404_NOT_FOUND)
        if not membership(request.user, session.workspace):
            return Response({"detail": "workspace not found"}, status=status.HTTP_404_NOT_FOUND)
        memory = refresh_memory(session)
        payload = memory_payload(memory)
        if request.query_params.get("audit") in {"1", "true", "yes"}:
            payload["audit"] = [
                {
                    "id": str(item.id),
                    "action": item.action,
                    "before": item.before,
                    "after": item.after,
                    "actor": str(item.actor_id) if item.actor_id else None,
                    "created_at": item.created_at,
                }
                for item in memory.audits.all()[:30]
            ]
        return Response(payload)

    def patch(self, request, session_id):
        try:
            session = DialogueSession.objects.select_related("workspace", "canvas", "canvas__branch").get(id=session_id)
        except DialogueSession.DoesNotExist:
            return Response({"detail": "dialogue session not found"}, status=status.HTTP_404_NOT_FOUND)
        member = membership(request.user, session.workspace)
        if not member:
            return Response({"detail": "workspace not found"}, status=status.HTTP_404_NOT_FOUND)
        if member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=status.HTTP_403_FORBIDDEN)

        # Refresh before locking so pending proposals and newly committed facts
        # are reflected in the optimistic-concurrency response.
        refresh_memory(session)
        with transaction.atomic():
            memory = get_memory(session)
            memory = type(memory).objects.select_for_update().get(id=memory.id)
            expected = request.data.get("expected_revision")
            if expected is not None:
                try:
                    expected = int(expected)
                except (TypeError, ValueError):
                    return Response({"detail": "expected_revision 必须是整数"}, status=status.HTTP_400_BAD_REQUEST)
                if expected != memory.revision:
                    return Response(
                        {"detail": "记忆已被其他会话更新", "memory": memory_payload(memory)},
                        status=status.HTTP_409_CONFLICT,
                    )

            before = memory_editable_payload(memory)
            changed = False
            if "manual_notes" in request.data:
                raw_notes = request.data.get("manual_notes")
                if not isinstance(raw_notes, list) or len(raw_notes) > 40:
                    return Response({"detail": "manual_notes 必须是不超过 40 项的数组"}, status=status.HTTP_400_BAD_REQUEST)
                notes = []
                for item in raw_notes:
                    if isinstance(item, str):
                        text = item.strip()[:1200]
                        if text:
                            notes.append({"text": text, "updated_at": timezone.now().isoformat()})
                    elif isinstance(item, dict):
                        text = str(item.get("text") or "").strip()[:1200]
                        if text:
                            notes.append({"text": text, "updated_at": str(item.get("updated_at") or timezone.now().isoformat())[:80]})
                    else:
                        return Response({"detail": "manual_notes 项必须是文本或对象"}, status=status.HTTP_400_BAD_REQUEST)
                memory.manual_notes = notes
                changed = notes != (before["manual_notes"] or [])

            if "archive_question" in request.data or "restore_question" in request.data:
                action_key = "archive_question" if "archive_question" in request.data else "restore_question"
                raw = request.data.get(action_key)
                text = str(raw.get("text") if isinstance(raw, dict) else raw or "").strip()[:600]
                if not text:
                    return Response({"detail": "需要问题文本"}, status=status.HTTP_400_BAD_REQUEST)
                archived = [item for item in (memory.archived_questions or []) if isinstance(item, dict)]
                existing = {str(item.get("text") or ""): item for item in archived}
                if action_key == "archive_question":
                    existing[text] = {"text": text, "reason": str(raw.get("reason") or "")[:600] if isinstance(raw, dict) else "", "archived_at": timezone.now().isoformat()}
                else:
                    existing.pop(text, None)
                memory.archived_questions = list(existing.values())[:MAX_OPEN_QUESTIONS]
                archived_texts = {str(item.get("text") or "") for item in memory.archived_questions}
                memory.open_questions = [
                    item for item in (memory.open_questions or [])
                    if str(item.get("text") or "") not in archived_texts
                ]
                if action_key == "restore_question":
                    # Restore the active/deferred session copy immediately so
                    # the PATCH response is complete and the next revision
                    # check does not need a hidden refresh to rebuild it.
                    current = _question_from_session(session)
                    if current and current["text"] == text:
                        memory.open_questions = [
                            current,
                            *[
                                item for item in memory.open_questions
                                if str(item.get("text") or "") != text
                            ],
                        ][:MAX_OPEN_QUESTIONS]
                changed = memory.archived_questions != (before["archived_questions"] or []) or changed

            if not changed:
                return Response(memory_payload(memory))
            memory.revision = int(memory.revision or 0) + 1
            # ``open_questions`` is derived but is also persisted so the
            # response and the next optimistic-concurrency request observe the
            # same revision. Omitting it here would make the next refresh
            # silently mutate the memory and turn a valid restore into 409.
            memory.save(update_fields=["manual_notes", "archived_questions", "open_questions", "revision", "updated_at"])
            after = memory_editable_payload(memory)
            DialogueMemoryAudit.objects.create(memory=memory, actor=request.user, action="edit", before=before, after=after)
        return Response(memory_payload(memory))


class MaintenanceReviewView(APIView):
    """Return non-binding AI/deterministic maintenance advice for a world view."""

    def post(self, request, workspace_id):
        workspace = WorldWorkspace.objects.filter(id=workspace_id).first()
        member = membership(request.user, workspace) if workspace else None
        if not member:
            return Response({"detail": "workspace not found"}, status=status.HTTP_404_NOT_FOUND)

        branch_key = str(request.data.get("branch") or request.query_params.get("branch") or "main")
        branch = None
        if branch_key != "main":
            branch = WorldBranch.objects.filter(
                workspace=workspace, status=WorldBranch.Status.ACTIVE, name=branch_key
            ).first()
            if branch is None:
                try:
                    branch_id = UUID(branch_key)
                except (TypeError, ValueError, AttributeError):
                    branch_id = None
                if branch_id is not None:
                    branch = WorldBranch.objects.filter(
                        workspace=workspace, status=WorldBranch.Status.ACTIVE, id=branch_id
                    ).first()
            if not branch:
                return Response({"detail": "branch not found"}, status=status.HTTP_404_NOT_FOUND)

        report = build_consistency_report(workspace, branch)
        model = str(request.data.get("model") or "")
        try:
            result = MaintenanceReviewService(configured_provider(workspace)).review(
                workspace, branch, report, model=model
            )
        except ProviderError:
            logger.exception("Maintenance review failed for workspace %s", workspace.id)
            return Response(
                {
                    "status": "unavailable",
                    "detail": "维护 Agent 暂不可用；确定性一致性报告仍然可用。",
                    "report": report,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)


class MaintenanceProposalView(APIView):
    """Turn a user-confirmed maintenance suggestion into a pending proposal.

    This endpoint deliberately accepts the proposed title/content from the
    user.  The maintenance Agent only supplies context; it never gets a write
    path to formal entities.  The server recomputes the deterministic report
    and rejects stale or hallucinated issue references before creating a
    pending ``EntityProposal``.
    """

    def post(self, request, workspace_id):
        workspace = WorldWorkspace.objects.filter(id=workspace_id).first()
        member = membership(request.user, workspace) if workspace else None
        if not member:
            return Response({"detail": "workspace not found"}, status=status.HTTP_404_NOT_FOUND)
        if member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=status.HTTP_403_FORBIDDEN)

        payload = request.data if isinstance(request.data, dict) else {}
        action = str(payload.get("action") or "")
        issue_code = str(payload.get("issue_code") or "").strip()
        if action != "draft_proposal":
            return Response({"detail": "只有 draft_proposal 建议可以转为提案"}, status=status.HTTP_400_BAD_REQUEST)
        if not issue_code or len(issue_code) > 80:
            return Response({"detail": "需要合法的 issue_code"}, status=status.HTTP_400_BAD_REQUEST)

        canvas_id = payload.get("canvas")
        canvas = StagingCanvas.objects.filter(id=canvas_id, workspace=workspace).select_related("branch").first()
        if not canvas:
            return Response({"detail": "canvas not found"}, status=status.HTTP_404_NOT_FOUND)
        if canvas.status == StagingCanvas.Status.ARCHIVED:
            return Response({"detail": "归档画布不能创建提案"}, status=status.HTTP_409_CONFLICT)

        branch_key = str(payload.get("branch") or "main")
        branch = None
        if branch_key != "main":
            branch = WorldBranch.objects.filter(
                workspace=workspace, status=WorldBranch.Status.ACTIVE, name=branch_key
            ).first()
            if branch is None:
                try:
                    branch = WorldBranch.objects.filter(
                        workspace=workspace, status=WorldBranch.Status.ACTIVE, id=UUID(branch_key)
                    ).first()
                except (TypeError, ValueError, AttributeError):
                    branch = None
            if branch is None:
                return Response({"detail": "branch not found"}, status=status.HTTP_404_NOT_FOUND)

        report = build_consistency_report(workspace, branch)
        issue = next((item for item in report.get("issues", []) if str(item.get("code")) == issue_code), None)
        if issue is None:
            return Response({"detail": "该维护问题已过期或不存在，请重新审阅"}, status=status.HTTP_409_CONFLICT)

        entity_type = str(payload.get("entity_type") or Entity.EntityType.CANONICAL_SETTING)
        valid_types = {value for value, _ in Entity.EntityType.choices}
        if entity_type not in valid_types:
            return Response({"detail": "未知的实体类型"}, status=status.HTTP_400_BAD_REQUEST)
        title = str(payload.get("title") or "").strip()
        content = str(payload.get("content") or "").strip()
        if not title or len(title) > 500:
            return Response({"detail": "标题不能为空且不能超过 500 个字符"}, status=status.HTTP_400_BAD_REQUEST)
        if len(content) > 100000:
            return Response({"detail": "内容不能超过 100000 个字符"}, status=status.HTTP_400_BAD_REQUEST)

        raw_targets = payload.get("target_ids", [])
        target_ids = [str(value) for value in raw_targets] if isinstance(raw_targets, list) else []
        report_targets = {str(issue.get(key)) for key in ("entity_id", "relation_id") if issue.get(key)}
        if any(target not in report_targets for target in target_ids):
            return Response({"detail": "提案目标必须来自当前一致性问题"}, status=status.HTTP_400_BAD_REQUEST)
        if not target_ids:
            target_ids = sorted(report_targets)

        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if idempotency_key:
            for existing in EntityProposal.objects.filter(canvas=canvas, source=EntityProposal.Source.AI).order_by("-created_at")[:100]:
                maintenance = (existing.metadata or {}).get("maintenance")
                if isinstance(maintenance, dict) and maintenance.get("idempotency_key") == idempotency_key:
                    from apps.canvas.serializers import EntityProposalSerializer
                    return Response({**EntityProposalSerializer(existing).data, "idempotent": True}, status=status.HTTP_200_OK)

        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        metadata = {**metadata, "maintenance": {
            "issue_code": issue_code,
            "action": action,
            "reason": str(payload.get("reason") or issue.get("message") or "").strip()[:1200],
            "target_ids": target_ids,
            "branch": report.get("branch", "main"),
            "idempotency_key": idempotency_key,
            "confirmed_by": str(request.user.id),
        }}
        proposal = EntityProposal.objects.create(
            workspace=workspace, canvas=canvas, source=EntityProposal.Source.AI,
            entity_type=entity_type, title=title, content=content, metadata=metadata,
            conflicts=[{"issue_code": issue_code, "message": issue.get("message", "")}],
        )
        from apps.canvas.serializers import EntityProposalSerializer
        return Response(EntityProposalSerializer(proposal).data, status=status.HTTP_201_CREATED)


class AgentAnalysisView(APIView):
    """Run deterministic, auditable consistency/timeline analysis."""
    def post(self, request, session_id):
        try:
            session = DialogueSession.objects.select_related("workspace", "canvas").get(id=session_id)
        except DialogueSession.DoesNotExist:
            return Response({"detail": "dialogue session not found"}, status=404)
        member = membership(request.user, session.workspace)
        if not member:
            return Response({"detail": "workspace not found"}, status=404)
        if member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        mode = str(request.data.get("mode") or "consistency")
        if mode not in {"consistency", "timeline", "maintenance"}:
            return Response({"detail": "mode must be consistency, timeline or maintenance"}, status=400)
        key = str(request.data.get("branch") or "main")
        branch = None
        if key != "main":
            branch = WorldBranch.objects.filter(workspace=session.workspace, status=WorldBranch.Status.ACTIVE).filter(id=key).first()
            if branch is None:
                branch = WorldBranch.objects.filter(workspace=session.workspace, status=WorldBranch.Status.ACTIVE, name=key).first()
            if branch is None:
                return Response({"detail": "branch not found"}, status=404)
        running = AgentRun.objects.filter(workspace=session.workspace, status__in=[AgentRun.Status.QUEUED, AgentRun.Status.RUNNING]).count()
        max_running = int(getattr(settings, "AGENT_MAX_CONCURRENT_WORKSPACE", 2))
        if running >= max_running:
            return Response({"detail": "workspace agent concurrency limit reached", "limit": max_running}, status=429)
        try:
            run, result = run_analysis(workspace=session.workspace, branch=branch, user=request.user, session=session, canvas=session.canvas, mode=mode, model=str(request.data.get("model") or session.model or ""), token_budget=request.data.get("token_budget", 6000), context_revision=request.data.get("context_revision", 0))
        except AgentBusyError as exc:
            return Response({"detail": "agent concurrency limit reached", "code": str(exc)}, status=429)
        except Exception:
            logger.exception("Agent analysis failed for session %s", session.id)
            return Response({"detail": "analysis failed"}, status=503)
        return Response({"run_id": str(run.id), "status": run.status, "result": result}, status=201)


class AgentRunView(APIView):
    def _run(self, request, run_id):
        # Membership is checked after the id lookup to avoid exposing whether
        # an arbitrary run exists outside the caller workspace.
        run = AgentRun.objects.filter(id=run_id).select_related("workspace", "branch", "created_by").first()
        if not run or not membership(request.user, run.workspace):
            return None
        return run

    def get(self, request, run_id):
        run = self._run(request, run_id)
        if not run:
            return Response({"detail": "agent run not found"}, status=404)
        return Response({"id": str(run.id), "workspace": str(run.workspace_id), "branch": run.branch.name if run.branch else "main", "mode": run.mode, "status": run.status, "model": run.model, "context_revision": run.context_revision, "output": run.output_json, "validation_errors": run.validation_errors, "token_budget": run.token_budget, "input_tokens": run.input_tokens, "output_tokens": run.output_tokens, "latency_ms": run.latency_ms, "fallback_used": run.fallback_used, "created_at": run.created_at, "completed_at": run.completed_at})

    def post(self, request, run_id):
        run = self._run(request, run_id)
        if not run:
            return Response({"detail": "agent run not found"}, status=404)
        member = membership(request.user, run.workspace)
        if member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        if run.status in {AgentRun.Status.QUEUED, AgentRun.Status.RUNNING}:
            run.status = AgentRun.Status.CANCELLED; run.completed_at = timezone.now(); run.save(update_fields=["status", "completed_at"])
        return Response({"id": str(run.id), "status": run.status})


class AgentRunEventsView(APIView):
    """Small replayable SSE endpoint for queued/running/completed runs.

    The deterministic MVP completes in-process, but clients can use the same
    endpoint when a worker-backed runner is enabled later.
    """
    def get(self, request, run_id):
        run = AgentRun.objects.filter(id=run_id).select_related("workspace").first()
        if not run or not membership(request.user, run.workspace):
            return Response({"detail": "agent run not found"}, status=404)
        payload = {
            "run_id": str(run.id), "status": run.status, "output": run.output_json,
            "validation_errors": run.validation_errors, "context_revision": run.context_revision,
            "evidence_url": f"/api/v1/agent/runs/{run.id}/evidence/",
        }
        def stream():
            yield "event: status\ndata: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"
            if run.status in {AgentRun.Status.COMPLETED, AgentRun.Status.FAILED, AgentRun.Status.CANCELLED}:
                yield "event: done\ndata: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"
        response = StreamingHttpResponse(stream(), content_type="text/event-stream; charset=utf-8")
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
        return response


class AgentCancelView(APIView):
    def post(self, request, run_id):
        run = AgentRun.objects.filter(id=run_id).select_related("workspace").first()
        if not run or not membership(request.user, run.workspace):
            return Response({"detail": "agent run not found"}, status=404)
        member = membership(request.user, run.workspace)
        if member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        if run.status in {AgentRun.Status.QUEUED, AgentRun.Status.RUNNING}:
            run.status = AgentRun.Status.CANCELLED
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "completed_at"])
        return Response({"id": str(run.id), "status": run.status})


class AgentEvidenceView(APIView):
    def get(self, request, run_id):
        run = AgentRun.objects.filter(id=run_id).select_related("workspace").first()
        if not run or not membership(request.user, run.workspace):
            return Response({"detail": "agent run not found"}, status=404)
        return Response([{"id": str(item.id), "source_type": item.source_type, "source_id": item.source_id, "field_path": item.field_path, "source_revision": item.source_revision, "excerpt": item.excerpt, "relevance": item.relevance} for item in AgentEvidence.objects.filter(run=run).order_by("source_type", "source_id")])
