"""Deterministic, bounded long-term memory for the worldbuilding agent.

Memory is deliberately split into three trust levels:

* ``confirmed_facts`` is rebuilt only from the effective formal Entity/Relation
  rows of the selected branch.
* ``working_notes`` contains pending proposals and is never presented as canon.
* ``open_questions`` contains non-blocking questions surfaced by dialogue.

This first implementation is intentionally deterministic. It provides durable
cross-session context without allowing an LLM to silently promote its own text
or a user's draft into the formal world model.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.canvas.models import DialogueMemory, DialogueSession, EntityProposal
from apps.core.services.branching import effective_entities, effective_relations


MAX_FACTS = 160
MAX_RELATIONS = 320
MAX_WORKING_NOTES = 40
MAX_OPEN_QUESTIONS = 20
MAX_RECENT_MESSAGES = 8
MAX_TEXT = 1200


def scope_key(workspace_id: Any, branch_id: Any | None = None) -> str:
    return f"workspace:{workspace_id}:branch:{branch_id or 'main'}"


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def get_memory(session: DialogueSession) -> DialogueMemory:
    """Return the memory record for the session's workspace/branch scope."""
    canvas = session.canvas
    branch = canvas.branch if canvas is not None else None
    key = scope_key(session.workspace_id, branch.id if branch else None)
    memory, _ = DialogueMemory.objects.get_or_create(
        scope_key=key,
        defaults={
            "workspace_id": session.workspace_id,
            "branch": branch,
        },
    )
    # A malformed/legacy record must not leak across workspaces. The unique key
    # normally makes this unnecessary, but the explicit check keeps recovery
    # deterministic if data was imported manually.
    expected_branch_id = branch.id if branch else None
    if str(memory.workspace_id) != str(session.workspace_id) or str(memory.branch_id) != str(expected_branch_id):
        raise ValueError("dialogue memory scope does not match session")
    return memory


def _confirmed_facts(session: DialogueSession) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    canvas = session.canvas
    branch = canvas.branch if canvas is not None else None
    entities = effective_entities(session.workspace, branch)
    relations = effective_relations(session.workspace, branch)
    entity_rows = [
        {
            "kind": "entity",
            "id": str(entity.id),
            "entity_type": entity.type,
            "title": _text(entity.title, 500),
            "content": _text(entity.content),
            "status": entity.status,
        }
        for entity in entities[:MAX_FACTS]
    ]
    entity_by_id = {entity.id: entity for entity in entities}
    relation_rows = []
    for relation in relations[:MAX_RELATIONS]:
        source = entity_by_id.get(relation.source_id)
        target = entity_by_id.get(relation.target_id)
        relation_rows.append(
            {
                "kind": "relation",
                "id": str(relation.id),
                "source_id": str(relation.source_id),
                "source_title": _text(source.title if source else relation.source.title, 500),
                "target_id": str(relation.target_id),
                "target_title": _text(target.title if target else relation.target.title, 500),
                "relation_type": relation.relation_type,
                "valid_from": relation.valid_from,
                "valid_to": relation.valid_to,
                "properties": relation.properties or {},
            }
        )
    return entity_rows, relation_rows


def _working_notes(session: DialogueSession) -> list[dict[str, Any]]:
    canvas = session.canvas
    if canvas is None:
        return []
    proposals = canvas.entity_proposals.filter(
        status=EntityProposal.Status.PENDING,
    ).order_by("-created_at")[:MAX_WORKING_NOTES]
    return [
        {
            "kind": "pending_proposal",
            "id": str(proposal.id),
            "entity_type": proposal.entity_type,
            "title": _text(proposal.title, 500),
            "content": _text(proposal.content),
            "source": proposal.source,
            "created_at": _iso(proposal.created_at),
        }
        for proposal in proposals
    ]


def _question_from_session(session: DialogueSession) -> dict[str, Any] | None:
    raw = (session.context or {}).get("question")
    if not isinstance(raw, dict):
        return None
    text = _text(raw.get("text"), 600)
    if not text:
        return None
    status = _text(raw.get("status"), 30) or "pending"
    # Closed questions stay auditable in the session context, but must not
    # reappear as active prompts after a refresh. Deferred questions remain in
    # the long-term memory list and can be restored from there.
    if status in {"skipped", "answered", "archived"}:
        return None
    return {
        "text": text,
        "reason": _text(raw.get("reason"), 600),
        "priority": _text(raw.get("priority"), 40) or "expansion",
        "status": status,
        "session_id": str(session.id),
        # Use the session's persisted timestamp instead of ``now()`` so a
        # read/refresh is idempotent and does not create phantom revisions.
        "updated_at": session.updated_at.isoformat() if session.updated_at else timezone.now().isoformat(),
    }


