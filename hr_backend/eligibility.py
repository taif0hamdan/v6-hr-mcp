"""
Active/eligible-employee filter.

VERIFIED GAP (do not "fix" this by guessing a field): as of this build,
querying the live Oracle instance behind V4 - oracleapi directly showed
that EMPLOYEE_STATUS_CODE, TERMINATION_DATE, EMP_TYPE and CONTRACT_TYPE_CODE
are NULL for all 50 employees, USER_CATG is a job category (MANAGEMENT /
STAFF) rather than a status, and no column anywhere in the HR schema
contains "GROUP" (i.e. no EMPLOYEESTATUSGROUPID equivalent exists). There is
currently no real active/inactive signal in this data.

Decision (explicit, not silent): treat every employee the provider returns
as eligible until a real status field is identified - possibly only once a
production HR instance (rather than this seeded test schema) is connected.

config.HrApiSettings.status_field / active_status_values are wired through
so that plugging in a real field later is a one-line change here, not a
rewrite of every caller (search, lists, committee membership, reports all
call is_eligible(), never check status directly).
"""

from __future__ import annotations

from hr_backend.config import HrApiSettings, get_settings
from hr_backend.normalize import NormalizedEmployee


def is_eligible(emp: NormalizedEmployee, raw: dict | None = None, *, settings: HrApiSettings | None = None) -> bool:
    settings = settings or get_settings().hr_api

    if not settings.status_field:
        # No status field configured/available in this environment (the
        # current, verified state) - missing/unknown status is normally
        # "not active" per spec, but here EVERY record is missing it, so
        # the documented fallback is "treat as active" rather than
        # excluding the entire roster.
        return True

    if raw is None:
        return True  # can't evaluate the field without the raw row; fail open per the same documented fallback

    value = raw.get(settings.status_field)
    if value is None:
        return False  # once a real field exists: missing/unknown status IS "not active"

    normalized = str(value).strip()
    return normalized in settings.active_status_values
