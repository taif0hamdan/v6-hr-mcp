"""Pydantic request/response models for the domain CRUD routers (committees,
sessions, evaluations, results). Kept separate from models.py (ORM) so the
wire format can evolve independently of the storage schema."""

from __future__ import annotations

import datetime

from pydantic import BaseModel


class CommitteeCreate(BaseModel):
    name_ar: str
    name_en: str | None = None
    organization_id: str | None = None
    department_id: str | None = None
    section_id: str | None = None
    subsection_id: str | None = None


class CommitteeOut(CommitteeCreate):
    id: int
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class CommitteeMemberOut(BaseModel):
    employee_id: str
    full_name_ar: str | None
    full_name: str | None
    rank_id: str | None
    role_in_committee: str | None
    eligible: bool
    added_at: datetime.datetime


class AddMemberRequest(BaseModel):
    employee_id: str
    role_in_committee: str | None = None


class SessionCreate(BaseModel):
    committee_id: int
    name: str
    scheduled_at: datetime.datetime | None = None
    location: str | None = None


class SessionOut(BaseModel):
    id: int
    committee_id: int
    name: str
    scheduled_at: datetime.datetime | None
    location: str | None
    status: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class SessionStatusUpdate(BaseModel):
    status: str


class EvaluationCreate(BaseModel):
    employee_id: str
    score: float | None = None
    notes: str | None = None


class EvaluationOut(BaseModel):
    id: int
    session_id: int
    employee_id: str
    evaluator_user_id: int | None
    score: float | None
    notes: str | None
    employee_snapshot: dict | None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = {"from_attributes": True}


class ResultCreate(BaseModel):
    outcome: str


class ResultOut(BaseModel):
    id: int
    evaluation_id: int
    outcome: str
    recorded_at: datetime.datetime
    recorded_by: int | None

    model_config = {"from_attributes": True}
