"""
Authorization: exact-unit vs subtree grants, expanded against the FULL
(unfiltered) division tree - never the visibility-scoped one (visibility
hides things from choice lists/reports; it must never silently affect who
can read or write what). A global "admin" role has full access by design -
a separate, deliberate concept from scoped unit-admin grants; granting a
unit admin a subtree must never be treated as organization-wide access.
"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from hr_backend.models import User, UnitAdminGrant
from hr_backend.normalize import NormalizedDivision


def expand_subtree(root_id: str, division_index: dict[str, NormalizedDivision]) -> set[str]:
    """All ids reachable from root_id via parent_id pointers, root included.
    Cycle-safe (visited set) so a malformed tree can't infinite-loop."""
    children: dict[str, list[str]] = {}
    for node in division_index.values():
        if node.parent_id:
            children.setdefault(node.parent_id, []).append(node.id)

    result: set[str] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(children.get(current, []))
    return result


def get_authorized_divn_ids(
    session: Session,
    user: User,
    division_index: dict[str, NormalizedDivision],
) -> set[str] | Literal["all"]:
    if user.role == "admin":
        return "all"

    grants = session.query(UnitAdminGrant).filter(UnitAdminGrant.user_id == user.id).all()
    authorized: set[str] = set()
    for grant in grants:
        if grant.scope == "subtree":
            authorized |= expand_subtree(grant.divn_id, division_index)
        else:  # "exact"
            authorized.add(grant.divn_id)
    return authorized


def is_authorized_for_unit(authorized: set[str] | Literal["all"], divn_id: str | None) -> bool:
    if authorized == "all":
        return True
    return bool(divn_id) and divn_id in authorized


def filter_authorized_ids(authorized: set[str] | Literal["all"], divn_ids: list[str]) -> list[str]:
    if authorized == "all":
        return list(divn_ids)
    return [d for d in divn_ids if d in authorized]


def require_unit_authorization(
    session: Session,
    user: User,
    divn_id: str | None,
    division_index: dict[str, NormalizedDivision],
) -> None:
    """Raises 403 unless the user is authorized (exact or subtree grant, or
    the global admin role) for the given unit. A None divn_id (a committee
    with no unit scope configured) is only writable by a global admin -
    an unscoped committee is never implicitly org-wide for a unit admin."""
    authorized = get_authorized_divn_ids(session, user, division_index)
    if divn_id is None:
        if authorized != "all":
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not authorized for this unit")
        return
    if not is_authorized_for_unit(authorized, divn_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="not authorized for this unit")
