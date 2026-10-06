"""
POST /auth/login - LDAP-authenticates, safely links/provisions the local
account, best-effort syncs the HR-owned profile fields (never blocking
login on an HR outage), issues the session cookie.
POST /auth/logout, GET /auth/me.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from hr_backend.config import get_settings
from hr_backend.db import get_db
from hr_backend.ldap_auth import LdapIdentity, LdapOutcome, authenticate
from hr_backend.models import User
from hr_backend.normalize import find_cached_employee
from hr_backend.security import (
    SESSION_COOKIE_NAME,
    AmbiguousAccountMapping,
    create_session_token,
    find_or_link_account,
    get_current_user,
    sync_profile_fields,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    ldap_username: str | None
    employee_id: str | None
    email: str | None
    full_name_ar: str | None
    full_name: str | None
    rank_id: str | None
    organization_id: str | None
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


@router.post("/login", response_model=UserOut)
def login(body: LoginRequest, response: Response, session: Session = Depends(get_db)) -> User:
    settings = get_settings()
    result = authenticate(body.username, body.password, settings.ldap)

    if result.outcome == LdapOutcome.NOT_CONFIGURED:
        if not settings.security.dev_login_bypass:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="LDAP is not configured and the development login bypass is disabled.",
            )
        # Dev-only path: config.py already refuses this combination when
        # HR_BACKEND_ENV=production, so reaching here means it's explicitly
        # a non-production, explicitly-opted-in developer environment.
        identity = LdapIdentity(username=body.username, employee_id=None, email=None)
    elif result.outcome == LdapOutcome.UNAVAILABLE:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="authentication service unavailable")
    elif result.outcome == LdapOutcome.NOT_FOUND:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    elif result.outcome == LdapOutcome.INVALID_CREDENTIALS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    else:
        identity = result.identity

    try:
        user = find_or_link_account(session, identity)
    except AmbiguousAccountMapping as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="account deactivated")

    # Best-effort HR enrichment from the cached roster - never blocks login,
    # never blanks a known profile if the roster cache is cold/stale (see
    # sync_profile_fields: hr_employee=None leaves existing fields untouched).
    hr_employee = find_cached_employee(session, user.employee_id)
    sync_profile_fields(user, identity=identity, hr_employee=hr_employee)
    session.commit()

    token = create_session_token(user.id)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.security.environment == "production",
        max_age=settings.security.session_max_age_seconds,
    )
    return user


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"status": "logged_out"}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user
