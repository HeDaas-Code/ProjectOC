"""Read-only, branch-scoped tools exposed to the agent.

The functions in this module deliberately return JSON-compatible data only.
They never call ``save`` and therefore cannot mutate canon.  A caller can use
``AuditedToolbox`` to attach deterministic hashes and timings to an AgentRun.
"""
from __future__ import annotations

import hashlib
import json
import time

from apps.core.models import (
    WorldBranch,
    WorldWorkspace,
)
from apps.core.services.branching import effective_entities, effective_relations
from apps.core.services.consistency import _temporal_rows
from apps.version_control.services.git_sync import GitRepositoryService


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _entity(row):
    return {"id": str(row.id), "type": row.type, "title": row.title, "content": row.content, "metadata": row.metadata, "status": row.status}


def get_branch_context(workspace: WorldWorkspace, branch: WorldBranch | None = None):
    entities = effective_entities(workspace, branch)
    relations = effective_relations(workspace, branch)
    return {"branch": branch.name if branch else "main", "entities": [_entity(x) for x in entities], "relations": [{"id": str(r.id), "source": str(r.source_id), "target": str(r.target_id), "type": r.relation_type, "properties": r.properties} for r in relations]}


def get_entity(workspace, entity_id, branch=None):
    for row in effective_entities(workspace, branch):
        if str(row.id) == str(entity_id) or str(row.base_entity_id or "") == str(entity_id):
            return _entity(row)
    return None


def get_links(workspace, entity_id, branch=None):
    wanted = {str(entity_id)}
    entities = effective_entities(workspace, branch)
    for row in entities:
        if str(row.base_entity_id or "") == str(entity_id):
            wanted.add(str(row.id))
    return [{"id": str(r.id), "source": str(r.source_id), "target": str(r.target_id), "type": r.relation_type, "direction": "out"} for r in effective_relations(workspace, branch) if str(r.source_id) in wanted]


def get_backlinks(workspace, entity_id, branch=None):
    wanted = {str(entity_id)}
    for row in effective_entities(workspace, branch):
        if str(row.base_entity_id or "") == str(entity_id):
            wanted.add(str(row.id))
    return [{"id": str(r.id), "source": str(r.source_id), "target": str(r.target_id), "type": r.relation_type, "direction": "in"} for r in effective_relations(workspace, branch) if str(r.target_id) in wanted]


def get_timeline_snapshot(workspace, branch=None, timeline_id=None):
    lifespans, entries = _temporal_rows(workspace, branch)
    rows = []
    for entry in entries:
        if timeline_id and str(entry.timeline_id) != str(timeline_id):
            continue
        rows.append({"id": str(entry.id), "timeline": str(entry.timeline_id), "event": str(entry.event_id), "start_value": entry.start_value, "end_value": entry.end_value, "sequence": entry.sequence, "summary": entry.summary, "participants": [{"entity": str(p.entity_id), "role": p.role} for p in entry.participations.all()]})
    return {"branch": branch.name if branch else "main", "entries": sorted(rows, key=lambda x: (x["start_value"], x["sequence"], x["id"])), "lifespans": [{"id": str(x.id), "character": str(x.character_id), "time_system": str(x.time_system_id), "start_value": x.start_value, "end_value": x.end_value} for x in lifespans]}


def compare_timeline_snapshots(before, after):
    def keyed(rows): return {str(x.get("id")): x for x in rows if isinstance(x, dict) and x.get("id")}
    result = {}
    for key in ("entries", "lifespans"):
        a, b = keyed(before.get(key, [])), keyed(after.get(key, []))
        result[key] = {"added": [b[k] for k in sorted(b.keys()-a.keys())], "removed": [a[k] for k in sorted(a.keys()-b.keys())], "changed": [{"id": k, "before": a[k], "after": b[k]} for k in sorted(a.keys() & b.keys()) if a[k] != b[k]]}
    return result