def _open_questions(memory: DialogueMemory, session: DialogueSession) -> list[dict[str, Any]]:
    archived = {
        _text(item.get("text"), 600)
        for item in (memory.archived_questions or [])
        if isinstance(item, dict) and _text(item.get("text"), 600)
    }
    values: list[dict[str, Any]] = []
    for item in memory.open_questions or []:
        if isinstance(item, dict) and _text(item.get("text"), 600) and _text(item.get("text"), 600) not in archived:
            values.append(item)

    # A session may close its current question (skip/answer/archive) without
    # adding it to ``archived_questions``. Remove the session's durable question
    # record before deciding whether to reinsert its active/deferred form; this
    # prevents a previously pending copy from surviving a status transition.
    raw_question = (session.context or {}).get("question")
    session_text = _text(raw_question.get("text"), 600) if isinstance(raw_question, dict) else ""
    if session_text:
        values = [item for item in values if _text(item.get("text"), 600) != session_text]

    current = _question_from_session(session)
    if current and current["text"] not in archived:
        values.insert(0, current)
    return values[:MAX_OPEN_QUESTIONS]


def _recent_summary(session: DialogueSession) -> str:
    messages = list(session.messages.order_by("-created_at")[:MAX_RECENT_MESSAGES])[::-1]
    if not messages:
        return ""
    lines = []
    for message in messages:
        role = {"user": "用户", "assistant": "助手", "system": "系统"}.get(message.role, message.role)
        lines.append(f"{role}：{_text(message.content, 500)}")
    return "\n".join(lines)[:7000]


def _summary(session: DialogueSession, entities: list[dict[str, Any]], relations: list[dict[str, Any]], notes: list[dict[str, Any]]) -> str:
    canvas = session.canvas
    branch = canvas.branch if canvas is not None else None
    branch_name = branch.name if branch is not None else "main"
    return (
        f"当前记忆范围：{branch_name}。已确认正式实体 {len(entities)} 个、正式关系 {len(relations)} 条；"
        f"待审核草稿 {len(notes)} 个。正式事实来自当前分支的 PostgreSQL 有效视图，"
        "待审核草稿和问题不会自动成为正式设定。"
    )


@transaction.atomic
def refresh_memory(session: DialogueSession) -> DialogueMemory:
    """Rebuild bounded memory from authoritative state and recent dialogue."""
    memory = get_memory(session)
    entities, relations = _confirmed_facts(session)
    notes = _working_notes(session)
    questions = _open_questions(memory, session)
    derived = {
        "confirmed_facts": entities + relations,
        "working_notes": notes,
        "open_questions": questions,
        "recent_session_summary": _recent_summary(session),
        "summary": _summary(session, entities, relations, notes),
    }
    changed = any(getattr(memory, key) != value for key, value in derived.items())
    if changed:
        for key, value in derived.items():
            setattr(memory, key, value)
        memory.revision = int(memory.revision or 0) + 1
        memory.save(update_fields=[*derived.keys(), "revision", "updated_at"])
    return memory


def memory_editable_payload(memory: DialogueMemory) -> dict[str, Any]:
    """Return only fields that a user is allowed to edit or audit."""
    return {
        "manual_notes": memory.manual_notes or [],
        "archived_questions": memory.archived_questions or [],
    }


def memory_payload(memory: DialogueMemory) -> dict[str, Any]:
    """Serialize a memory record without exposing internal model details."""
    return {
        "scope_key": memory.scope_key,
        "revision": memory.revision,
        "summary": memory.summary,
        "confirmed_facts": memory.confirmed_facts or [],
        "working_notes": memory.working_notes or [],
        "open_questions": memory.open_questions or [],
        "recent_session_summary": memory.recent_session_summary,
        "manual_notes": memory.manual_notes or [],
        "archived_questions": memory.archived_questions or [],
        "updated_at": memory.updated_at,
    }


def context_payload(session: DialogueSession) -> dict[str, Any]:
    """Return a bounded, read-only payload suitable for an LLM context."""
    # Re-read authoritative branch state whenever context is requested so a
    # commit performed in another session becomes visible without waiting for
    # another dialogue turn. This also preserves the formal/draft boundary.
    memory = refresh_memory(session)
    return {
        "scope_key": memory.scope_key,
        "revision": memory.revision,
        "summary": memory.summary,
        "confirmed_facts": (memory.confirmed_facts or [])[:MAX_FACTS + MAX_RELATIONS],
        "working_notes": (memory.working_notes or [])[:MAX_WORKING_NOTES],
        "open_questions": (memory.open_questions or [])[:MAX_OPEN_QUESTIONS],
        "recent_session_summary": memory.recent_session_summary[:7000],
        "manual_notes": (memory.manual_notes or [])[:MAX_WORKING_NOTES],
    }
