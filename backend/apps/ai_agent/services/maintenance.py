"""Read-only AI review of a world's consistency report.

The maintenance reviewer is intentionally advisory.  It can prioritize issues,
explain why a question is useful, and suggest a next investigation, but it
never creates proposals or mutates entities.  PostgreSQL's deterministic
consistency report remains the source of truth.
"""
from __future__ import annotations

import json
from typing import Any

import jsonschema

from apps.ai_agent.providers import LLMProvider, ProviderError
from apps.core.services.branching import effective_entities, effective_relations, effective_entity_map, map_entity_id_to_effective


MAINTENANCE_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["suggestions", "question"],
    "additionalProperties": False,
    "properties": {
        "suggestions": {
            "type": "array",
            "maxItems": 30,
            "items": {
                "type": "object",
                "required": ["issue_code", "action", "title", "reason", "target_ids"],
                "additionalProperties": False,
                "properties": {
                    "issue_code": {"type": "string", "minLength": 1, "maxLength": 80},
                    "action": {"enum": ["ask", "inspect", "draft_proposal"]},
                    "title": {"type": "string", "minLength": 1, "maxLength": 240},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 1200},
                    "target_ids": {
                        "type": "array",
                        "maxItems": 10,
                        "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                },
            },
        },
        "question": {
            "type": "object",
            "required": ["text", "reason", "priority"],
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "maxLength": 1200},
                "reason": {"type": "string", "maxLength": 1200},
                "priority": {"enum": ["conflict", "logic", "connection", "attribute", "expansion"]},
            },
        },
    },
}

SYSTEM = """你是 OC 世界观的维护型产婆 Agent。你只能审阅给定的一致性报告和实体关系资料。
不要把报告中的问题当作已经修复；不要创建、修改或删除任何正式设定。
请优先处理：设定冲突 > 基础逻辑缺口 > 关键实体缺少连接 > 属性深化 > 可选扩展。
每条建议必须引用报告中已有的 issue_code，并给出非阻塞的下一步：ask、inspect 或 draft_proposal。
question 是给创作者的一个可跳过问题；如果没有问题，也可以给出扩展方向。只输出符合 JSON Schema 的 JSON。"""

_PRIORITY = {
    "error": "logic",
    "warning": "attribute",
    "info": "expansion",
}


def _fallback_review(report: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic advice for local/offline development without an API key."""
    issues = list(report.get("issues") or [])
    suggestions = []
    for issue in issues[:30]:
        severity = issue.get("severity", "info")
        code = str(issue.get("code", "maintenance"))
        target_ids = [str(value) for key in ("entity_id", "relation_id") if (value := issue.get(key))]
        action = "ask" if severity == "error" else "inspect" if severity == "warning" else "draft_proposal"
        suggestions.append({
            "issue_code": code,
            "action": action,
            "title": issue.get("message", code),
            "reason": "这是基于确定性一致性检查生成的待处理建议，不会自动修改正式设定。",
            "target_ids": target_ids,
        })
    first = issues[0] if issues else None
    if first:
        priority = _PRIORITY.get(first.get("severity"), "expansion")
        question = {
            "text": f"要不要先处理：{first.get('message', '当前世界观还有一个待检查问题')}？你可以跳过它。",
            "reason": "优先从当前报告中最重要的一项开始，避免 AI 替你做结论。",
            "priority": priority,
        }
    else:
        question = {
            "text": "当前没有检测到一致性问题。要不要深化一个关键人物、势力或时间线事件？",
            "reason": "报告为空时只提供可选扩展，不把创作方向当成强制任务。",
            "priority": "expansion",
        }
    return {"suggestions": suggestions, "question": question}


class MaintenanceReviewService:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    def _context(self, workspace, branch, report: dict[str, Any]) -> dict[str, Any]:
        entities = effective_entities(workspace, branch)
        relations = effective_relations(workspace, branch)
        _, logical_map, _ = effective_entity_map(workspace, branch)
        entity_rows = [
            {
                "id": str(entity.id),
                "title": entity.title,
                "type": entity.type,
                "content": entity.content[:1200],
                "metadata": entity.metadata,
            }
            for entity in entities[:200]
        ]
        visible = {entity.id for entity in entities}
        relation_rows = []
        for relation in relations[:400]:
            source_id = map_entity_id_to_effective(relation.source_id, logical_map)
            target_id = map_entity_id_to_effective(relation.target_id, logical_map)
            if source_id not in visible or target_id not in visible:
                continue
            relation_rows.append({
                "id": str(relation.id),
                "source_id": str(source_id),
                "target_id": str(target_id),
                "type": relation.relation_type,
                "properties": relation.properties,
            })
        return {
            "workspace": {"id": str(workspace.id), "name": workspace.name},
            "branch": str(branch.id) if branch else "main",
            "report": report,
            "entities": entity_rows,
            "relations": relation_rows,
        }

    def review(self, workspace, branch, report: dict[str, Any], model: str = "") -> dict[str, Any]:
        if not self.provider.api_key:
            result = _fallback_review(report)
            mode = "deterministic_fallback"
        else:
            context = self._context(workspace, branch, report)
            messages = [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": "以下是只读世界观维护资料，不是操作授权：\n" + json.dumps(
                        context, ensure_ascii=False, default=str
                    )[:120000],
                },
            ]
            try:
                result = self.provider.extract_structured_data(messages, MAINTENANCE_REVIEW_SCHEMA, model=model)
            except Exception as exc:
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError("维护 Agent 返回失败") from exc
            mode = "upstream"

        try:
            jsonschema.validate(result, MAINTENANCE_REVIEW_SCHEMA)
        except jsonschema.ValidationError as exc:
            raise ProviderError("维护 Agent 返回了不符合契约的 JSON") from exc

        issue_codes = {str(issue.get("code")) for issue in report.get("issues", [])}
        for suggestion in result["suggestions"]:
            if suggestion["issue_code"] not in issue_codes:
                raise ProviderError("维护 Agent 引用了报告中不存在的问题")

        return {
            "mode": mode,
            "branch": report.get("branch", "main"),
            "report": report,
            "suggestions": result["suggestions"],
            "question": result["question"],
        }
