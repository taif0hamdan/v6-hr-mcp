"""
Shared sort order for every employee-facing list: rank_id ascending, then
employee_id ascending, both numeric - "do not sort numeric IDs
lexicographically" (so "10" doesn't sort before "2"). Missing/non-numeric
values sort after all numeric ones. Performance-leaderboard ordering is a
separate, intentional score order and must NOT use this helper.
"""

from __future__ import annotations

import math
from typing import Iterable, TypeVar

from hr_backend.normalize import NormalizedEmployee

T = TypeVar("T")


def _numeric_sort_key(value: str | None) -> float:
    if value is None:
        return math.inf
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.inf  # non-numeric values sort after numeric ones, never lexicographically


def employee_sort_key(emp: NormalizedEmployee) -> tuple[float, float]:
    return (_numeric_sort_key(emp.rank_id), _numeric_sort_key(emp.employee_id))


def sort_employees(employees: Iterable[NormalizedEmployee]) -> list[NormalizedEmployee]:
    return sorted(employees, key=employee_sort_key)
