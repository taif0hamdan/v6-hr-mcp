"""
Session issuance, current-user dependency, safe LDAP-account linking, and
the single profile-sync function that enforces field ownership everywhere
(login, import, admin sync all call sync_profile_fields - never hand-edit
HR-owned fields elsewhere):
    HR owns:   full_name_ar, full_name, rank_id, organization_id
    App owns:  role, is_active
    LDAP owns: ldap_username, email (HR never supplies these here anyway)
"""

from __future__ import annotations

import datetime
import logging

from fastapi import Cookie, Depends, HTTPException, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hr_backend.config import SecuritySettings, get_settings
from hr_backend.db import get_db
from hr_backend.ldap_auth import LdapIdentity
from hr_backend.models import User
from hr_backend.normalize import NormalizedEmployee

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "hr_session"


class AmbiguousAccountMapping(Exception):
    """Raised when an LDAP identity's username and employee_id each resolve to a different, existing account."""


def _serializer(settings: SecuritySettings | None = None) -> URLSafeTimedSerializer:
    settings = settings or get_settings().security
    return URLSafeTimedSerializer(settings.session_secret, salt="hr-backend-session")


def create_session_token(user_id: int) -> str:
    return _serializer().dumps({"user_id": user_id})


def read_session_token(token: str) -> int | None:
    settings = get_settings().security
    try:
        data = _serializer(settings).loads(token, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("user_id")


def find_or_link_account(session: Session, identity: LdapIdentity) -> User:
    """Find-or-create per the spec's safe-merge rules: match by
    case-insensitive login and by unique employee_id; attach rather than
    duplicate; reject ambiguous mappings; never merge on name."""
    by_username = None
    if identity.username:
        by_username = (
            session.query(User)
            .filter(User.ldap_username.isnot(None))
            .filter(User.ldap_username == identity.username.strip().lower())
            .one_or_none()
        )
    by_employee = None
    if identity.employee_id:
        by_employee = session.query(User).filter(User.employee_id == identity.employee_id).one_or_none()

    if by_username and by_employee and by_username.id != by_employee.id:
        raise AmbiguousAccountMapping(
            f"LDAP login '{identity.username}' and employee_id '{identity.employee_id}' "
            f"resolve to two different existing accounts (ids {by_username.id} and {by_employee.id})."
        )

    account = by_username or by_employee
    if account is not None:
        # The account that matched already carries the OTHER identifier set
        # to a conflicting value (e.g. this username is on file for a
        # different employee_id than LDAP just presented) - this is the
        # same ambiguous-mapping case as above, just reached via a single
        # matching lookup instead of two different ones. Never silently
        # keep the old value or silently relink.
        if identity.employee_id and account.employee_id and account.employee_id != identity.employee_id:
            raise AmbiguousAccountMapping(
                f"Account {account.id} already has employee_id '{account.employee_id}' on file, "
                f"but LDAP now presents employee_id '{identity.employee_id}' for login "
                f"'{identity.username}'."
            )
        if (
            identity.username
            and account.ldap_username
            and account.ldap_username != identity.username.strip().lower()
        ):
            raise AmbiguousAccountMapping(
                f"Account {account.id} already has ldap_username '{account.ldap_username}' on file, "
                f"but LDAP now presents a different login for employee_id '{identity.employee_id}'."
            )

        # Attach whichever identifier was missing, preserving local id/roles/history.
        if identity.employee_id and not account.employee_id:
            account.employee_id = identity.employee_id
        if identity.username and not account.ldap_username:
            account.ldap_username = identity.username.strip().lower()
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise AmbiguousAccountMapping(
                "Attaching this LDAP identity would violate a uniqueness constraint - "
                "it is already claimed by another account."
            ) from exc
        return account

    settings = get_settings().security
    if not settings.auto_provision_users:
        raise LookupError("No local account exists for this LDAP identity and auto-provisioning is disabled.")

    new_user = User(
        ldap_username=identity.username.strip().lower() if identity.username else None,
        employee_id=identity.employee_id,
        email=identity.email,
        role="viewer",
        is_active=True,
    )
    session.add(new_user)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise AmbiguousAccountMapping(
            "A race with another request claimed this identity first."
        ) from exc
    return new_user


def sync_profile_fields(user: User, *, identity: LdapIdentity | None, hr_employee: NormalizedEmployee | None) -> None:
    """HR owns name/rank/org fields - only overwritten when hr_employee is
    available (an HR outage must never blank a known profile). LDAP may own
    email when HR doesn't supply one (it never does, for this provider) -
    existing LDAP/app-owned email is preserved either way. Role/is_active
    (app-owned) are never touched here."""
    if hr_employee is not None:
        user.full_name_ar = hr_employee.full_name_ar or user.full_name_ar
        user.full_name = hr_employee.full_name or user.full_name
        user.rank_id = hr_employee.rank_id or user.rank_id
        user.organization_id = hr_employee.organization_id or user.organization_id
    # else: HR unavailable this cycle - leave HR-owned fields exactly as they were.

    if identity and identity.email and not user.email:
        user.email = identity.email
    user.last_login_at = datetime.datetime.utcnow()


def get_current_user(
    session: Session = Depends(get_db),
    hr_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> User:
    if not hr_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    user_id = read_session_token(hr_session)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="session invalid or expired")
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="session invalid or expired")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="account deactivated")
    return user


def require_role(*allowed_roles: str):
    def _dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="insufficient permissions")
        return user

    return _dependency
