"""
Match an employee's resolved organizational membership against a
committee/report's configured target unit(s).

Rule (per spec): "Match selected unit membership against all four IDs" -
for organization 1752, department 2427, section 2444, a person must match
EACH respective selection. A level left unset on the committee is a
wildcard at that level, not an automatic match-everything. The deepest
matching level may be used for display grouping only - it must never
silently become the person's authorization unit (that's authorization.py's
job, driven by UnitAdminGrant rows, not by committee membership).
"""

from __future__ import annotations

from dataclasses import dataclass

from hr_backend.normalize import NormalizedEmployee

_LEVELS = ("organization_id", "department_id", "section_id", "subsection_id")


@dataclass(frozen=True)
class UnitScope:
    organization_id: str | None = None
    department_id: str | None = None
    section_id: str | None = None
    subsection_id: str | None = None


def matches_unit_scope(emp: NormalizedEmployee, scope: UnitScope) -> bool:
    for level in _LEVELS:
        target = getattr(scope, level)
        if target is None:
            continue  # wildcard at this level
        if getattr(emp, level) != target:
            return False
    return True


def deepest_matching_level(emp: NormalizedEmployee, scope: UnitScope) -> str | None:
    """Display-only grouping hint - the finest-grained level both the
    employee and the scope specify and agree on. Never used for authorization."""
    deepest = None
    for level in _LEVELS:
        target = getattr(scope, level)
        if target is not None and getattr(emp, level) == target:
            deepest = level
    return deepest
