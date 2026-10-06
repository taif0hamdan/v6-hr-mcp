"""
Unit/session reports: start from the eligible HR roster (matched against
the committee's unit scope) and LEFT JOIN local Evaluation/Result rows -
never the reverse. This guarantees an HR employee with no local account and
no results still appears in the report (as not_completed), and headcount is
never derived solely from saved result rows. Reading a report never creates
a local User account for anyone.

Historical semantics (explicit, per spec): this report reflects the
CURRENT cached HR roster plus whatever Evaluation/Result rows exist now -
i.e. "current roster plus past outcomes". It is NOT "membership as of a
past date" (an effective-dated roster as of a specific prior year) - that
would require historical roster snapshots this system doesn't keep, and is
out of scope here; if that semantic is ever needed it must be a distinct,
explicitly-labeled report, not a silent reinterpretation of this one.

Status categories (separated per spec - "not-completed from absent and
not-applicable"):
  - not_applicable: employee is in scope but fails is_eligible() (currently
    a no-op - see eligibility.py - so this category is presently unused)
  - completed: a Result exists with an outcome other than "absent"
  - absent: a Result exists with outcome == "absent" (an evaluator's
    explicit entry, not inferred from silence)
  - not_completed: everyone else in scope - evaluated-but-no-result yet, or
    not yet evaluated at all. The data model has no distinct "no-show
    before any entry" signal beyond an explicit "absent" outcome, so this
    is reported honestly as one bucket rather than inventing a split the
    data can't actually support.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from hr_backend.authorization import require_unit_authorization
from hr_backend.eligibility import is_eligible
from hr_backend.db import get_db
from hr_backend.membership import UnitScope, matches_unit_scope
from hr_backend.models import Committee, EvalSession, Evaluation, User
from hr_backend.normalize import get_cached_division_index, get_cached_roster
from hr_backend.ordering import sort_employees
from hr_backend.security import get_current_user

router = APIRouter(prefix="/reports", tags=["reports"])


class SessionReportRow(BaseModel):
    employee_id: str
    full_name_ar: str | None
    full_name: str | None
    rank_id: str | None
    status: str  # "completed" | "absent" | "not_completed" | "not_applicable"
    outcome: str | None


class SessionReport(BaseModel):
    session_id: int
    committee_id: int
    rows: list[SessionReportRow]


@router.get("/sessions/{session_id}", response_model=SessionReport)
def session_report(
    session_id: int, session: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> SessionReport:
    eval_session = session.get(EvalSession, session_id)
    if eval_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
    committee = session.get(Committee, eval_session.committee_id)
    if committee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="committee not found for this session")

    division_index = get_cached_division_index(session)
    require_unit_authorization(session, user, committee.organization_id, division_index)

    scope = UnitScope(
        organization_id=committee.organization_id,
        department_id=committee.department_id,
        section_id=committee.section_id,
        subsection_id=committee.subsection_id,
    )
    roster = [emp for emp in get_cached_roster(session) if matches_unit_scope(emp, scope)]
    ordered_roster = sort_employees(roster)

    evaluations_by_employee = {
        e.employee_id: e for e in session.query(Evaluation).filter_by(session_id=session_id).all()
    }

    rows: list[SessionReportRow] = []
    for emp in ordered_roster:
        evaluation = evaluations_by_employee.get(emp.employee_id)
        result = evaluation.result if evaluation else None

        if not is_eligible(emp):
            report_status, outcome = "not_applicable", None
        elif result is not None and (result.outcome or "").strip().lower() == "absent":
            report_status, outcome = "absent", result.outcome
        elif result is not None:
            report_status, outcome = "completed", result.outcome
        else:
            report_status, outcome = "not_completed", None

        rows.append(
            SessionReportRow(
                employee_id=emp.employee_id,
                full_name_ar=emp.full_name_ar,
                full_name=emp.full_name,
                rank_id=emp.rank_id,
                status=report_status,
                outcome=outcome,
            )
        )

    return SessionReport(session_id=session_id, committee_id=committee.id, rows=rows)
