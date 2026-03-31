"""vCon builder for IETF sessions.

Constructs vCon objects from IETF session data including video, materials,
transcripts, and chat logs. Uses vcon-lib (the ``vcon`` package) for core
vCon construction, WTF transcription, and lawful basis extensions.
"""

import base64
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vcon import Vcon
from vcon.party import Party
from vcon.dialog import Dialog

from .models import (
    ChatMessage,
    IETFMaterial,
    IETFMeeting,
    IETFPerson,
    IETFSession,
)
from .transcription import TranscriptionResult
from .youtube import VideoMetadata
from .zulip_client import chat_messages_to_json, chat_messages_to_text

logger = logging.getLogger(__name__)


class VConBuilder:
    """Builder for creating vCon objects from IETF session data."""

    def __init__(self):
        self.vcon = Vcon.build_new()
        self._party_map: dict[str, int] = {}  # email/name -> party index

    def set_subject(self, subject: str) -> "VConBuilder":
        """Set the vCon subject."""
        self.vcon.vcon_dict["subject"] = subject
        return self

    def set_meeting_metadata(
        self, meeting: IETFMeeting, session: IETFSession
    ) -> "VConBuilder":
        """Set metadata from IETF meeting and session."""
        self.vcon.vcon_dict["subject"] = (
            f"IETF {meeting.number} - {session.group_acronym.upper()} "
            f"Working Group Session"
        )

        # Add meeting metadata as attachment
        self.vcon.add_attachment(
            purpose="meeting_metadata",
            encoding="json",
            body={
                "ietf_meeting_number": meeting.number,
                "location": f"{meeting.city}, {meeting.country}" if meeting.city else None,
                "working_group": session.group_acronym,
                "session_id": session.session_id,
                "room": session.room,
                "start_time": session.start_time.isoformat() if session.start_time else None,
                "duration_seconds": session.duration_seconds,
            },
        )

        return self

    def add_party(
        self,
        name: str,
        email: str | None = None,
        role: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Add a party to the vCon. Returns the party index."""
        # Check if party already exists
        key = email or name
        if key in self._party_map:
            return self._party_map[key]

        party = Party(
            name=name,
            mailto=email,
            role=role,
            meta=meta,
        )
        index = len(self.vcon.parties)
        self.vcon.add_party(party)
        self._party_map[key] = index
        return index

    def add_persons(self, persons: list[IETFPerson]) -> list[int]:
        """Add multiple persons as parties. Returns list of party indices."""
        indices = []
        for person in persons:
            idx = self.add_party(
                name=person.name,
                email=person.email,
                role=person.role,
                meta={"affiliation": person.affiliation} if person.affiliation else None,
            )
            indices.append(idx)
        return indices

    def add_attendees_party(self, count: int | None = None) -> int:
        """Add a generic 'attendees' party representing all participants."""
        return self.add_party(
            name="IETF Attendees",
            role="attendee",
            meta={"count": count} if count else None,
        )

    def add_video_dialog(
        self,
        video: VideoMetadata,
        session: IETFSession,
        party_indices: list[int] | None = None,
    ) -> int:
        """Add video recording as a dialog. Returns dialog index."""
        start_time = session.start_time or datetime.now(UTC)

        dialog = Dialog(
            type="video",
            start=start_time,
            duration=video.duration_seconds or session.duration_seconds,
            parties=party_indices or list(range(len(self.vcon.parties))),
            mediatype="video/mp4",
            url=video.url,
            meta={
                "source": "youtube",
                "video_id": video.video_id,
                "title": video.title,
                "ietf_meeting": session.meeting_number,
                "working_group": session.group_acronym,
            },
        )

        index = len(self.vcon.dialog)
        self.vcon.add_dialog(dialog)
        return index

    def add_video_dialog_from_url(
        self,
        url: str,
        session: IETFSession,
        mimetype: str = "video/mp4",
        party_indices: list[int] | None = None,
    ) -> int:
        """Add video dialog from a URL (YouTube or Meetecho)."""
        start_time = session.start_time or datetime.now(UTC)

        dialog = Dialog(
            type="video",
            start=start_time,
            duration=session.duration_seconds,
            parties=party_indices or list(range(len(self.vcon.parties))),
            mediatype=mimetype,
            url=url,
            meta={
                "ietf_meeting": session.meeting_number,
                "working_group": session.group_acronym,
            },
        )

        index = len(self.vcon.dialog)
        self.vcon.add_dialog(dialog)
        return index

    def add_video_dialog_inline(
        self,
        video_path: Path,
        session: IETFSession,
        party_indices: list[int] | None = None,
    ) -> int:
        """Add video as inline base64url-encoded content.

        Warning: This can make the vCon very large!
        """
        content = video_path.read_bytes()
        encoded = base64.urlsafe_b64encode(content).decode("ascii")

        start_time = session.start_time or datetime.now(UTC)

        dialog = Dialog(
            type="video",
            start=start_time,
            duration=session.duration_seconds,
            parties=party_indices or list(range(len(self.vcon.parties))),
            mediatype="video/mp4",
            filename=video_path.name,
            body=encoded,
            encoding="base64url",
        )

        index = len(self.vcon.dialog)
        self.vcon.add_dialog(dialog)
        return index

    def add_chat_dialog(
        self,
        messages: list[ChatMessage],
        session: IETFSession,
        as_text: bool = True,
    ) -> int:
        """Add chat log as a text dialog. Returns dialog index."""
        if not messages:
            return -1

        start_time = messages[0].timestamp if messages else (session.start_time or datetime.now(UTC))

        if as_text:
            body = chat_messages_to_text(messages)
            mimetype = "text/plain"
        else:
            body = chat_messages_to_json(messages)
            mimetype = "application/json"

        dialog = Dialog(
            type="text",
            start=start_time,
            parties=list(range(len(self.vcon.parties))),
            mediatype=mimetype,
            body=body if isinstance(body, str) else None,
            encoding="none",
            meta={
                "source": "zulip",
                "stream": messages[0].stream if messages else None,
                "message_count": len(messages),
                "ietf_meeting": session.meeting_number,
                "working_group": session.group_acronym,
            },
        )

        index = len(self.vcon.dialog)
        self.vcon.add_dialog(dialog)

        # If JSON format, set body as dict after creation
        if not as_text:
            self.vcon.vcon_dict["dialog"][index]["body"] = body

        return index

    def add_material_attachment(
        self,
        material: IETFMaterial,
        content: bytes | None = None,
        inline: bool = False,
    ) -> "VConBuilder":
        """Add a meeting material as an attachment."""
        if inline and content:
            encoded = base64.urlsafe_b64encode(content).decode("ascii")
            hash_value = hashlib.sha256(content).hexdigest()

            self.vcon.add_attachment(
                purpose=material.type,
                body=encoded,
                encoding="base64url",
                mediatype=material.mimetype,
                filename=material.filename,
                content_hash=hash_value,
            )
        else:
            self.vcon.add_attachment(
                purpose=material.type,
                body={
                    "url": material.url,
                    "mimetype": material.mimetype,
                    "filename": material.filename,
                    "title": material.title,
                },
                encoding="json",
            )

        return self

    def add_materials(
        self,
        materials: list[IETFMaterial],
        inline: bool = False,
        downloader=None,
    ) -> "VConBuilder":
        """Add multiple materials as attachments."""
        for material in materials:
            content = None
            if inline and downloader:
                content = downloader.get_material_content(material)
            self.add_material_attachment(material, content=content, inline=inline)
        return self

    def add_transcript(
        self,
        transcript: TranscriptionResult,
        dialog_index: int = 0,
    ) -> "VConBuilder":
        """Add a transcript as a WTF transcription attachment using vcon-lib."""
        # Calculate average confidence
        confidences = [seg.confidence for seg in transcript.segments if seg.confidence is not None]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.95

        segments = [
            {
                "id": seg.id if seg.id is not None else i,
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text,
                "confidence": round(seg.confidence, 4) if seg.confidence is not None else 0.95,
                **({"speaker": seg.speaker} if seg.speaker is not None else {}),
            }
            for i, seg in enumerate(transcript.segments)
        ]

        self.vcon.add_wtf_transcription_attachment(
            transcript={
                "text": transcript.text,
                "language": transcript.language or "en",
                "duration": transcript.duration or 0.0,
                "confidence": avg_confidence,
            },
            segments=segments,
            metadata={
                "created_at": datetime.now(UTC).isoformat(),
                "processed_at": datetime.now(UTC).isoformat(),
                "provider": transcript.provider,
                "model": transcript.model or "unknown",
            },
            dialog_index=dialog_index,
        )
        return self

    def add_analysis(
        self,
        analysis_type: str,
        body: str | dict[str, Any],
        dialog_index: int | None = None,
        vendor: str | None = None,
    ) -> "VConBuilder":
        """Add generic analysis data."""
        self.vcon.add_analysis(
            type=analysis_type,
            dialog=dialog_index if dialog_index is not None else 0,
            vendor=vendor or "unknown",
            body=body,
            encoding="json" if isinstance(body, dict) else "none",
        )
        return self

    def add_ingress_info(
        self,
        source: str = "ietf2vcon",
        **kwargs,
    ) -> "VConBuilder":
        """Add ingress (source) information attachment."""
        self.vcon.add_attachment(
            purpose="ingress_info",
            encoding="json",
            body={
                "source": source,
                "converter_version": "0.1.0",
                "converted_at": datetime.now(UTC).isoformat(),
                **kwargs,
            },
        )
        return self

    def add_lawful_basis(
        self,
        lawful_basis: str,
        purpose_grants: list[dict] | None = None,
        terms_of_service: str | None = None,
        terms_of_service_name: str | None = None,
        jurisdiction: str | None = None,
        controller: str | None = None,
        expiration: datetime | None = None,
        notes: str | None = None,
    ) -> "VConBuilder":
        """Add a lawful basis attachment using vcon-lib's lawful basis extension."""
        grants = []
        for grant in (purpose_grants or []):
            grants.append({
                "purpose": grant.get("purpose", ""),
                "granted": grant.get("status", "granted") == "granted",
                "granted_at": datetime.now(UTC).isoformat(),
            })

        exp = expiration.isoformat() + "Z" if expiration else "2099-12-31T23:59:59Z"

        # Extra fields go into metadata dict per vcon-lib API
        meta = {}
        if terms_of_service_name:
            meta["terms_of_service_name"] = terms_of_service_name
        if jurisdiction:
            meta["jurisdiction"] = jurisdiction
        if controller:
            meta["controller"] = controller
        if notes:
            meta["notes"] = notes

        self.vcon.add_lawful_basis_attachment(
            lawful_basis=lawful_basis,
            expiration=exp,
            purpose_grants=grants,
            terms_of_service=terms_of_service,
            metadata=meta if meta else None,
        )
        return self

    def add_ietf_note_well(self, session_start: datetime | None = None) -> "VConBuilder":
        """Add IETF Note Well as a lawful basis attachment.

        The IETF Note Well establishes the legal basis for recording and
        processing IETF meeting sessions.
        """
        return self.add_lawful_basis(
            lawful_basis="legitimate_interests",
            purpose_grants=[
                {"purpose": "recording", "status": "granted"},
                {"purpose": "transcription", "status": "granted"},
                {"purpose": "publication", "status": "granted"},
                {"purpose": "archival", "status": "granted"},
                {"purpose": "analysis", "status": "granted"},
            ],
            terms_of_service="https://www.ietf.org/about/note-well/",
            terms_of_service_name="IETF Note Well",
            jurisdiction="IETF",
            controller="Internet Engineering Task Force (IETF)",
            notes=(
                "Participation in IETF meetings constitutes agreement to the Note Well, "
                "which permits recording, transcription, and public archival of proceedings. "
                "See also BCP 78 (RFC 5378) and BCP 79 (RFC 8179) for IPR policies."
            ),
        )

    def build(self) -> Vcon:
        """Build and return the final vCon."""
        self.vcon.vcon_dict["updated_at"] = datetime.now(UTC).isoformat()
        return self.vcon

    def to_json(self, indent: int = 2) -> str:
        """Build and serialize to JSON."""
        self.build()
        return self.vcon.to_json()

    def to_dict(self) -> dict[str, Any]:
        """Build and serialize to dictionary."""
        self.build()
        return self.vcon.to_dict()
