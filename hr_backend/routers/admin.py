"""
Permission-gated admin actions: HR cache refresh (clears ONLY HR cache
snapshot rows, never business records, and generation-guards against an
in-flight stale refresh repopulating what was just invalidated - see
hr_cache.admin_clear_cache), hidden-unit overrides, classification
overrides. All require role == "admin" (the global platform-admin role,
distinct from scoped unit-admin grants).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from hr_backend.background import refresh_roster_once
from hr_backend.classification import clear_explicit_classification, set_explicit_classification
from hr_backend.db import get_db
from hr_backend.hr_cache import admin_clear_cache
from hr_backend.models import User
from hr_backend.security import require_role
from hr_backend.visibility import set_visibility

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/cache/refresh")
def refresh_cache(session: Session = Depends(get_db), user: User = Depends(require_role("admin"))) -> dict:
    cleared = admin_clear_cache(session)
    session.commit()
    # Trigger an immediate refresh (bounded by its own 120s budget) rather
    # than making the admin wait for the next 60s background poll.
    ok = refresh_roster_once()
    return {"cleared_entries": cleared, "refresh_triggered": True, "refresh_succeeded": ok}


class VisibilityRequest(BaseModel):
    hidden: bool


@router.put("/units/{divn_id}/visibility")
def set_unit_visibility(
    divn_id: str,
    body: VisibilityRequest,
    session: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
) -> dict:
    set_visibility(session, divn_id, body.hidden, set_by=user.id)
    session.commit()
    return {"divn_id": divn_id, "hidden": body.hidden}


class ClassificationRequest(BaseModel):
    classification: str


@router.put("/units/{divn_id}/classification")
def set_unit_classification(
    divn_id: str,
    body: ClassificationRequest,
    session: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
) -> dict:
    set_explicit_classification(session, divn_id, body.classification, set_by=user.id)
    session.commit()
    return {"divn_id": divn_id, "classification": body.classification, "source": "explicit"}


@router.delete("/units/{divn_id}/classification")
def clear_unit_classification(
    divn_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
) -> dict:
    cleared = clear_explicit_classification(session, divn_id)
    session.commit()
    return {"divn_id": divn_id, "cleared": cleared}
