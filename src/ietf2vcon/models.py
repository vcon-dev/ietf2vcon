"""Data models for IETF sessions.

IETF-specific models for meeting metadata, sessions, materials, and persons.
Core vCon models (Vcon, Party, Dialog, Analysis, etc.) are provided by the
vcon library (vcon-lib).
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


# --- IETF Data Models ---


# A meeting is identified either by number (124) or, for an interim, by the
# Datatracker's meeting name (interim-2026-vcon-02). Both are what the API
# takes in the `number` field, so both belong in this type.
MeetingNumber = int | str


class IETFMeeting(BaseModel):
    """IETF meeting metadata."""

    number: MeetingNumber
    city: str | None = None
    country: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    time_zone: str | None = None


class IETFSession(BaseModel):
    """IETF working group session."""

    meeting_number: MeetingNumber
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
    # A mutable HTML page (draft page, session page, shared notes) rather than
    # a published file. Its bytes change, so it is referenced from the body
    # instead of being promoted to url + content_hash, which would assert an
    # integrity guarantee that does not hold.
    landing_page: bool = False
    # The file the IETF publishes, as the Datatracker names it
    # (`slides-116-teas-...-05.pdf`). The document record is authoritative:
    # guessing at the extension picks the wrong sibling wherever a deck exists
    # as both .pdf and .pptx.
    uploaded_filename: str | None = None
    # Where those exact bytes are served. `url` is the Datatracker page, which
    # is a display endpoint -- it renders Markdown as HTML and converts
    # PowerPoint to PDF -- so it cannot be what a content_hash describes.
    file_url: str | None = None


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
