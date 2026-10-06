"""
ORM models for the HR backend's own (app-owned) database.

Field-ownership rule enforced throughout this schema (see security.py's
sync_profile_fields): HR owns name/rank/org fields on User, the app owns
role/permissions, LDAP may own username/email. No migration here ever
deletes a Committee/EvalSession/Evaluation/Result row - history is
preserved; HR refreshes only touch the HR-owned columns.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """An application login account. Linked to HR via employee_id (MIL_ID), not by name."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # LDAP/app-owned identity. Stored lowercase; lookups are always
    # case-insensitive against this column (see ldap_auth.find_local_account).
    ldap_username: Mapped[str | None] = mapped_column(String(256), unique=True, nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    # HR join key = MIL_ID (per decision - see plan). Nullable until linked;
    # unique once set so two local accounts can never claim the same
    # employee (ambiguous mappings are rejected before they reach here).
    employee_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)

    # HR-owned fields, synced by security.sync_profile_fields - never
    # hand-edited elsewhere, never overwritten with blanks during an outage.
    full_name_ar: Mapped[str | None] = mapped_column(String(256), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    rank_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    organization_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # App-owned.
    role: Mapped[str] = mapped_column(String(64), default="viewer", server_default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class UnitAdminGrant(Base):
    """Authorization grant: a user may administer one division, exactly or including its subtree."""

    __tablename__ = "unit_admin_grants"
    __table_args__ = (UniqueConstraint("user_id", "divn_id", name="uq_unit_admin_grant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    divn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # "exact" | "subtree"
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    granted_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class Committee(Base):
    __tablename__ = "committees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name_ar: Mapped[str] = mapped_column(String(256), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Target unit for membership matching - any level left null is a
    # wildcard at that level (see membership.matches_committee_scope).
    organization_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    section_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subsection_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    memberships: Mapped[list["CommitteeMembership"]] = relationship(back_populates="committee")
    sessions: Mapped[list["EvalSession"]] = relationship(back_populates="committee")


class CommitteeMembership(Base):
    __tablename__ = "committee_memberships"
    __table_args__ = (UniqueConstraint("committee_id", "employee_id", name="uq_committee_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    committee_id: Mapped[int] = mapped_column(ForeignKey("committees.id"), nullable=False)
    # Deliberately the HR employee_id (MIL_ID) string, not a local user FK -
    # a person can be a committee member without ever logging in locally.
    employee_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role_in_committee: Mapped[str | None] = mapped_column(String(64), nullable=True)
    added_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    committee: Mapped["Committee"] = relationship(back_populates="memberships")


class EvalSession(Base):
    __tablename__ = "eval_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    committee_id: Mapped[int] = mapped_column(ForeignKey("committees.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    scheduled_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", server_default="scheduled")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    committee: Mapped["Committee"] = relationship(back_populates="sessions")
    evaluations: Mapped[list["Evaluation"]] = relationship(back_populates="session")


class Evaluation(Base):
    """One evaluator's entry for one employee within one session. Historical - never rewritten by an HR refresh."""

    __tablename__ = "evaluations"
    __table_args__ = (UniqueConstraint("session_id", "employee_id", name="uq_evaluation_subject"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("eval_sessions.id"), nullable=False)
    employee_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    # Snapshot of the employee's HR-derived attributes AT THE TIME of
    # evaluation - deliberately denormalized so a later HR profile change
    # (rename, rank change, re-org) never alters what a past evaluation
    # record is understood to say about who/what was evaluated.
    employee_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    score: Mapped[float | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    session: Mapped["EvalSession"] = relationship(back_populates="evaluations")
    result: Mapped["Result | None"] = relationship(back_populates="evaluation", uselist=False)


class Result(Base):
    """Finalized outcome derived from an Evaluation. Preserved permanently once recorded."""

    __tablename__ = "results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"), unique=True, nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "pass" | "fail" | provider-defined
    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    evaluation: Mapped["Evaluation"] = relationship(back_populates="result")


class UnitVisibilityOverride(Base):
    """Local-only visibility flag for a division node. Never deletes data, never grants/revokes access."""

    __tablename__ = "unit_visibility_overrides"

    divn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    set_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    set_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class UnitClassificationOverride(Base):
    """Explicit classification for a division node (e.g. "regular"/"pro"). Resolver walks ancestors when absent."""

    __tablename__ = "unit_classification_overrides"

    divn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    set_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    set_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class HrCacheSnapshot(Base):
    """
    Durable cache row. `generation` guards against an in-flight stale
    refresh repopulating data an admin just invalidated; a failed refresh
    never overwrites a prior good `payload` (see hr_cache.py).
    """

    __tablename__ = "hr_cache_snapshots"

    cache_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    mapping_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ok", server_default="ok")  # "ok" | "error"
    generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BackgroundJobLease(Base):
    """Shared lease row so only one worker runs a given background job at a time."""

    __tablename__ = "background_job_leases"

    job_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    leased_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class Setting(Base):
    """Small private key/value store - e.g. the global cache-invalidation generation counter."""

    __tablename__ = "hr_backend_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(String(1024), nullable=True)
