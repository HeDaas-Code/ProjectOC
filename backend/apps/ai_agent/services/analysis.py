"""Deterministic, auditable Agent analysis primitives.

Rules run before an LLM.  The optional model is only asked to explain findings;
it never receives a write-capable tool and never writes canon.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict

from apps.ai_agent.models import AgentEvidence, AgentRun
from apps.ai_agent.providers import ProviderError, configured_provider
from apps.core.models import (
    Entity,
    Relation,
    TimeSystemConversion,
    WorldBranch,
    WorldWorkspace,
)
from apps.core.services.branching import effective_entities, effective_relations
from apps.core.services.consistency import _temporal_rows, build_consistency_report
from django.conf import settings
from django.utils import timezone

_LOCK_GUARD = threading.Lock()
_WORKSPACE_ACTIVE = defaultdict(int)
_USER_ACTIVE = defaultdict(int)
_BRANCH_ACTIVE = defaultdict(int)


class AgentBusyError(RuntimeError):
    """The configured workspace/user/branch concurrency budget is exhausted."""


class AgentCancelledError(RuntimeError):
    """Raised when a user cancels a run while it is executing."""


def _try_acquire(workspace, branch, user):
    workspace_key = str(workspace.id)
    user_key = str(getattr(user, "id", "anonymous"))
    branch_key = f"{workspace_key}:{branch.id if branch else 'main'}"
    with _LOCK_GUARD:
        if _WORKSPACE_ACTIVE[workspace_key] >= int(getattr(settings, "AGENT_MAX_CONCURRENT_WORKSPACE", 2)):
            raise AgentBusyError("workspace_agent_concurrency_limit")
        if _USER_ACTIVE[user_key] >= int(getattr(settings, "AGENT_MAX_CONCURRENT_USER", 1)):
            raise AgentBusyError("user_agent_concurrency_limit")
        if _BRANCH_ACTIVE[branch_key] >= int(getattr(settings, "AGENT_MAX_CONCURRENT_BRANCH", 1)):
            raise AgentBusyError("branch_agent_concurrency_limit")
        _WORKSPACE_ACTIVE[workspace_key] += 1
        _USER_ACTIVE[user_key] += 1
        _BRANCH_ACTIVE[branch_key] += 1
    return workspace_key, user_key, branch_key


def _release(keys):
    if not keys:
        return
    workspace_key, user_key, branch_key = keys
    with _LOCK_GUARD:
        for counter, key in ((_WORKSPACE_ACTIVE, workspace_key), (_USER_ACTIVE, user_key), (_BRANCH_ACTIVE, branch_key)):
            counter[key] = max(0, counter[key] - 1)
            if not counter[key]:
                counter.pop(key, None)


def _context_revision(canvas=None, session=None):
    if canvas is not None:
        return int(getattr(canvas, "snapshot_version", 0) or 0)
    if session is not None:
        memory = getattr(session, "context", {}) or {}
        return int(memory.get("revision", 0) or 0) if isinstance(memory, dict) else 0
    return 0


def _llm_explanation(result, model, token_budget):
    """Ask an optional provider to explain deterministic findings.

    Returns ``(advisory, fallback_used, error_code, usage)``. The provider has
    no write tools and its output is treated as advisory JSON; deterministic
    findings always survive provider failure.
    """
    if not getattr(settings, "AGENT_LLM_ENABLED", True):
        return {}, False, "llm_disabled", {}
    provider = configured_provider(workspace)
    if not getattr(provider, "api_key", ""):
        return {}, False, "provider_not_configured", {}
    schema = {"type": "object", "properties": {
        "summary": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "finding_notes": {"type": "array", "items": {"type": "object", "properties": {"title": {"type": "string"}, "explanation": {"type": "string"}, "confidence": {"type": "number"}}, "required": ["title", "explanation"]}},
    }, "required": ["summary", "questions", "finding_notes"], "additionalProperties": False}
    prompt = [
        {"role": "system", "content": "你是设定一致性审阅助手。只能解释给定事实，不能创造或写入正式设定。只返回符合 JSON Schema 的摘要、问题和审阅说明。"},
        {"role": "user", "content": json.dumps({"findings": result.get("findings", []), "summary": result.get("summary", {}), "token_budget": token_budget}, ensure_ascii=False)},
    ]
    try:
        data = provider.extract_structured_data(prompt, schema, model=model)
        return data, False, "", dict(getattr(provider, "last_usage", {}) or {})
    except ProviderError as exc:
        fallback = str(getattr(settings, "AGENT_FALLBACK_MODEL", "") or "").strip()
        if fallback and fallback != model:
            try:
                data = provider.extract_structured_data(prompt, schema, model=fallback)
                return data, True, "", dict(getattr(provider, "last_usage", {}) or {})
            except ProviderError as fallback_exc:
                return {}, True, getattr(fallback_exc, "code", "provider_fallback_failed"), dict(getattr(provider, "last_usage", {}) or {})
        return {}, False, getattr(exc, "code", "provider_failed"), dict(getattr(provider, "last_usage", {}) or {})


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _finding(severity, category, title, explanation, evidence=None, questions=None, confidence=1.0, resolution=None):
    return {
        "severity": severity, "category": category, "title": title, "explanation": explanation,
        "evidence": evidence or [], "suggested_questions": questions or [], "confidence": confidence,
        **({"suggested_resolution": resolution} if resolution else {}),
    }



def _revision_refs(workspace, branch, canvas=None):
    """Return durable revision references used by every Agent evidence row."""
    refs = []
    if branch and branch.base_commit:
        refs.append({"source_type": "git_commit", "source_id": str(branch.base_commit), "field_path": "base_commit", "source_revision": str(branch.base_commit), "excerpt": "branch base commit", "relevance": 0.8})
    elif not branch:
        latest = Entity.objects.filter(workspace=workspace, branch__isnull=True).exclude(commit_hash="").order_by("-updated_at").first()
        if latest and latest.commit_hash:
            refs.append({"source_type": "git_commit", "source_id": latest.commit_hash, "field_path": "commit_hash", "source_revision": latest.commit_hash, "excerpt": "latest durable entity commit", "relevance": 0.7})
    if canvas is not None:
        revision = int(getattr(canvas, "snapshot_version", 0) or 0)
        if revision:
            refs.append({"source_type": "snapshot", "source_id": str(canvas.id), "field_path": "snapshot_version", "source_revision": f"canvas:{canvas.id}:v{revision}", "excerpt": "canvas snapshot revision", "relevance": 0.7})
    return refs


def _issue_category(issue):
    code = str(issue.get("code") or "")
    if code.startswith("temporal") or code in {"invalid_time_range", "timeline_without_events", "event_without_participants", "character_without_lifespan", "relation_outside_lifespan", "dangling_timeline_entry"}:
        return "temporal"
    if code.startswith("branch_"):
        return "branch"
    if code in {"archived_entity_referenced", "dangling_relation", "invalid_relation_time_range", "conflict_relation_count"} or issue.get("relation_id"):
        return "relation"
    if code in {"orphan_entity", "unlinked_floating_tip", "empty_content", "timeline_without_events"}:
        return "missing_context"
    return "identity"

def analyze_workspace(workspace: WorldWorkspace, branch: WorldBranch | None, mode: str = "consistency"):
    """Return stable findings and evidence for one effective branch view."""
    findings = []
    evidence = []
    report = build_consistency_report(workspace, branch)
    for issue in report.get("issues", []):
        refs = []
        for key, source_type in (("entity_id", "entity"), ("relation_id", "relation")):
            if issue.get(key):
                ref = {"source_type": source_type, "source_id": str(issue[key]), "field_path": "", "excerpt": issue.get("message", ""), "relevance": 1.0}
                refs.append(ref); evidence.append(ref)
        metadata = issue.get("metadata") if isinstance(issue.get("metadata"), dict) else {}
        for extra in metadata.get("evidence", []):
            if isinstance(extra, dict):
                refs.append(extra)
                evidence.append(extra)
        resolution = None
        if issue.get("code") in {"branch_main_overlap", "duplicate_canonical_identity", "archived_entity_referenced", "relation_outside_lifespan"}:
            resolution = {"kind": "review", "target_ids": [str(issue[key]) for key in ("entity_id", "relation_id") if issue.get(key)], "requires_user_confirmation": True}
        findings.append(_finding(
            "error" if issue.get("severity") == "error" else "warning", _issue_category(issue),
            issue.get("code", "consistency_issue"), issue.get("message", "一致性问题"), refs,
            ["你希望保留哪一条设定？", "是否要把这个冲突转成待审核提案？"], 0.98, resolution,
        ))

    if mode in ("consistency", "timeline"):
        lifespans, entries = _temporal_rows(workspace, branch)
        lifespans = list(lifespans)
        entries = list(entries)
        life_by_key = {(str(x.character_id), str(x.time_system_id)): x for x in lifespans}
        for entry in entries:
            participants = list(entry.participations.select_related("entity"))
            for participation in participants:
                life = life_by_key.get((str(participation.entity_id), str(entry.time_system_id)))
                if not life or life.start_value is None:
                    continue
                end = entry.end_value if entry.end_value is not None else entry.start_value
                if entry.start_value < life.start_value or (life.end_value is not None and end > life.end_value):
                    refs = [
                        {"source_type": "event", "source_id": str(entry.event_id), "field_path": "start_value/end_value", "excerpt": f"{entry.start_value}..{end}", "relevance": 1.0},
                        {"source_type": "lifespan", "source_id": str(life.id), "field_path": "start_value/end_value", "excerpt": f"{life.start_value}..{life.end_value}", "relevance": 1.0},
                    ]
                    evidence.extend(refs)
                    findings.append(_finding(
                        "error", "temporal", "人物生命周期与事件冲突",
                        f"{participation.entity.title} 参与“{entry.event.title}”的时间超出其生命周期。",
                        refs, ["事件发生在生命周期之外，是否需要调整事件时间？", "是否这是一个化名、继承者或跨时间体系的角色？"], 0.99,
                    ))

        effective = list(effective_relations(workspace, branch))
        entity_ids = {str(e.id) for e in effective_entities(workspace, branch)}
        for relation in effective:
            if str(relation.source_id) not in entity_ids or str(relation.target_id) not in entity_ids:
                ref = {"source_type": "relation", "source_id": str(relation.id), "field_path": "source/target", "excerpt": "relation endpoint is outside branch view", "relevance": 1.0}
                evidence.append(ref)
                findings.append(_finding("error", "relation", "关系引用了当前分支之外的实体", "该关系在当前分支不可解析。", [ref], ["要补入实体、改换引用，还是归档关系？"], 0.99))

        # deterministic causal ordering warning: an event cannot end before another
        # event with an explicit prerequisite relation on the same axis.
        by_event = {str(e.event_id): e for e in entries}
        for rel in effective:
            if rel.relation_type not in (Relation.RelationType.DERIVES_FROM, Relation.RelationType.INFLUENCES):
                continue
            before = by_event.get(str(rel.source_id)); after = by_event.get(str(rel.target_id))
            if before and after and before.start_value < after.start_value:
                refs = [{"source_type": "relation", "source_id": str(rel.id), "field_path": "relation_type", "excerpt": rel.relation_type, "relevance": 0.8}]
                evidence.extend(refs)
                findings.append(_finding("warning", "temporal", "事件顺序与关系方向值得复核", "关系方向暗示的先后与时间线排序相反。", refs, ["关系方向是否应该反转？", "是否存在未记录的中间事件？"], 0.82))

    evidence.extend(_revision_refs(workspace, branch))

    # Include explicit conversion records when multiple chronology systems are
    # present; this gives the timeline Agent a citable basis rather than an
    # unexplained model guess.
    conversions = TimeSystemConversion.objects.filter(workspace=workspace).order_by("source_system_id", "target_system_id")
    for conversion in conversions:
        evidence.append({"source_type": "timeline", "source_id": str(conversion.id), "field_path": "numerator/denominator/offset", "source_revision": str(conversion.updated_at.isoformat()), "excerpt": f"{conversion.source_system_id}->{conversion.target_system_id}: {conversion.numerator}/{conversion.denominator}+{conversion.offset}", "relevance": 0.5})

    # deterministic stable order makes results cacheable and easy to diff
    findings.sort(key=lambda x: (x["severity"], x["category"], x["title"], json.dumps(x.get("evidence", []), sort_keys=True)))
    evidence = sorted({json.dumps(x, sort_keys=True): x for x in evidence}.values(), key=lambda x: (x.get("source_type", ""), x.get("source_id", ""), x.get("field_path", "")))
    return {"branch": branch.name if branch else "main", "mode": mode, "findings": findings, "evidence": evidence, "summary": {"total": len(findings), "errors": sum(x["severity"] == "error" for x in findings), "warnings": sum(x["severity"] == "warning" for x in findings)}}


def _bounded_result(result, token_budget):
    """Keep deterministic findings within the run's hard output budget."""
    raw = json.dumps(result, ensure_ascii=False, default=str)
    max_chars = max(2048, int(token_budget) * 4)
    if len(raw) <= max_chars:
        return result, False
    findings = list(result.get("findings", []))
    kept = []
    used = 0
    for item in findings:
        size = len(json.dumps(item, ensure_ascii=False, default=str))
        if kept and used + size > max_chars:
            break
        kept.append(item); used += size
    result = {**result, "findings": kept, "summary": {**result.get("summary", {}), "total": len(kept), "truncated": True}, "validation_errors": ["token_budget_exceeded"]}
    return result, True


