"""vCon builder for IETF sessions.

Constructs vCon objects from IETF session data including video, materials,
transcripts, and chat logs.
"""

import base64
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    ChatMessage,
    DialogType,
    IETFMaterial,
    IETFMeeting,
    IETFPerson,
    IETFSession,
    TranscriptSegment,
    VCon,
    VConAnalysis,
    VConAttachment,
    VConDialog,
    VConParty,
)
from .transcription import TranscriptionResult, transcription_to_vcon_analysis
from .youtube import VideoMetadata
from .zulip_client import chat_messages_to_json, chat_messages_to_text

logger = logging.getLogger(__name__)


class VConBuilder:
    """Builder for creating vCon objects from IETF session data."""

    def __init__(self):
        self.vcon = VCon(
            uuid=uuid4(),
            created_at=datetime.utcnow(),
        )
        self._party_map: dict[str, int] = {}  # email/name -> party index

    def set_subject(self, subject: str) -> "VConBuilder":
        """Set the vCon subject."""
        self.vcon.subject = subject
        return self

    def set_meeting_metadata(
        self, meeting: IETFMeeting, session: IETFSession
    ) -> "VConBuilder":
        """Set metadata from IETF meeting and session."""
        self.vcon.subject = (
            f"IETF {meeting.number} - {session.group_acronym.upper()} "
            f"Working Group Session"
        )

        # Add meeting metadata as attachment
        self.vcon.attachments.append(
            VConAttachment(
                type="meeting_metadata",
                encoding="none",
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
        )

        return self

    def add_party(
        self,
        name: str,
        email: str | None = None,
        role: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Add a party to the vCon.

        Returns the party index.
        """
        # Check if party already exists
        key = email or name
        if key in self._party_map:
            return self._party_map[key]

        party = VConParty(
            name=name,
            mailto=email,
            role=role,
            meta=meta,
        )
        index = len(self.vcon.parties)
        self.vcon.parties.append(party)
        self._party_map[key] = index
        return index

    def add_persons(self, persons: list[IETFPerson]) -> list[int]:
        """Add multiple persons as parties.

        Returns list of party indices.
        """
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
        """Add video recording as a dialog.

        Args:
            video: Video metadata from YouTube
            session: IETF session info
            party_indices: Indices of parties in the video

        Returns:
            Index of the added dialog
        """
        start_time = session.start_time or datetime.utcnow()

        dialog = VConDialog(
            type=DialogType.VIDEO,
            start=start_time,
            duration=video.duration_seconds or session.duration_seconds,
            parties=party_indices or list(range(len(self.vcon.parties))),
            mimetype="video/mp4",
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
        self.vcon.dialog.append(dialog)
        return index

    def add_video_dialog_from_url(
        self,
        url: str,
        session: IETFSession,
        mimetype: str = "video/mp4",
        party_indices: list[int] | None = None,
    ) -> int:
        """Add video dialog from a URL (YouTube or Meetecho)."""
        start_time = session.start_time or datetime.utcnow()

        dialog = VConDialog(
            type=DialogType.VIDEO,
            start=start_time,
            duration=session.duration_seconds,
            parties=party_indices or list(range(len(self.vcon.parties))),
            mimetype=mimetype,
            url=url,
            meta={
                "ietf_meeting": session.meeting_number,
                "working_group": session.group_acronym,
            },
        )

        index = len(self.vcon.dialog)
        self.vcon.dialog.append(dialog)
        return index

    def add_video_dialog_inline(
        self,
        video_path: Path,
        session: IETFSession,
        party_indices: list[int] | None = None,
    ) -> int:
        """Add video as inline base64-encoded content.

        Warning: This can make the vCon very large!
        """
        content = video_path.read_bytes()
        encoded = base64.b64encode(content).decode("ascii")

        # Compute hash for integrity
        hash_value = hashlib.sha256(content).hexdigest()

        start_time = session.start_time or datetime.utcnow()

        dialog = VConDialog(
            type=DialogType.VIDEO,
            start=start_time,
            duration=session.duration_seconds,
            parties=party_indices or list(range(len(self.vcon.parties))),
            mimetype="video/mp4",
            filename=video_path.name,
            body=encoded,
            encoding="base64",
            alg="SHA-256",
            signature=hash_value,
        )

        index = len(self.vcon.dialog)
        self.vcon.dialog.append(dialog)
        return index

    def add_chat_dialog(
        self,
        messages: list[ChatMessage],
        session: IETFSession,
        as_text: bool = True,
    ) -> int:
        """Add chat log as a text dialog.

        Args:
            messages: Chat messages
            session: IETF session
            as_text: If True, store as plain text. If False, store as JSON.

        Returns:
            Index of the added dialog
        """
        if not messages:
            return -1

        start_time = messages[0].timestamp if messages else (session.start_time or datetime.utcnow())

        if as_text:
            body = chat_messages_to_text(messages)
            mimetype = "text/plain"
        else:
            body = chat_messages_to_json(messages)
            mimetype = "application/json"

        dialog = VConDialog(
            type=DialogType.TEXT,
            start=start_time,
            parties=list(range(len(self.vcon.parties))),  # All parties
            mimetype=mimetype,
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
        self.vcon.dialog.append(dialog)

        # If JSON format, store in body as dict
        if not as_text:
            self.vcon.dialog[index].body = body

        return index

    def add_material_attachment(
        self,
        material: IETFMaterial,
        content: bytes | None = None,
        inline: bool = False,
    ) -> "VConBuilder":
        """Add a meeting material as an attachment.

        Args:
            material: Material metadata
            content: Optional content bytes (for inline storage)
            inline: If True and content provided, embed content
        """
        attachment = VConAttachment(
            type=material.type,
            mimetype=material.mimetype,
            filename=material.filename,
            meta={
                "title": material.title,
                "order": material.order,
            },
        )

        if inline and content:
            encoded = base64.b64encode(content).decode("ascii")
            attachment.body = encoded
            attachment.encoding = "base64"

            # Add hash for integrity
            hash_value = hashlib.sha256(content).hexdigest()
            attachment.meta["sha256"] = hash_value
        else:
            attachment.url = material.url

        self.vcon.attachments.append(attachment)
        return self

    def add_materials(
        self,
        materials: list[IETFMaterial],
        inline: bool = False,
        downloader=None,
    ) -> "VConBuilder":
        """Add multiple materials as attachments.

        Args:
            materials: List of materials
            inline: If True, download and embed content
            downloader: MaterialsDownloader instance for inline mode
        """
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
        """Add a transcript as analysis."""
        analysis = transcription_to_vcon_analysis(transcript, dialog_index)
        self.vcon.analysis.append(analysis)
        return self

    def add_analysis(
        self,
        analysis_type: str,
        body: str | dict[str, Any],
        dialog_index: int | None = None,
        vendor: str | None = None,
    ) -> "VConBuilder":
        """Add generic analysis data."""
        analysis = VConAnalysis(
            type=analysis_type,
            dialog=dialog_index,
            vendor=vendor,
            body=body,
            encoding="none",
        )
        self.vcon.analysis.append(analysis)
        return self

    def add_ingress_info(
        self,
        source: str = "ietf2vcon",
        **kwargs,
    ) -> "VConBuilder":
        """Add ingress (source) information attachment."""
        self.vcon.attachments.append(
            VConAttachment(
                type="ingress_info",
                encoding="none",
                body={
                    "source": source,
                    "converter_version": "0.1.0",
                    "converted_at": datetime.utcnow().isoformat(),
                    **kwargs,
                },
            )
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
        """Add a lawful basis attachment (draft-howe-vcon-lawful-basis).

        Documents the legal foundation for processing conversation data.

        Args:
            lawful_basis: GDPR Article 6 basis (consent, contract, legal_obligation,
                         vital_interests, public_task, legitimate_interests)
            purpose_grants: List of purpose permissions (recording, transcription, etc.)
            terms_of_service: URI to the terms document
            terms_of_service_name: Human-readable name for the terms
            jurisdiction: Legal jurisdiction (e.g., "IETF", "EU")
            controller: Data controller organization
            expiration: When the lawful basis expires
            notes: Additional context
        """
        body = {
            "lawful_basis": lawful_basis,
        }

        if purpose_grants:
            body["purpose_grants"] = purpose_grants
        if terms_of_service:
            body["terms_of_service"] = terms_of_service
        if terms_of_service_name:
            body["terms_of_service_name"] = terms_of_service_name
        if jurisdiction:
            body["jurisdiction"] = jurisdiction
        if controller:
            body["controller"] = controller
        if expiration:
            body["expiration"] = expiration.isoformat()
        if notes:
            body["notes"] = notes

        self.vcon.attachments.append(
            VConAttachment(
                type="lawful_basis",
                encoding="none",
                body=body,
                meta={
                    "spec": "draft-howe-vcon-lawful-basis-00",
                },
            )
        )
        return self

    def add_ietf_note_well(self, session_start: datetime | None = None) -> "VConBuilder":
        """Add IETF Note Well as a lawful basis attachment.

        The IETF Note Well establishes the legal basis for recording and
        processing IETF meeting sessions. By participating in an IETF meeting,
        attendees agree to:
        - IPR disclosure requirements (BCP 78, BCP 79)
        - Recording and publication of proceedings
        - Public archival of materials

        This constitutes both "legitimate interests" (IETF's interest in
        documenting standards development) and "public task" (standards
        development serves the public interest) under GDPR Article 6.
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

    def build(self) -> VCon:
        """Build and return the final vCon."""
        self.vcon.updated_at = datetime.utcnow()
        return self.vcon

    def to_json(self, indent: int = 2) -> str:
        """Build and serialize to JSON."""
        return self.build().to_json(indent=indent)

    def to_dict(self) -> dict[str, Any]:
        """Build and serialize to dictionary."""
        return self.build().to_dict()
