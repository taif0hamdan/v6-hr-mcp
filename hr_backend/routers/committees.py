"""
Committee CRUD + membership. Creating/editing a committee, or adding a
member, requires authorization over the committee's organization_id (exact
or subtree grant, or the global admin role) - evaluated against the
AUTHORITATIVE (full, unfiltered) division tree, never the visibility-scoped
one. Listing committees/members is read-only and available to any
authenticated user (directory-style access, same as /divisions).

Membership additions are matched against the committee's configured unit
scope (membership.matches_unit_scope) using the employee's HR-derived
membership from the cached roster - an employee outside the committee's
scope is rejected, not silently added.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from hr_backend.authorization import require_unit_authorization
from hr_backend.db import get_db
from hr_backend.eligibility import is_eligible
from hr_backend.membership import UnitScope, matches_unit_scope
from hr_backend.models import Committee, CommitteeMembership, User
from hr_backend.normalize import find_cached_employee, get_cached_division_index
from hr_backend.ordering import sort_employees
from hr_backend.schemas import AddMemberRequest, CommitteeCreate, CommitteeMemberOut, CommitteeOut
from hr_backend.security import get_current_user

router = APIRouter(prefix="/committees", tags=["committees"])


def _scope_from_committee(committee: Committee) -> UnitScope:
    return UnitScope(
        organization_id=committee.organization_id,
        department_id=committee.department_id,
        section_id=committee.section_id,
        subsection_id=committee.subsection_id,
    )


@router.post("", response_model=CommitteeOut, status_code=status.HTTP_201_CREATED)
def create_committee(
    body: CommitteeCreate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Committee:
    division_index = get_cached_division_index(session)
    require_unit_authorization(session, user, body.organization_id, division_index)

    committee = Committee(**body.model_dump())
    session.add(committee)
    session.commit()
    return committee


@router.get("", response_model=list[CommitteeOut])
def list_committees(session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[Committee]:
    return session.query(Committee).all()


@router.get("/{committee_id}", response_model=CommitteeOut)
def get_committee(
    committee_id: int, session: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Committee:
    committee = session.get(Committee, committee_id)
    if committee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="committee not found")
    return committee


@router.get("/{committee_id}/members", response_model=list[CommitteeMemberOut])
def list_committee_members(
    committee_id: int, session: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[CommitteeMemberOut]:
    committee = session.get(Committee, committee_id)
    if committee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="committee not found")

    memberships = {m.employee_id: m for m in committee.memberships}
    employees = [
        find_cached_employee(session, employee_id) or _placeholder(employee_id)
        for employee_id in memberships
    ]
    employees = [e for e in employees if e is not None]
    ordered = sort_employees(employees)

    return [
        CommitteeMemberOut(
            employee_id=emp.employee_id,
            full_name_ar=emp.full_name_ar,
            full_name=emp.full_name,
            rank_id=emp.rank_id,
            role_in_committee=memberships[emp.employee_id].role_in_committee,
            eligible=is_eligible(emp),
            added_at=memberships[emp.employee_id].added_at,
        )
        for emp in ordered
    ]


def _placeholder(employee_id: str):
    """A committee member not currently found in the cached roster (cold
    cache, or the employee no longer appears in /employees) still needs to
    render in the member list - with blank enrichment, not a crash or a
    silently dropped row."""
    from hr_backend.normalize import NormalizedEmployee

    return NormalizedEmployee(
        employee_id=employee_id, pf_no=None, full_name_ar=None, full_name=None,
        rank_id=None, divn_id=None, unit_id=None,
    )


@router.post("/{committee_id}/members", status_code=status.HTTP_201_CREATED)
def add_committee_member(
    committee_id: int,
    body: AddMemberRequest,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    committee = session.get(Committee, committee_id)
    if committee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="committee not found")

    division_index = get_cached_division_index(session)
    require_unit_authorization(session, user, committee.organization_id, division_index)

    scope = _scope_from_committee(committee)
    employee = find_cached_employee(session, body.employee_id)
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="employee not found in the HR roster")
    if not matches_unit_scope(employee, scope):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="employee's organizational membership does not match this committee's unit scope",
        )

    existing = (
        session.query(CommitteeMembership)
        .filter_by(committee_id=committee_id, employee_id=body.employee_id)
        .one_or_none()
    )
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="employee is already a member of this committee")

    session.add(
        CommitteeMembership(
            committee_id=committee_id, employee_id=body.employee_id, role_in_committee=body.role_in_committee
        )
    )
    session.commit()
    return {"committee_id": committee_id, "employee_id": body.employee_id}


@router.delete("/{committee_id}/members/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_committee_member(
    committee_id: int,
    employee_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    committee = session.get(Committee, committee_id)
    if committee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="committee not found")

    division_index = get_cached_division_index(session)
    require_unit_authorization(session, user, committee.organization_id, division_index)

    membership = (
        session.query(CommitteeMembership).filter_by(committee_id=committee_id, employee_id=employee_id).one_or_none()
    )
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="membership not found")
    session.delete(membership)
    session.commit()
