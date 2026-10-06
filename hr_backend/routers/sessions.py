"""
Evaluation session CRUD. Scoped to a committee; authorization is checked
against the committee's organization_id (same resolver as committees.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from hr_backend.authorization import require_unit_authorization
from hr_backend.db import get_db
from hr_backend.models import Committee, EvalSession, User
from hr_backend.normalize import get_cached_division_index
from hr_backend.schemas import SessionCreate, SessionOut, SessionStatusUpdate
from hr_backend.security import get_current_user

router = APIRouter(prefix="/sessions", tags=["sessions"])

VALID_STATUSES = {"scheduled", "in_progress", "completed", "cancelled"}


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def create_session_(
    body: SessionCreate, session: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> EvalSession:
    committee = session.get(Committee, body.committee_id)
    if committee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="committee not found")

    division_index = get_cached_division_index(session)
    require_unit_authorization(session, user, committee.organization_id, division_index)

    eval_session = EvalSession(
        committee_id=body.committee_id, name=body.name, scheduled_at=body.scheduled_at, location=body.location
    )
    session.add(eval_session)
    session.commit()
    return eval_session


@router.get("", response_model=list[SessionOut])
def list_sessions(session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[EvalSession]:
    return session.query(EvalSession).all()


@router.get("/{session_id}", response_model=SessionOut)
def get_session_(
    session_id: int, session: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> EvalSession:
    eval_session = session.get(EvalSession, session_id)
    if eval_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
    return eval_session


@router.patch("/{session_id}/status", response_model=SessionOut)
def update_session_status(
    session_id: int,
    body: SessionStatusUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EvalSession:
    eval_session = session.get(EvalSession, session_id)
    if eval_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
    if body.status not in VALID_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"status must be one of {sorted(VALID_STATUSES)}")

    committee = session.get(Committee, eval_session.committee_id)
    division_index = get_cached_division_index(session)
    require_unit_authorization(session, user, committee.organization_id, division_index)

    eval_session.status = body.status
    session.commit()
    return eval_session
