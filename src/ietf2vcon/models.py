"""Data models for IETF sessions.

IETF-specific models for meeting metadata, sessions, materials, and persons.
Core vCon models (Vcon, Party, Dialog, Analysis, etc.) are provided by the
vcon library (vcon-lib).
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


# --- IETF Data Models ---


class IETFMeeting(BaseModel):
    """IETF meeting metadata."""

    number: int
    city: str | None = None
    country: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    time_zone: str | None = None


class IETFSession(BaseModel):
    """IETF working group session."""

    meeting_number: int
    group_acronym: str
    session_id: str
    name: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_seconds: int | None = None
    room: str | None = None
    agenda_url: str | None = None
    minutes_url: str | None = None
    video_url: str | None = None
    audio_url: str | None = None
    recording_url: str | None = None


class IETFMaterial(BaseModel):
    """IETF meeting material (slides, agenda, minutes, etc.)."""

    type: str  # slides, agenda, minutes, draft, etc.
    title: str
    url: str
    filename: str | None = None
    mimetype: str | None = None
    order: int | None = None


class IETFPerson(BaseModel):
    """Person involved in IETF session."""

    name: str
    email: str | None = None
    affiliation: str | None = None
    role: str | None = None  # chair, presenter, author, etc.


# --- Chat Log Models ---


class ChatMessage(BaseModel):
    """A chat message from Zulip or Meetecho."""

    timestamp: datetime
    sender: str
    content: str
    sender_email: str | None = None
    topic: str | None = None
    stream: str | None = None
