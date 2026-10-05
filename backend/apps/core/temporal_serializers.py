from rest_framework import serializers
from .models import CharacterLifespan, Entity, TimeSystem, TimeSystemConversion, TimelineEntry, TimelineParticipation


class TimeSystemSerializer(serializers.ModelSerializer):
    class Meta:
        model = TimeSystem
        fields = ["id", "workspace", "name", "epoch_label", "unit_name", "units", "calendar_rules", "display_format", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_units(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("units must be an ordered list")
        for unit in value:
            if not isinstance(unit, dict) or not isinstance(unit.get("name"), str) or not unit["name"].strip():
                raise serializers.ValidationError("each unit requires a non-empty name")
            if not isinstance(unit.get("ticks"), int) or unit["ticks"] <= 0:
                raise serializers.ValidationError("each unit requires a positive integer ticks value")
        return value


    def validate_calendar_rules(self, value):
        if value in (None, {}):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("calendar_rules must be an object")
        mode = value.get("mode", "fixed_units")
        if mode not in {"fixed_units", "variable_months"}:
            raise serializers.ValidationError("calendar_rules.mode must be fixed_units or variable_months")
        if mode == "fixed_units":
            return {**value, "mode": mode}
        months = value.get("months")
        if not isinstance(months, list) or not months:
            raise serializers.ValidationError("variable_months calendars require a non-empty months list")
        names = set()
        normalized_months = []
        for month in months:
            if not isinstance(month, dict) or not isinstance(month.get("name"), str) or not month["name"].strip():
                raise serializers.ValidationError("each calendar month requires a non-empty name")
            name = month["name"].strip()
            if name in names:
                raise serializers.ValidationError("calendar month names must be unique")
            names.add(name)
            days = month.get("days")
            if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
                raise serializers.ValidationError("each calendar month requires positive integer days")
            normalized_months.append({"name": name, "days": days})
        leap_rule = value.get("leap_rule") or {}
        if not isinstance(leap_rule, dict):
            raise serializers.ValidationError("leap_rule must be an object")
        if leap_rule:
            cycle = leap_rule.get("cycle")
            extra_days = leap_rule.get("extra_days", 0)
            if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle <= 0:
                raise serializers.ValidationError("leap_rule.cycle must be a positive integer")
            if isinstance(extra_days, bool) or not isinstance(extra_days, int) or extra_days < 0:
                raise serializers.ValidationError("leap_rule.extra_days must be a non-negative integer")
            leap_rule = {"cycle": cycle, "extra_days": extra_days}
        return {
            "mode": "variable_months",
            "months": normalized_months,
            "leap_rule": leap_rule,
            "year_zero": bool(value.get("year_zero", True)),
            "era": str(value.get("era", "")).strip(),
        }


class TimeSystemConversionSerializer(serializers.ModelSerializer):
    source_system_name = serializers.CharField(source="source_system.name", read_only=True)
    target_system_name = serializers.CharField(source="target_system.name", read_only=True)

    class Meta:
        model = TimeSystemConversion
        fields = [
            "id", "workspace", "source_system", "source_system_name",
            "target_system", "target_system_name", "numerator",
            "denominator", "offset", "label", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "source_system_name", "target_system_name"]
        validators = []

    def validate(self, attrs):
        attrs = super().validate(attrs)
        source = attrs.get("source_system", getattr(self.instance, "source_system", None))
        target = attrs.get("target_system", getattr(self.instance, "target_system", None))
        workspace = attrs.get("workspace", getattr(self.instance, "workspace", None))
        numerator = attrs.get("numerator", getattr(self.instance, "numerator", 1))
        denominator = attrs.get("denominator", getattr(self.instance, "denominator", 1))
        if source and target and source.pk == target.pk:
            raise serializers.ValidationError({"target_system": "source_system and target_system must differ"})
        for field, system in (("source_system", source), ("target_system", target)):
            if system and workspace and system.workspace_id != workspace.id:
                raise serializers.ValidationError({field: "must belong to the selected workspace"})
        if numerator is None or numerator <= 0:
            raise serializers.ValidationError({"numerator": "must be greater than zero"})
        if denominator is None or denominator <= 0:
            raise serializers.ValidationError({"denominator": "must be greater than zero"})
        if source and target and workspace and TimeSystemConversion.objects.filter(
            workspace=workspace, source_system=source, target_system=target,
        ).exclude(pk=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError({"target_system": "a conversion for this route already exists"})
        return attrs


class TimelineEntrySerializer(serializers.ModelSerializer):
    participants = serializers.ListField(child=serializers.DictField(), write_only=True, required=False)
    participant_details = serializers.SerializerMethodField()
    timeline_title = serializers.CharField(source="timeline.title", read_only=True)
    event_title = serializers.CharField(source="event.title", read_only=True)
    time_system_name = serializers.CharField(source="time_system.name", read_only=True)

    class Meta:
        model = TimelineEntry
        fields = ["id", "workspace", "branch", "timeline", "timeline_title", "event", "event_title", "time_system", "time_system_name", "start_value", "end_value", "sequence", "summary", "properties", "archived", "participants", "participant_details", "created_at", "updated_at"]
        read_only_fields = ["id", "branch", "archived", "participant_details", "created_at", "updated_at"]

    def get_participant_details(self, obj):
        return [{"id": str(row.entity_id), "title": row.entity.title, "type": row.entity.type, "role": row.role} for row in obj.participations.select_related("entity").order_by("entity__title")]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        timeline = attrs.get("timeline", getattr(self.instance, "timeline", None))
        event = attrs.get("event", getattr(self.instance, "event", None))
        axis = attrs.get("time_system", getattr(self.instance, "time_system", None))
        workspace = attrs.get("workspace", getattr(self.instance, "workspace", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        if timeline and timeline.type != Entity.EntityType.TIMELINE:
            raise serializers.ValidationError({"timeline": "timeline must reference a timeline entity"})
        if event and event.type != Entity.EntityType.EVENT:
            raise serializers.ValidationError({"event": "event must reference an event entity"})
        for field, obj in (("timeline", timeline), ("event", event), ("time_system", axis), ("branch", branch)):
            if obj and workspace and obj.workspace_id != workspace.id:
                raise serializers.ValidationError({field: "must belong to the selected workspace"})
        start = attrs.get("start_value", getattr(self.instance, "start_value", None))
        end = attrs.get("end_value", getattr(self.instance, "end_value", None))
        if start is not None and end is not None and end < start:
            raise serializers.ValidationError({"end_value": "must be greater than or equal to start_value"})
        participants = attrs.get("participants", [])
        if participants and workspace:
            ids = [item.get("entity") for item in participants]
            normalized_ids = list(map(str, ids))
            found = {str(pk) for pk in Entity.objects.filter(workspace=workspace, id__in=ids).values_list("id", flat=True)}
            if len(found) != len(set(normalized_ids)) or len(normalized_ids) != len(set(normalized_ids)) or any(not isinstance(item.get("role", ""), str) for item in participants):
                raise serializers.ValidationError({"participants": "participants must uniquely reference workspace entities and have string roles"})
        if not self.instance and all((timeline, event, axis, workspace)):
            duplicate = TimelineEntry.objects.filter(workspace=workspace, timeline=timeline, event=event, time_system=axis, branch=branch).exists()
            if duplicate:
                raise serializers.ValidationError({"event": "this event is already placed on this timeline and time system"})
        return attrs

    def create(self, validated_data):
        participants = validated_data.pop("participants", [])
        entry = super().create(validated_data)
        self._save_participants(entry, participants)
        return entry

    def update(self, instance, validated_data):
        participants = validated_data.pop("participants", None)
        entry = super().update(instance, validated_data)
        if participants is not None:
            self._save_participants(entry, participants)
        return entry

    @staticmethod
    def _save_participants(entry, participants):
        if participants is None:
            return
        TimelineParticipation.objects.filter(entry=entry).delete()
        TimelineParticipation.objects.bulk_create([
            TimelineParticipation(entry=entry, entity_id=item["entity"], role=item.get("role", ""))
            for item in participants
        ])


class CharacterLifespanSerializer(serializers.ModelSerializer):
    character_title = serializers.CharField(source="character.title", read_only=True)
    time_system_name = serializers.CharField(source="time_system.name", read_only=True)

    class Meta:
        model = CharacterLifespan
        fields = ["id", "workspace", "branch", "character", "character_title", "time_system", "time_system_name", "start_value", "end_value", "properties", "archived"]
        read_only_fields = ["id", "branch", "archived", "character_title", "time_system_name"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        character = attrs.get("character", getattr(self.instance, "character", None))
        axis = attrs.get("time_system", getattr(self.instance, "time_system", None))
        workspace = attrs.get("workspace", getattr(self.instance, "workspace", None))
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        if character and character.type != Entity.EntityType.CHARACTER:
            raise serializers.ValidationError({"character": "must reference a character entity"})
        for field, obj in (("character", character), ("time_system", axis), ("branch", branch)):
            if obj and workspace and obj.workspace_id != workspace.id:
                raise serializers.ValidationError({field: "must belong to the selected workspace"})
        start = attrs.get("start_value", getattr(self.instance, "start_value", None))
        end = attrs.get("end_value", getattr(self.instance, "end_value", None))
        if start is not None and end is not None and end < start:
            raise serializers.ValidationError({"end_value": "must be greater than or equal to start_value"})
        if not self.instance and all((character, axis, workspace)) and CharacterLifespan.objects.filter(workspace=workspace, character=character, time_system=axis, branch=branch).exists():
            raise serializers.ValidationError({"character": "a lifespan already exists for this character and time system"})
        return attrs
