"""World-content repository export and replayable Git synchronization."""
from __future__ import annotations

import json
import re
import subprocess
import fcntl
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from apps.core.models import CharacterLifespan, Entity, Relation, TimeSystem, TimeSystemConversion, TimelineEntry, WorldBranch
from apps.core.services.branching import effective_entity_map, effective_entities, effective_relations, map_entity_id_to_effective


TYPE_DIRECTORIES = {
    "canonical_setting": "settings",
    "character": "characters",
    "timeline": "timelines",
    "event": "events",
    "floating_tip": "floating_tips",
    "item": "settings/items",
    "location": "settings/locations",
    "faction": "settings/factions",
}
REPOSITORY_DIRECTORIES = [
    "settings",
    "settings/items",
    "settings/locations",
    "settings/factions",
    "characters",
    "timelines",
    "events",
    "floating_tips",
    "indexes",
]


class GitRepositoryService:
    def __init__(self, workspace):
        self.workspace = workspace
        base = settings.WORLD_REPOS_ROOT.resolve()
        configured = Path(workspace.repo_path).expanduser() if workspace.repo_path else Path(workspace.slug)
        self.root = (configured if configured.is_absolute() else base / configured).resolve()
        if not self.root.is_relative_to(base):
            raise ValueError("Invalid repository path")

    def _run(self, args, check=True):
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or "Git command failed")
        return result.stdout.strip()

    def ensure_repository(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in REPOSITORY_DIRECTORIES:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self._run(["init", "-q", "--initial-branch=main"])

    @contextmanager
    def repository_lock(self):
        lock_root = settings.WORLD_REPOS_ROOT.resolve() / ".locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = lock_root / f"{self.workspace.id}.lock"
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def ensure_main_baseline(self):
        """Materialize PostgreSQL main state as a real, stable Git baseline."""
        with self.repository_lock():
            return self._ensure_main_baseline_locked()

    def _ensure_main_baseline_locked(self):
        self.ensure_repository()
        has_head = bool(self._run(["rev-parse", "--verify", "HEAD"], check=False))
        if has_head:
            has_main = bool(self._run(["show-ref", "--verify", "refs/heads/main"], check=False))
            if not has_main:
                self._run(["branch", "-M", "main"])
            elif self._run(["branch", "--show-current"], check=False) != "main":
                self._run(["checkout", "main"])
        else:
            self._run(["symbolic-ref", "HEAD", "refs/heads/main"])
        self.write_snapshot(self.snapshot())
        self._run(["add", "-A"])
        self._commit_if_changed("Initialize or refresh main world snapshot")
        return self._run(["rev-parse", "HEAD"])

    def _commit_exists(self, commit):
        return bool(commit and self._run(["rev-parse", "--verify", f"{commit}^{{commit}}"], check=False))

    def find_commit_by_marker(self, job, ref=None):
        """Find a previously-created commit for a durable job marker.

        Git commits are the external side effect of a commit job. Looking up
        the marker before writing a new snapshot makes recovery after a
        process crash safe: a job whose commit succeeded but whose database
        status update did not complete is simply finalized from this result.
        """
        self.ensure_repository()
        marker = f"OC-Job: {job.id}"
        args = ["log", ref or "--all", "--fixed-strings", f"--grep={marker}", "--format=%H", "-1"]
        value = self._run(args, check=False)
        return value if re.fullmatch(r"[0-9a-f]{40,64}", value) else ""

    def ensure_branch_ref(self, branch):
        """Ensure a branch ref exists without moving an established baseline."""
        with self.repository_lock():
            self.ensure_repository()
            ref = branch.git_ref or f"oc/branch/{branch.pk.hex}"
            if self._run(["show-ref", "--verify", f"refs/heads/{ref}"], check=False):
                base_commit = branch.base_commit
                if not self._commit_exists(base_commit):
                    base_commit = self._run(["rev-parse", f"{ref}^{{commit}}"])
                return ref, base_commit

            # A missing ref on an existing row must be reconstructed from the
            # recorded baseline, never from today's moving main.
            base_commit = branch.base_commit if self._commit_exists(branch.base_commit) else self._ensure_main_baseline_locked()
            self._run(["branch", ref, base_commit])
            return ref, base_commit

    def prepare_branch(self, branch):
        """Create an isolated named ref from the exact current main baseline."""
        with self.repository_lock():
            self.ensure_repository()
            ref = branch.git_ref or f"oc/branch/{branch.pk.hex}"
            if self._run(["show-ref", "--verify", f"refs/heads/{ref}"], check=False):
                base_commit = branch.base_commit
                if not self._commit_exists(base_commit):
                    base_commit = self._run(["rev-parse", f"{ref}^{{commit}}"])
                return ref, base_commit
            base_commit = branch.base_commit if self._commit_exists(branch.base_commit) else self._ensure_main_baseline_locked()
            self._run(["branch", ref, base_commit])
            return ref, base_commit

    def branch_checkout(self, branch):
        ref = branch.git_ref or f"oc/branch/{branch.pk.hex}"
        checkout = (settings.WORLD_REPOS_ROOT.resolve() / ".worktrees" / str(self.workspace.id) / branch.pk.hex).resolve()
        if not checkout.is_relative_to(settings.WORLD_REPOS_ROOT.resolve()):
            raise ValueError("Invalid branch worktree path")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        if not (checkout / ".git").exists():
            self.ensure_repository()
            self._run(["worktree", "add", str(checkout), ref])
        service = GitRepositoryService(self.workspace)
        service.root = checkout
        return service

    def _clear_managed_snapshot(self):
        """Remove only files owned by the world export, never ``.git``.

        A branch snapshot is an overlay, not a second copy of main. Clearing
        managed paths before writing makes branch deletions real Git changes
        instead of leaving stale main files in a branch worktree.
        """
        self.ensure_repository()
        managed = [self.root / ".worldconfig", *(self.root / directory for directory in REPOSITORY_DIRECTORIES)]
        for path in managed:
            if path.is_dir():
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                    elif child.is_dir():
                        child.rmdir()
            elif path.exists():
                path.unlink()

    def write_snapshot(self, files):
        self._clear_managed_snapshot()
        for relative, content in files.items():
            relative_path = Path(relative)
            path = (self.root / relative_path).resolve()
            if not path.is_relative_to(self.root) or ".git" in relative_path.parts:
                raise ValueError("Unsafe export path")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def _commit_if_changed(self, message):
        staged_diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=self.root, timeout=30
        )
        if staged_diff.returncode == 0:
            return self._run(["rev-parse", "HEAD"], check=False)
        if staged_diff.returncode != 1:
            raise RuntimeError("Unable to inspect staged Git changes")
        self._run(["-c", "user.name=ProjectOC", "-c", "user.email=projectoc@localhost", "commit", "--allow-empty", "-m", message])
        return self._run(["rev-parse", "HEAD"])

    def snapshot(self, branch=None):
        """Export the effective world view for ``main`` or one branch.

        Main snapshots contain only main rows. Branch snapshots use the same
        copy-on-write overlay as REST and graph services: an override replaces
        its base row, a tombstone removes it, and branch-local rows are added.
        """
        files = {
            ".worldconfig": json.dumps(
                {
                    "id": str(self.workspace.id),
                    "name": self.workspace.name,
                    "version": 1,
                    "branch": str(branch.id) if branch is not None else "main",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        }
        entities = effective_entities(self.workspace, branch)
        if branch is None:
            logical_map = {entity.id: entity for entity in entities}
        else:
            _, logical_map, _ = effective_entity_map(self.workspace, branch)
        relations = effective_relations(self.workspace, branch)

        entity_rows = []
        for entity in sorted(entities, key=lambda row: str(row.id)):
            directory = TYPE_DIRECTORIES[entity.type]
            path = f"{directory}/{entity.id}.md"
            header = {
                "id": str(entity.id),
                "base_entity": str(entity.base_entity_id) if entity.base_entity_id else None,
                "branch": str(entity.branch_id) if entity.branch_id else "main",
                "type": entity.type,
                "title": entity.title,
                "status": entity.status,
                "metadata": entity.metadata,
            }
            files[path] = (
                "---\n"
                + json.dumps(header, ensure_ascii=False, indent=2)
                + "\n---\n\n"
                + entity.content.rstrip()
                + "\n"
            )
            entity_rows.append({
                **header,
                "content": entity.content,
            })
            Entity.objects.filter(id=entity.id).update(git_path=path)

        # Keep a compact, machine-readable entity index alongside the Markdown
        # files.  The Markdown remains the human-facing export; the index lets
        # historical views reconstruct titles/types without guessing from the
        # current PostgreSQL state.
        files["indexes/entities.json"] = json.dumps(
            entity_rows, indent=2, ensure_ascii=False
        ) + "\n"

        relation_rows = []
        for relation in sorted(relations, key=lambda row: str(row.id)):
            source_id = map_entity_id_to_effective(relation.source_id, logical_map)
            target_id = map_entity_id_to_effective(relation.target_id, logical_map)
            relation_rows.append({
                "id": str(relation.id),
                "base_relation": str(relation.base_relation_id) if relation.base_relation_id else None,
                "branch": str(relation.branch_id) if relation.branch_id else "main",
                "source": str(source_id),
                "target": str(target_id),
                "type": relation.relation_type,
                "properties": relation.properties,
                "weight": relation.weight,
                "time_system": str(relation.time_system_id) if relation.time_system_id else None,
                "valid_from": relation.valid_from,
                "valid_to": relation.valid_to,
                "archived": relation.archived,
            })
        files["indexes/relations.json"] = json.dumps(relation_rows, indent=2, ensure_ascii=False) + "\n"

        axes = TimeSystem.objects.filter(workspace=self.workspace).order_by("id")
        files["indexes/time-systems.json"] = json.dumps([
            {"id": str(axis.id), "name": axis.name, "epoch_label": axis.epoch_label,
             "unit_name": axis.unit_name, "units": axis.units, "calendar_rules": axis.calendar_rules,
             "display_format": axis.display_format}
            for axis in axes
        ], indent=2, ensure_ascii=False) + "\n"
        conversions = TimeSystemConversion.objects.filter(workspace=self.workspace).select_related("source_system", "target_system")
        files["indexes/time-system-conversions.json"] = json.dumps([
            {
                "id": str(conversion.id),
                "source_system": str(conversion.source_system_id),
                "target_system": str(conversion.target_system_id),
                "numerator": conversion.numerator,
                "denominator": conversion.denominator,
                "offset": conversion.offset,
                "label": conversion.label,
            }
            for conversion in conversions.order_by("id")
        ], indent=2, ensure_ascii=False) + "\n"
        entries = TimelineEntry.objects.filter(workspace=self.workspace)
        lifespans = CharacterLifespan.objects.filter(workspace=self.workspace)
        if branch is None:
            entries = entries.filter(branch__isnull=True, archived=False)
            lifespans = lifespans.filter(branch__isnull=True, archived=False)
        else:
            from django.db.models import Exists, OuterRef, Q
            branch_entry_override = TimelineEntry.objects.filter(
                branch=branch, timeline_id=OuterRef("timeline_id"),
                event_id=OuterRef("event_id"), time_system_id=OuterRef("time_system_id"),
            )
            branch_lifespan_override = CharacterLifespan.objects.filter(
                branch=branch, character_id=OuterRef("character_id"),
                time_system_id=OuterRef("time_system_id"),
            )
            entries = entries.filter(Q(branch=branch, archived=False) | (Q(branch__isnull=True, archived=False) & ~Exists(branch_entry_override)))
            lifespans = lifespans.filter(Q(branch=branch, archived=False) | (Q(branch__isnull=True, archived=False) & ~Exists(branch_lifespan_override)))
        files["indexes/timeline-entries.json"] = json.dumps([
            {"id": str(entry.id), "timeline": str(entry.timeline_id), "timeline_title": entry.timeline.title,
             "event": str(entry.event_id), "event_title": entry.event.title,
             "time_system": str(entry.time_system_id), "start_value": entry.start_value,
             "end_value": entry.end_value, "sequence": entry.sequence, "summary": entry.summary,
             "properties": entry.properties, "archived": entry.archived, "participants": [
                 {"entity": str(row.entity_id), "role": row.role}
                 for row in entry.participations.order_by("entity_id")
             ]}
            for entry in entries.select_related("timeline", "event", "time_system").order_by("id")
        ], indent=2, ensure_ascii=False) + "\n"
        files["indexes/character-lifespans.json"] = json.dumps([
            {"id": str(span.id), "character": str(span.character_id),
             "time_system": str(span.time_system_id), "start_value": span.start_value,
             "end_value": span.end_value, "properties": span.properties, "archived": span.archived}
            for span in lifespans.order_by("id")
        ], indent=2, ensure_ascii=False) + "\n"
        return files

    def commit_job(self, job):
        if job.branch_id:
            branch = job.branch
            ref, base_commit = self.prepare_branch(branch)
            if branch.git_ref != ref or branch.base_commit != base_commit:
                type(branch).objects.filter(pk=branch.pk).update(git_ref=ref, base_commit=base_commit)
                branch.git_ref, branch.base_commit = ref, base_commit
        with self.repository_lock():
            service = self.branch_checkout(job.branch) if job.branch_id else self
            return service._commit_job_locked(job)

    def _commit_job_locked(self, job):
        self.ensure_repository()
        marker = f"OC-Job: {job.id}"
        # A crash after the Git commit but before the DB update is safe to
        # replay: the marker identifies the already-created commit.
        previous = self.find_commit_by_marker(job, ref="HEAD")
        if previous:
            return previous

        self.write_snapshot(self.snapshot(branch=job.branch if job.branch_id else None))
        self._run(["add", "-A"])
        entities = list(Entity.objects.filter(id__in=job.entity_ids))
        types = ", ".join(sorted({entity.type for entity in entities})) or "relations"
        sessions = sorted(
            {
                entity.metadata.get("provenance", {}).get("session", "")
                for entity in entities
            }
            - {""}
        )
        message = (
            f"Confirm {types}: {len(job.entity_ids)} entities, "
            f"{len(job.relation_ids)} relations\n\n"
            f"Sessions: {', '.join(sessions) or 'manual'}\n{marker}"
        )
        self._run(
            [
                "-c",
                "user.name=ProjectOC",
                "-c",
                "user.email=projectoc@localhost",
                "commit",
                "--allow-empty",
                "-m",
                message,
            ]
        )
        return self._run(["rev-parse", "HEAD"])

    def status(self):
        """Return non-mutating repository/ref health for reconciliation UIs."""
        self.ensure_repository()
        main_commit = self._run(["rev-parse", "--verify", "refs/heads/main"], check=False)
        refs_output = self._run(
            ["for-each-ref", "refs/heads/oc/branch", "--format=%(refname:short)"],
            check=False,
        )
        refs = sorted({line.strip() for line in refs_output.splitlines() if line.strip()})
        branches = []
        known_refs = set()
        for branch in WorldBranch.objects.filter(workspace=self.workspace).exclude(name="main").order_by("name"):
            ref = branch.git_ref or f"oc/branch/{branch.pk.hex}"
            known_refs.add(ref)
            head = self._run(["rev-parse", "--verify", f"refs/heads/{ref}"], check=False)
            branches.append({
                "id": str(branch.id),
                "name": branch.name,
                "status": branch.status,
                "git_ref": ref,
                "ref_exists": bool(head),
                "head_commit": head or "",
                "base_commit": branch.base_commit,
                "base_exists": self._commit_exists(branch.base_commit),
                "base_matches_head": bool(head and branch.base_commit and head == branch.base_commit),
            })
        return {
            "repository": str(self.root),
            "main_ref_exists": bool(main_commit),
            "main_commit": main_commit or "",
            "branches": branches,
            "orphan_refs": [ref for ref in refs if ref not in known_refs],
        }

    def history(self, limit=30, ref=None):
        self.ensure_repository()
        args = ["log", f"-{max(1, min(int(limit), 200))}", "--pretty=format:%H%x09%ad%x09%s", "--date=iso"]
        if ref:
            args.append(ref)
        output = self._run(args, check=False)
        return [
            dict(zip(["hash", "date", "message"], line.split("\t", 2)))
            for line in output.splitlines()
            if line
        ]

    def _validated_commit(self, value, label):
        if value is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{40,64}", value):
            from rest_framework.exceptions import ValidationError
            raise ValidationError(f"{label} 必须是完整 commit hash")
        resolved = self._run(["rev-parse", "--verify", f"{value}^{{commit}}"], check=False)
        if not resolved:
            from rest_framework.exceptions import ValidationError
            raise ValidationError(f"{label} 不存在于当前仓库")
        return resolved

    def temporal_snapshot(self, commit=None, ref=None):
        """Read structured chronology indexes from one committed Git state."""
        self.ensure_repository()
        target = self._validated_commit(commit, "commit") if commit else (ref or "HEAD")
        resolved = self._run(["rev-parse", "--verify", f"{target}^{{commit}}"], check=False)
        if not resolved:
            return {"commit": "", "entities": [], "relations": [], "time_systems": [], "conversions": [], "timeline_entries": [], "character_lifespans": []}

        def load(name):
            raw = self._run(["show", f"{resolved}:indexes/{name}.json"], check=False)
            if not raw:
                return []
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return []
            return value if isinstance(value, list) else []

        return {
            "commit": resolved,
            "entities": load("entities"),
            "relations": load("relations"),
            "time_systems": load("time-systems"),
            "conversions": load("time-system-conversions"),
            "timeline_entries": load("timeline-entries"),
            "character_lifespans": load("character-lifespans"),
        }

    def temporal_diff(self, commit_a=None, commit_b=None, ref=None, timeline_id=None):
        """Return a deterministic, UI-friendly chronology diff.

        The diff is intentionally derived only from immutable Git indexes.  It
        never reads current PostgreSQL rows, so historical playback cannot be
        changed by a later edit.  Every collection has stable ordering and
        every record carries a semantic ``change_kind`` for the UI.
        """
        self.ensure_repository()
        head_ref = ref or "HEAD"
        head = self._run(["rev-parse", "--verify", f"{head_ref}^{{commit}}"], check=False)
        empty = {"commit": "", "entities": [], "relations": [], "time_systems": [], "conversions": [], "timeline_entries": [], "character_lifespans": []}
        if not head:
            return {"from": "", "to": "", "from_ref": {"kind": "empty", "value": "", "schema": "projectoc-temporal-v1"}, "to_ref": {"kind": "empty", "value": "", "schema": "projectoc-temporal-v1"}, "schema_compatible": True, "changes": {}, "addedEvents": [], "removedEvents": [], "changedEvents": [], "lifecycleChanges": {"added": [], "removed": [], "changed": []}, "participantChanges": [], "relationChanges": {"added": [], "removed": [], "changed": []}, "timeSystemChanges": {"time_systems": {"added": [], "removed": [], "changed": []}, "conversions": {"added": [], "removed": [], "changed": []}}, "warnings": ["没有可比较的 Git 提交"]}

        first = self._validated_commit(commit_a, "from")
        second = self._validated_commit(commit_b, "to")
        if first and second:
            from_ref, to_ref = first, second
        elif first:
            from_ref, to_ref = first, head
        elif second:
            parent = self._run(["rev-parse", "--verify", f"{second}^"], check=False)
            from_ref, to_ref = parent, second
        else:
            parent = self._run(["rev-parse", "--verify", f"{head_ref}^"], check=False)
            from_ref, to_ref = parent, head
        before = self.temporal_snapshot(from_ref) if from_ref else empty
        after = self.temporal_snapshot(to_ref)

        def keyed(values):
            return {str(row.get("id")): row for row in values if isinstance(row, dict) and row.get("id")}

        def title_for(row):
            if not isinstance(row, dict):
                return ""
            return str(row.get("event_title") or row.get("entry_title") or row.get("title") or row.get("character_title") or row.get("id") or row.get("entry_id") or "")

        def stable(row):
            if not isinstance(row, dict):
                return (0, "", "")
            candidate = row.get("start_value")
            if candidate is None:
                # Participant diffs carry arrays in before/after rather than
                # timeline-entry records. Keep their ordering deterministic
                # without assuming every diff payload is object-shaped.
                after_row = row.get("after") if isinstance(row.get("after"), dict) else {}
                before_row = row.get("before") if isinstance(row.get("before"), dict) else {}
                candidate = after_row.get("start_value", before_row.get("start_value", 0))
            try:
                candidate = int(candidate) if candidate is not None else 0
            except (TypeError, ValueError):
                candidate = 0
            return (candidate, title_for(row).casefold(), str(row.get("id") or row.get("entry_id") or ""))

        def annotate(row, kind):
            value = dict(row)
            value["change_kind"] = kind
            return value

        def compare(before_rows, after_rows, movement_fields=None):
            movement_fields = {"start_value", "end_value", "time_system", "sequence"} if movement_fields is None else set(movement_fields)
            bm, am = keyed(before_rows), keyed(after_rows)
            added = [annotate(am[item], "added") for item in am.keys() - bm.keys()]
            removed = [annotate(bm[item], "removed") for item in bm.keys() - am.keys()]
            changed = []
            for item in sorted(bm.keys() & am.keys()):
                before_row, after_row = bm[item], am[item]
                if before_row == after_row:
                    continue
                fields = sorted({*before_row.keys(), *after_row.keys()})
                field_changes = [{"field": field, "before": before_row.get(field), "after": after_row.get(field)} for field in fields if before_row.get(field) != after_row.get(field)]
                change = "moved" if {x["field"] for x in field_changes} & movement_fields else "modified"
                changed.append({"id": item, "before": before_row, "after": after_row, "changed_fields": field_changes, "change_kind": change})
            added.sort(key=stable); removed.sort(key=stable); changed.sort(key=stable)
            return {"added": added, "removed": removed, "changed": changed}

        changes = {
            key: compare(before[key], after[key], movement_fields=set()) if key == "character_lifespans" else compare(before[key], after[key])
            for key in ("time_systems", "conversions", "timeline_entries", "character_lifespans", "relations")
        }
        entries_added = changes["timeline_entries"]["added"]
        entries_removed = changes["timeline_entries"]["removed"]
        entries_changed = changes["timeline_entries"]["changed"]
        if timeline_id:
            tid = str(timeline_id)
            def belongs(row):
                return str(row.get("timeline")) == tid
            entries_added = [row for row in entries_added if belongs(row)]
            entries_removed = [row for row in entries_removed if belongs(row)]
            entries_changed = [row for row in entries_changed if belongs(row.get("after", {})) or belongs(row.get("before", {}))]

        participant_changes = []
        for row in entries_changed:
            before_participants = {json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in row["before"].get("participants", [])}
            after_participants = {json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in row["after"].get("participants", [])}
            if before_participants != after_participants:
                participant_changes.append({
                    "entry_id": row["id"], "entry_title": row["after"].get("event_title") or row["before"].get("event_title", ""),
                    "before": list(before_participants.values()), "after": list(after_participants.values()),
                    "added": [after_participants[key] for key in sorted(after_participants.keys() - before_participants.keys())],
                    "removed": [before_participants[key] for key in sorted(before_participants.keys() - after_participants.keys())],
                    "change_kind": "modified",
                })
        participant_changes.sort(key=stable)

        warnings = []
        if changes["time_systems"]["changed"] or changes["conversions"]["changed"]:
            warnings.append({"code": "time_system_changed", "severity": "warning", "message": "时间体系或换算规则发生变化，历史显示应以各自快照为准", "evidence": [{"source_type": "snapshot", "source_id": before.get("commit", ""), "source_revision": before.get("commit", "")}, {"source_type": "snapshot", "source_id": after.get("commit", ""), "source_revision": after.get("commit", "")} ]})
        if changes["relations"]["changed"] or changes["relations"]["added"] or changes["relations"]["removed"]:
            warnings.append({"code": "relation_changed", "severity": "info", "message": "关系变化可能影响人物关系切片，请重新运行一致性检查", "evidence": [{"source_type": "git_commit", "source_id": after.get("commit", ""), "source_revision": after.get("commit", "")} ]})

        def ref_snapshot(snapshot):
            return {"kind": "commit" if snapshot.get("commit") else "empty", "value": snapshot.get("commit", ""), "commit": snapshot.get("commit", ""), "schema": "projectoc-temporal-v1"}

        return {
            "from": before["commit"], "to": after["commit"], "from_ref": ref_snapshot(before), "to_ref": ref_snapshot(after),
            "schema_compatible": True, "schema_version": "projectoc-temporal-v1", "changes": changes,
            "addedEvents": entries_added, "removedEvents": entries_removed, "changedEvents": entries_changed,
            "lifecycleChanges": changes["character_lifespans"], "participantChanges": participant_changes,
            "relationChanges": changes["relations"], "timeSystemChanges": {"time_systems": changes["time_systems"], "conversions": changes["conversions"]},
            "warnings": warnings,
        }

    def diff(self, commit_a=None, commit_b=None, ref=None):
        """Compare two known commits; with no arguments show the latest commit on a ref."""
        self.ensure_repository()
        head_ref = ref or "HEAD"
        head = self._run(["rev-parse", "--verify", f"{head_ref}^{{commit}}"], check=False)
        if not head:
            return ""

        commit_a = self._validated_commit(commit_a, "from")
        commit_b = self._validated_commit(commit_b, "to")
        if commit_a and commit_b:
            revisions = [commit_a, commit_b]
        elif commit_a:
            revisions = [commit_a, head]
        elif commit_b:
            # Compare the requested commit with its first parent (or empty tree).
            parent = self._run(["rev-parse", "--verify", f"{commit_b}^"], check=False)
            if parent:
                revisions = [parent, commit_b]
            else:
                return self._run(["show", "--format=fuller", "--no-ext-diff", commit_b, "--"])
        else:
            parent = self._run(["rev-parse", "--verify", f"{head_ref}^"], check=False)
            if parent:
                revisions = [parent, head]
            else:
                return self._run(["show", "--format=fuller", "--no-ext-diff", head, "--"])

        return self._run(["diff", "--no-ext-diff", *revisions, "--"])