def _raise_if_cancelled(run):
    # Refresh only the small status field so a worker can observe cancellation
    # without replacing the in-memory result or evidence.
    if AgentRun.objects.filter(id=run.id, status=AgentRun.Status.CANCELLED).exists():
        raise AgentCancelledError("agent_run_cancelled")


def run_analysis(*, workspace, branch, user, session=None, canvas=None, mode="consistency", model="", token_budget=6000, context_revision=0):
    token_budget = max(512, min(int(token_budget or 6000), int(getattr(settings, "AGENT_MAX_TOKEN_BUDGET", 32000))))
    if mode not in {"consistency", "timeline", "maintenance"}:
        raise ValueError("unsupported analysis mode")
    requested_revision = int(context_revision or 0)
    current_revision = _context_revision(canvas=canvas, session=session)
    started = time.monotonic()
    payload = {"workspace": str(workspace.id), "branch": str(branch.id) if branch else "main", "mode": mode, "context_revision": requested_revision}
    run = AgentRun.objects.create(
        workspace=workspace, branch=branch, dialogue_session=session, canvas=canvas,
        mode=mode, status=AgentRun.Status.QUEUED, model=model,
        input_hash=_hash(payload), prompt_hash=_hash({"mode": mode, "budget": token_budget}),
        token_budget=token_budget, context_revision=requested_revision, created_by=user,
    )
    keys = None
    try:
        try:
            keys = _try_acquire(workspace, branch, user)
        except AgentBusyError as exc:
            run.status = AgentRun.Status.FAILED
            run.error_code = str(exc)
            run.validation_errors = [str(exc)]
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "error_code", "validation_errors", "completed_at"])
            raise
        run.status = AgentRun.Status.RUNNING
        run.save(update_fields=["status"])
        _raise_if_cancelled(run)
        from .tools import AuditedToolbox
        toolbox = AuditedToolbox(run, workspace, branch)
        toolbox.call("get_branch_context")
        toolbox.call("get_timeline_snapshot")
        toolbox.call("get_git_commit_context")
        if mode in {"consistency", "timeline"}:
            toolbox.call("find_temporal_conflicts")
            toolbox.call("find_relation_conflicts")
            toolbox.call("find_missing_timeline_events")
        toolbox.call("find_orphan_entities")
        result = analyze_workspace(workspace, branch, mode)
        _raise_if_cancelled(run)
        validation_errors = []
        if requested_revision and current_revision and requested_revision != current_revision:
            validation_errors.append("stale_context_revision")
            result.setdefault("findings", []).append(_finding(
                "warning", "missing_context", "分析上下文已过期",
                f"请求基于 revision {requested_revision}，当前画布 revision 为 {current_revision}；请在提交前重新分析。",
                questions=["是否重新加载当前画布后再分析？"], confidence=1.0,
            ))
        advisory, fallback_used, provider_error, usage = _llm_explanation(result, model, token_budget)
        _raise_if_cancelled(run)
        if advisory:
            result["advisory"] = advisory
        if provider_error:
            validation_errors.append(provider_error)
        result, truncated = _bounded_result(result, token_budget)
        if truncated:
            validation_errors.append("token_budget_exceeded")
        result["validation_errors"] = sorted(set(validation_errors))
        for item in result["evidence"]:
            AgentEvidence.objects.create(run=run, source_type=item["source_type"], source_id=item.get("source_id", ""), field_path=item.get("field_path", ""), source_revision=item.get("source_revision") or str(current_revision or requested_revision or ""), excerpt=item.get("excerpt", ""), relevance=float(item.get("relevance", 1.0)))
        run.output_json = result
        run.status = AgentRun.Status.COMPLETED
        run.validation_errors = result.get("validation_errors", [])
        run.fallback_used = bool(fallback_used)
        run.latency_ms = int((time.monotonic() - started) * 1000)
        run.input_tokens = int(usage.get("prompt_tokens") or min(token_budget, max(1, len(json.dumps(payload)) // 4)))
        run.output_tokens = int(usage.get("completion_tokens") or min(token_budget, max(1, len(json.dumps(result, ensure_ascii=False)) // 4)))
        run.completed_at = timezone.now()
        run.save(update_fields=["output_json", "status", "validation_errors", "fallback_used", "latency_ms", "input_tokens", "output_tokens", "completed_at"])
        return run, result
    except AgentBusyError:
        raise
    except AgentCancelledError:
        run.refresh_from_db(fields=["status"])
        if run.status != AgentRun.Status.CANCELLED:
            run.status = AgentRun.Status.CANCELLED
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "completed_at"])
        return run, {"status": AgentRun.Status.CANCELLED, "validation_errors": ["agent_run_cancelled"]}
    except Exception as exc:
        run.status = AgentRun.Status.FAILED
        run.error_code = type(exc).__name__
        run.validation_errors = [str(exc)]
        run.latency_ms = int((time.monotonic() - started) * 1000)
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_code", "validation_errors", "latency_ms", "completed_at"])
        raise
    finally:
        _release(keys)

