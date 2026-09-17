"""Request/response models for the API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PatientIn(BaseModel):
    chart_number: str = Field(min_length=1, max_length=40)
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    date_of_birth: str | None = None


class SessionIn(BaseModel):
    patient_id: str
    operatory: str = Field(min_length=1, max_length=40)
    note: str | None = None


class AssignIn(BaseModel):
    """Assign a quarantined image. Either target works; session wins if both."""

    session_id: str | None = None
    patient_id: str | None = None
