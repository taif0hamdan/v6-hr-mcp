"""
Hidden-unit overrides: local-only display preferences layered on top of the
HR-derived division tree. Visibility NEVER deletes data and NEVER grants or
revokes access - authorization.py always resolves against the full,
unfiltered tree. This module only decides what appears in choice lists and
reports; hiding a node consistently hides its descendants there too.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from hr_backend.authorization import expand_subtree
from hr_backend.models import UnitVisibilityOverride
from hr_backend.normalize import NormalizedDivision


def get_hidden_divn_ids(session: Session) -> set[str]:
    rows = session.query(UnitVisibilityOverride).filter(UnitVisibilityOverride.hidden.is_(True)).all()
    return {row.divn_id for row in rows}


def compute_visible_tree(
    division_index: dict[str, NormalizedDivision],
    hidden_ids: set[str],
) -> dict[str, NormalizedDivision]:
    """Full tree minus every explicitly-hidden node AND its descendants."""
    excluded: set[str] = set()
    for hidden_id in hidden_ids:
        excluded |= expand_subtree(hidden_id, division_index)
    return {id_: node for id_, node in division_index.items() if id_ not in excluded}


def set_visibility(session: Session, divn_id: str, hidden: bool, *, set_by: int | None) -> None:
    row = session.get(UnitVisibilityOverride, divn_id)
    if row is None:
        row = UnitVisibilityOverride(divn_id=divn_id)
        session.add(row)
    row.hidden = hidden
    row.set_by = set_by
