"""Data models for IETF sessions and vCon format."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl


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


# --- vCon Data Models ---


class PartyRole(str, Enum):
    """Roles for vCon parties."""

    CHAIR = "chair"
    PRESENTER = "presenter"
    SPEAKER = "speaker"
    ATTENDEE = "attendee"
    SCRIBE = "scribe"


class VConParty(BaseModel):
    """A party (participant) in a vCon."""

    name: str | None = None
    mailto: str | None = None
    tel: str | None = None
    role: str | None = None
    meta: dict[str, Any] | None = None


class DialogType(str, Enum):
    """Types of dialog in a vCon."""

    RECORDING = "recording"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    TRANSFER = "transfer"
    INCOMPLETE = "incomplete"


class VConDialog(BaseModel):
    """A dialog entry in a vCon (recording, text, etc.)."""

    type: DialogType
    start: datetime
    parties: list[int] = Field(default_factory=list)
    duration: int | None = None
    mimetype: str | None = None
    filename: str | None = None
    body: str | None = None
    encoding: str | None = None
    url: str | None = None
    alg: str | None = None
    signature: str | None = None
    meta: dict[str, Any] | None = None


class VConAttachment(BaseModel):
    """An attachment in a vCon.

    Per vCon spec, attachments must have type, body, and encoding.
    Additional metadata goes in the meta field.
    """

    type: str
    body: str | dict[str, Any]  # Required per spec
    encoding: str = "none"  # Required per spec: "none", "base64", or "base64url"
    party: int | None = None
    dialog: int | None = None
    start: datetime | None = None
    meta: dict[str, Any] | None = None


class TranscriptSegment(BaseModel):
    """A segment of a transcript."""

    id: int | None = None
    start: float
    end: float
    text: str
    speaker: int | None = None
    confidence: float | None = None


class VConAnalysis(BaseModel):
    """Analysis data in a vCon (transcript, summary, etc.)."""

    type: str  # transcript, summary, sentiment, etc.
    dialog: int | None = None
    vendor: str | None = None
    spec: str | None = None  # Draft/RFC reference for experimental types
    body: str | dict[str, Any] | None = None
    encoding: str | None = None
    vendor_schema: dict[str, Any] | None = None


class VCon(BaseModel):
    """The main vCon container."""

    vcon: str = "0.0.1"
    uuid: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    subject: str | None = None
    parties: list[VConParty] = Field(default_factory=list)
    dialog: list[VConDialog] = Field(default_factory=list)
    attachments: list[VConAttachment] = Field(default_factory=list)
    analysis: list[VConAnalysis] = Field(default_factory=list)
    meta: dict[str, Any] | None = None

    def to_json(self, indent: int = 2) -> str:
        """Serialize to JSON string."""
        return self.model_dump_json(indent=indent, exclude_none=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return self.model_dump(exclude_none=True, mode="json")


# --- Chat Log Models ---


class ChatMessage(BaseModel):
    """A chat message from Zulip or Meetecho."""

    timestamp: datetime
    sender: str
    content: str
    sender_email: str | None = None
    topic: str | None = None
    stream: str | None = None


# --- Lawful Basis Models (draft-howe-vcon-lawful-basis) ---


class LawfulBasisType(str, Enum):
    """GDPR Article 6 lawful bases for data processing."""

    CONSENT = "consent"
    CONTRACT = "contract"
    LEGAL_OBLIGATION = "legal_obligation"
    VITAL_INTERESTS = "vital_interests"
    PUBLIC_TASK = "public_task"
    LEGITIMATE_INTERESTS = "legitimate_interests"


class PurposeGrant(BaseModel):
    """A specific purpose permission within the lawful basis."""

    purpose: str  # e.g., "recording", "transcription", "analysis", "publication"
    status: str = "granted"  # "granted" or "denied"
    timestamp: datetime | None = None


class LawfulBasisAttachment(BaseModel):
    """Lawful basis attachment for vCon (draft-howe-vcon-lawful-basis).

    Documents the legal foundation for processing conversation data,
    supporting GDPR compliance and other privacy frameworks.
    """

    lawful_basis: LawfulBasisType
    expiration: datetime | None = None
    purpose_grants: list[PurposeGrant] = Field(default_factory=list)
    terms_of_service: str | None = None  # URI to terms document
    terms_of_service_name: str | None = None  # Human-readable name
    jurisdiction: str | None = None  # e.g., "IETF", "EU", "US"
    controller: str | None = None  # Data controller organization
    notes: str | None = None  # Additional context
