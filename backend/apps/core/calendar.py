"""Deterministic helpers for custom calendar rules.

Normalized chronology values stay integer ticks.  These helpers deliberately
contain no AI or locale-dependent parsing so the same world state formats the
same way in the API, Git snapshots, and clients.
"""

from __future__ import annotations

from typing import Any


def normalize_calendar_rules(value: Any) -> dict:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("calendar_rules must be an object")
    mode = value.get("mode", "fixed_units")
    if mode not in {"fixed_units", "variable_months"}:
        raise ValueError("calendar_rules.mode must be fixed_units or variable_months")
    if mode == "fixed_units":
        return {**value, "mode": mode}
    months = value.get("months")
    if not isinstance(months, list) or not months:
        raise ValueError("variable_months calendars require a non-empty months list")
    seen = set()
    normalized_months = []
    for month in months:
        if not isinstance(month, dict) or not isinstance(month.get("name"), str) or not month["name"].strip():
            raise ValueError("each calendar month requires a non-empty name")
        name = month["name"].strip()
        if name in seen:
            raise ValueError("calendar month names must be unique")
        seen.add(name)
        days = month.get("days")
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ValueError("each calendar month requires positive integer days")
        normalized_months.append({"name": name, "days": days})
    leap = value.get("leap_rule") or {}
    if not isinstance(leap, dict):
        raise ValueError("leap_rule must be an object")
    normalized_leap = {}
    if leap:
        cycle, extra = leap.get("cycle"), leap.get("extra_days", 0)
        if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle <= 0:
            raise ValueError("leap_rule.cycle must be a positive integer")
        if isinstance(extra, bool) or not isinstance(extra, int) or extra < 0:
            raise ValueError("leap_rule.extra_days must be a non-negative integer")
        normalized_leap = {"cycle": cycle, "extra_days": extra}
    return {
        "mode": "variable_months",
        "months": normalized_months,
        "leap_rule": normalized_leap,
        "year_zero": bool(value.get("year_zero", True)),
        "era": str(value.get("era", "")).strip(),
    }


def _year_length(year: int, rules: dict) -> int:
    length = sum(month["days"] for month in rules["months"])
    leap = rules.get("leap_rule") or {}
    cycle = leap.get("cycle")
    if cycle and year > 0 and year % cycle == 0:
        length += leap.get("extra_days", 0)
    return length


def format_calendar_value(value: int, rules: dict, unit_name: str = "tick") -> str:
    """Format a normalized base-unit value with variable month lengths.

    Year zero is supported by default.  When ``year_zero`` is false, the
    display jumps from 1 BCE to 1 CE at the epoch; the underlying tick value
    remains continuous and unmodified.
    """
    normalized = normalize_calendar_rules(rules)
    if normalized.get("mode") != "variable_months":
        return str(value)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("calendar values must be integers")
    months = normalized["months"]
    year_zero = normalized.get("year_zero", True)
    remaining = abs(value)
    direction = -1 if value < 0 else 1
    year = 0
    if direction < 0:
        while remaining:
            previous = year - 1
            length = _year_length(previous, normalized)
            if remaining < length:
                year = previous
                break
            remaining -= length
            year = previous
    else:
        while True:
            length = _year_length(year, normalized)
            if remaining < length:
                break
            remaining -= length
            year += 1
    month_index = 0
    while month_index < len(months) and remaining >= months[month_index]["days"]:
        remaining -= months[month_index]["days"]
        month_index += 1
    if month_index == len(months):
        # Extra leap days are displayed as a named day after the final month.
        month_name, day = "闰日", remaining + 1
    else:
        month_name, day = months[month_index]["name"], remaining + 1
    display_year = year
    era = normalized.get("era", "")
    if not year_zero and year <= 0:
        display_year = abs(year) + 1
        era = f"{era}前" if era else "前"
    prefix = f"{era} " if era else ""
    return f"{prefix}{'-' if direction < 0 else ''}{display_year}年 {month_name}{day}{unit_name}"
