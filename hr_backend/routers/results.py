"""
Result CRUD - intentionally create+read only, no update endpoint. Once a
Result is recorded it is historical and permanent ("do not rewrite old
results just to refresh HR attributes") - a correction, if ever needed, is
a distinct business decision out of scope here, not a silent overwrite.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from hr_backend.authorization import require_unit_authorization
from hr_backend.db import get_db
from hr_backend.models import Committee, EvalSession, Evaluation, Result, User
from hr_backend.normalize import get_cached_division_index
from hr_backend.schemas import ResultCreate, ResultOut
from hr_backend.security import get_current_user

router = APIRouter(prefix="/evaluations", tags=["results"])


@router.post("/{evaluation_id}/results", response_model=ResultOut, status_code=status.HTTP_201_CREATED)
def create_result(
    evaluation_id: int,
    body: ResultCreate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Result:
    evaluation = session.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="evaluation not found")

    eval_session = session.get(EvalSession, evaluation.session_id)
    committee = session.get(Committee, eval_session.committee_id)
    division_index = get_cached_division_index(session)
    require_unit_authorization(session, user, committee.organization_id, division_index)

    if evaluation.result is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="a result already exists for this evaluation and cannot be overwritten")

    result = Result(evaluation_id=evaluation_id, outcome=body.outcome, recorded_by=user.id)
    session.add(result)
    session.commit()
    return result


@router.get("/{evaluation_id}/results", response_model=ResultOut)
def get_result(
    evaluation_id: int, session: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Result:
    evaluation = session.get(Evaluation, evaluation_id)
    if evaluation is None or evaluation.result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="result not found")
    return evaluation.result
