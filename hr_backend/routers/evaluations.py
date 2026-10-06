"""
Evaluation entry. Write-eligibility is checked SEPARATELY from display
eligibility (the spec's rule: "Check inactive-person restrictions on the
backend for new assignments/results, not just selection menus.") - even
though is_eligible() is currently a documented no-op, the check happens
here at the write path specifically, not only when building a selection
list, so wiring in a real status field later immediately enforces it on
writes without any router change.

The employee being evaluated must match the committee's unit scope (same
rule as committee membership). A snapshot of the employee's HR attributes
at evaluation time is stored on the row so a later HR profile change never
retroactively alters what a past evaluation is understood to describe.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from hr_backend.authorization import require_unit_authorization
from hr_backend.db import get_db
from hr_backend.eligibility import is_eligible
from hr_backend.membership import UnitScope, matches_unit_scope
from hr_backend.models import Committee, EvalSession, Evaluation, User
from hr_backend.normalize import find_cached_employee, get_cached_division_index
from hr_backend.schemas import EvaluationCreate, EvaluationOut
from hr_backend.security import get_current_user

router = APIRouter(prefix="/sessions", tags=["evaluations"])


def _committee_for_session(session: Session, eval_session: EvalSession) -> Committee:
    committee = session.get(Committee, eval_session.committee_id)
    if committee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="committee not found for this session")
    return committee


@router.post("/{session_id}/evaluations", response_model=EvaluationOut, status_code=status.HTTP_201_CREATED)
def create_evaluation(
    session_id: int,
    body: EvaluationCreate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Evaluation:
    eval_session = session.get(EvalSession, session_id)
    if eval_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")

    committee = _committee_for_session(session, eval_session)
    division_index = get_cached_division_index(session)
    require_unit_authorization(session, user, committee.organization_id, division_index)

    employee = find_cached_employee(session, body.employee_id)
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="employee not found in the HR roster")

    scope = UnitScope(
        organization_id=committee.organization_id,
        department_id=committee.department_id,
        section_id=committee.section_id,
        subsection_id=committee.subsection_id,
    )
    if not matches_unit_scope(employee, scope):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="employee's organizational membership does not match this committee's unit scope",
        )

    # Write-eligibility check - separate call site from any display list.
    if not is_eligible(employee):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="employee is not eligible for a new evaluation")

    existing = (
        session.query(Evaluation).filter_by(session_id=session_id, employee_id=body.employee_id).one_or_none()
    )
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="an evaluation for this employee already exists in this session")

    evaluation = Evaluation(
        session_id=session_id,
        employee_id=body.employee_id,
        evaluator_user_id=user.id,
        employee_snapshot=asdict(employee),
        score=body.score,
        notes=body.notes,
    )
    session.add(evaluation)
    session.commit()
    return evaluation


@router.get("/{session_id}/evaluations", response_model=list[EvaluationOut])
def list_evaluations(
    session_id: int, session: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[Evaluation]:
    eval_session = session.get(EvalSession, session_id)
    if eval_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
    return session.query(Evaluation).filter_by(session_id=session_id).all()
