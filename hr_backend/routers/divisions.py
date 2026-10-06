"""
GET /divisions - the visibility-scoped tree for normal users; admins (role
"admin") get the raw full tree. Backend-enforced: an unauthorized/hidden
unit is never merely hidden in the UI, it is never returned here at all for
a non-admin caller outside their authorized scope... except visibility
itself is a DISPLAY concept, not an authorization one (see visibility.py's
docstring) - so this endpoint applies visibility to everyone, and
separately would apply authorization scoping on any endpoint that also
requires write access (committees/sessions/etc., not plain reading of the
org chart, which the spec treats as directory access rather than a write
surface).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from hr_backend.classification import resolve_classification
from hr_backend.db import get_db
from hr_backend.models import User
from hr_backend.normalize import get_cached_division_index, is_excluded_division_type
from hr_backend.security import get_current_user
from hr_backend.visibility import compute_visible_tree, get_hidden_divn_ids

router = APIRouter(prefix="/divisions", tags=["divisions"])


class DivisionOut(BaseModel):
    id: str
    parent_id: str | None
    name_ar: str | None
    name_en: str | None
    divn_type: str | None
    classification: str
    classification_label_ar: str


@router.get("", response_model=list[DivisionOut])
def list_divisions(
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    include_hidden: bool = Query(False, description="Admins only - include hidden units and OTHERS/MILITARY nodes."),
) -> list[DivisionOut]:
    full_index = get_cached_division_index(session)

    if include_hidden and user.role == "admin":
        nodes = full_index
    else:
        hidden_ids = get_hidden_divn_ids(session)
        nodes = compute_visible_tree(full_index, hidden_ids)
        # OTHERS/MILITARY-typed nodes are excluded from the selection tree
        # by type (spec rule), independent of the hidden-unit overrides above.
        nodes = {id_: n for id_, n in nodes.items() if not is_excluded_division_type(n.divn_type)}

    out = []
    for node in nodes.values():
        resolved = resolve_classification(session, node.id, full_index)
        out.append(
            DivisionOut(
                id=node.id,
                parent_id=node.parent_id,
                name_ar=node.name_ar,
                name_en=node.name_en,
                divn_type=node.divn_type,
                classification=resolved.value,
                classification_label_ar=resolved.label_ar,
            )
        )
    return out