def get_git_commit_context(workspace, branch=None, commit=None):
    """Read only the durable Git context for an Agent evidence trail."""
    service = GitRepositoryService(workspace)
    if not service.root.exists() or not (service.root / ".git").exists():
        return {"commit": "", "files": [], "branch": branch.name if branch else "main", "status": "repository_unavailable"}
    target = str(commit or (branch.base_commit if branch else "") or "").strip()
    if not target:
        target = service._run(["rev-parse", "--verify", "main^{commit}"], check=False)
    if not target:
        return {"commit": "", "files": [], "branch": branch.name if branch else "main"}
    resolved = service._run(["rev-parse", "--verify", f"{target}^{{commit}}"], check=False)
    if not resolved:
        return {"commit": "", "files": [], "branch": branch.name if branch else "main", "error": "commit_not_found"}
    files = service._run(["show", "--format=", "--name-only", resolved], check=False).splitlines()
    return {"commit": resolved, "branch": branch.name if branch else "main", "files": sorted({x.strip() for x in files if x.strip()}), "subject": service._run(["show", "-s", "--format=%s", resolved], check=False)}


def find_missing_timeline_events(workspace, branch=None):
    """Generate only proposal-shaped hints; never create an event."""
    snapshot = get_timeline_snapshot(workspace, branch)
    entries = snapshot.get("entries", [])
    hints = []
    for current in entries:
        for other in entries:
            if current["id"] == other["id"] or current["event"] == other["event"]:
                continue
            if current["start_value"] < other["start_value"] and current["sequence"] > other["sequence"]:
                hints.append({"kind": "missing_intermediate_event", "before_entry": current["id"], "after_entry": other["id"], "proposal": {"entity_type": "event", "title": "待补充的中间事件", "content": "请确认两个事件之间是否存在尚未记录的因果或过渡事件。", "requires_user_confirmation": True}})
    unique = {(x["before_entry"], x["after_entry"]): x for x in hints}
    return [unique[key] for key in sorted(unique)]

def find_temporal_conflicts(workspace, branch=None):
    from .analysis import analyze_workspace
    return [x for x in analyze_workspace(workspace, branch, "timeline")["findings"] if x["category"] == "temporal"]


def find_relation_conflicts(workspace, branch=None):
    from .analysis import analyze_workspace
    return [x for x in analyze_workspace(workspace, branch, "consistency")["findings"] if x["category"] == "relation"]


def find_orphan_entities(workspace, branch=None):
    entities = effective_entities(workspace, branch)
    links = effective_relations(workspace, branch)
    connected = {str(x.source_id) for x in links} | {str(x.target_id) for x in links}
    return [_entity(x) for x in entities if str(x.id) not in connected]


TOOL_FUNCTIONS = {name: globals()[name] for name in ("get_branch_context", "get_entity", "get_links", "get_backlinks", "get_timeline_snapshot", "compare_timeline_snapshots", "get_git_commit_context", "find_missing_timeline_events", "find_temporal_conflicts", "find_relation_conflicts", "find_orphan_entities")}


class AuditedToolbox:
    def __init__(self, run, workspace, branch=None):
        self.run, self.workspace, self.branch = run, workspace, branch

    def call(self, name: str, **arguments):
        if name not in TOOL_FUNCTIONS:
            raise ValueError(f"unknown read-only tool: {name}")
        started = time.monotonic()
        args = {**arguments}
        # Public tool arguments cannot replace the authenticated workspace or branch.
        args.pop("workspace", None); args.pop("branch", None)
        try:
            result = TOOL_FUNCTIONS[name](self.workspace, branch=self.branch, **args)
            from apps.ai_agent.models import AgentToolCallAudit
            AgentToolCallAudit.objects.create(run=self.run, tool_name=name, arguments_hash=_hash(args), result_hash=_hash(result), status="completed", latency_ms=int((time.monotonic()-started)*1000))
            return result
        except Exception as exc:
            from apps.ai_agent.models import AgentToolCallAudit
            AgentToolCallAudit.objects.create(run=self.run, tool_name=name, arguments_hash=_hash(args), result_hash="", status="failed", error_code=type(exc).__name__, latency_ms=int((time.monotonic()-started)*1000))
            raise
