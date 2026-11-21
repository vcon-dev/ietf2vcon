"""vCon builder for IETF sessions.

Constructs vCon objects from IETF session data including video, materials,
transcripts, and chat logs. Uses vcon-lib (the ``vcon`` package) for core
vCon construction, WTF transcription, and lawful basis extensions.

Output is vCon core-05 (syntax "0.5.0"). One IETF meeting session is one
Session Object (sessions[0]). The recording and the chat log are Dialogs that
the Session lists; every attachment is scoped with a party plus that Session
(or, for a transcript, the Dialog it describes). The Datatracker and Meetecho
give no join or drop times, so no Events are emitted.

Linked recordings (YouTube watch pages, ietf.org audio, Meetecho) are Dialogs
with ``url`` and no ``content_hash``. Their bytes are not stable or not
fetchable, so no honest hash exists. This follows the WG direction on core
issue https://github.com/ietf-wg-vcon/draft-ietf-vcon-vcon-core/issues/40,
which allows a ``url`` without ``content_hash``; the core-05 text (Section 2.4,
Appendix C) still says MUST until the WG revises it.
scripts/validate_vcons.py counts and reports these, not as failures.
"""

import base64
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from vcon import Vcon
from vcon.party import Party
from vcon.dialog import Dialog
from vcon.session import Session

from . import __version__
from .datatracker import MET_STATUS
from .hashing import content_hash_token
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


def json_body(payload: dict[str, Any]) -> dict[str, Any]:
    """The body of an ``encoding: "json"`` entry.

    Core-05 section 2.3.2: for ``"json"`` the body is the JSON value itself
    (an object here), not a serialized string. Releases before 0.4.0 wrote a
    string, following the core-02 reading; vcon 1.0.4 and later warn on that.
    """
    return payload


class VConBuilder:
    """Builder for creating vCon objects from IETF session data."""

    def __init__(self):
        self.vcon = Vcon.build_new()
        self._party_map: dict[str, int] = {}  # email/name -> party index
        # One IETF session is one core-05 Session. It is created up front so
        # every dialog and attachment has something to be scoped to; start and
        # duration are filled in by set_meeting_metadata().
        self.session_index: int = self.vcon.add_session(Session())
        # Party recorded as the author of vCon-level attachments (the lawful
        # basis, metadata, ingress info). Index 0 is the first chair when
        # chairs are known, otherwise the attendees party.
        self.attachment_party: int = 0
        # The meeting_metadata body is a JSON string, so it is rewritten whenever
        # something is added to it (see _write_meeting_metadata).
        self._meeting_metadata: dict[str, Any] | None = None
        self._meeting_metadata_index: int | None = None
        self._materials_info: list[dict[str, Any]] = []
        # When the session started, as the value given to grants' granted_at.
        self._session_start: datetime | None = None
        # The session's status slug, or None when unknown.
        self._session_status: str | None = None
        # The lawful basis attachment's position and its two proof entries; which
        # of them the file may carry is settled in build(), once every attachment
        # is known.
        self._lawful_basis_index: int | None = None
        self._lawful_proofs: list[dict] = []

    def set_subject(self, subject: str) -> "VConBuilder":
        """Set the vCon subject."""
        self.vcon.vcon_dict["subject"] = subject
        return self

    def set_meeting_metadata(
        self, meeting: IETFMeeting, session: IETFSession
    ) -> "VConBuilder":
        """Set metadata from IETF meeting and session."""
        # A numbered meeting reads "IETF 124 - VCON Working Group Session".
        # An interim's identifier is a name, not a number, so it is labelled
        # as the interim it is rather than dropped into the same sentence.
        if str(meeting.number).isdigit():
            self.vcon.vcon_dict["subject"] = (
                f"IETF {meeting.number} - {session.group_acronym.upper()} "
                f"Working Group Session"
            )
        else:
            self.vcon.vcon_dict["subject"] = (
                f"IETF {session.group_acronym.upper()} Working Group Interim "
                f"Session - {meeting.number}"
            )

        session_dict = self.vcon.vcon_dict["sessions"][self.session_index]
        if session.start_time:
            session_dict["start"] = session.start_time.isoformat()
        if session.duration_seconds:
            session_dict["duration"] = session.duration_seconds

        self._session_status = session.status
        if session.start_time:
            self._session_start = session.start_time
        elif meeting.start_date:
            # No scheduled time: date the grants by the meeting's first day.
            d = meeting.start_date
            self._session_start = datetime(d.year, d.month, d.day, tzinfo=UTC)
        else:
            self._session_start = None

        # Add meeting metadata as attachment. body is a JSON *string*: the spec
        # requires a string body even when the content is JSON.
        self._meeting_metadata = {
            "ietf_meeting_number": meeting.number,
            "location": f"{meeting.city}, {meeting.country}" if meeting.city else None,
            "working_group": session.group_acronym,
            "session_id": session.session_id,
            # Omitted when the status is unknown. A session that did not meet
            # has no start_time, duration_seconds or room (the datatracker
            # layer leaves them out), matching the Session, which has no start.
            **({"status": session.status} if session.status else {}),
            "room": session.room,
            "start_time": session.start_time.isoformat() if session.start_time else None,
            "duration_seconds": session.duration_seconds,
        }
        self._meeting_metadata_index = len(self.vcon.attachments)
        self.vcon.add_attachment(
            purpose="meeting_metadata",
            party=self.attachment_party,
            session=self.session_index,
            encoding="json",
            mediatype="application/json",
            body={},
        )
        self._write_meeting_metadata()

        return self

    def _write_meeting_metadata(self) -> None:
        """Serialize the meeting_metadata body, with the materials known so far."""
        if self._meeting_metadata is None or self._meeting_metadata_index is None:
            return
        payload = dict(self._meeting_metadata)
        if self._materials_info:
            payload["materials"] = self._materials_info
        attachment = self.vcon.vcon_dict["attachments"][self._meeting_metadata_index]
        attachment["body"] = json_body(payload)

    def add_party(
        self,
        name: str,
        email: str | None = None,
        role: str | None = None,
    ) -> int:
        """Add a party to the vCon. Returns the party index.

        core-05 defines no Party ``meta``, so none is written.
        """
        # Check if party already exists
        key = email or name
        if key in self._party_map:
            return self._party_map[key]

        party = Party(
            name=name,
            mailto=email,
            role=role,
        )
        index = len(self.vcon.parties)
        self.vcon.add_party(party)
        self._party_map[key] = index
        # `role` on a party is defined by the contact-center extension
        # (draft-ietf-vcon-cc-extension-02 section 3.1.1), registered as "CC",
        # so using it obliges the vCon to declare it. Without the declaration a
        # reader is entitled to ignore the field, which would drop who chaired
        # the session.
        if role:
            self.vcon.add_extension("CC")
        return index

    def add_persons(self, persons: list[IETFPerson]) -> list[int]:
        """Add multiple persons as parties. Returns list of party indices."""
        indices = []
        for person in persons:
            idx = self.add_party(
                name=person.name,
                email=person.email,
                role=person.role,
            )
            indices.append(idx)
        return indices

    def add_attendees_party(self) -> int:
        """Add a generic 'attendees' party representing all participants."""
        return self.add_party(
            name="IETF Attendees",
            role="attendee",
        )

    def add_video_dialog(
        self,
        video: VideoMetadata,
        session: IETFSession,
        party_indices: list[int] | None = None,
    ) -> int:
        """Add a linked video recording as a dialog. Returns dialog index.

        The dialog carries ``url`` and ``mediatype`` only. It has NO
        ``content_hash``: a YouTube watch page is mutable HTML, so no honest
        hash exists. This follows the WG direction on core issue
        https://github.com/ietf-wg-vcon/draft-ietf-vcon-vcon-core/issues/40
        (the core-05 Section 2.4 text still says MUST).
        No Dialog ``meta`` is written either: core-05 defines none.
        """
        start_time = session.start_time or datetime.now(UTC)

        dialog = Dialog(
            type="recording",
            start=start_time,
            duration=video.duration_seconds or session.duration_seconds,
            parties=party_indices or list(range(len(self.vcon.parties))),
            mediatype="video/mp4",
            url=video.url,
        )

        index = len(self.vcon.dialog)
        self.vcon.add_dialog(dialog, session=self.session_index)
        return index

    def add_video_dialog_from_url(
        self,
        url: str,
        session: IETFSession,
        mimetype: str = "video/mp4",
        party_indices: list[int] | None = None,
    ) -> int:
        """Add a linked recording (YouTube, Meetecho, ietf.org MP3) from a URL.

        ``url`` and ``mediatype`` only; no ``content_hash`` (none can honestly
        be computed for these targets), following the WG direction on core
        issue #40. No Dialog ``meta``.
        """
        start_time = session.start_time or datetime.now(UTC)

        dialog = Dialog(
            type="recording",
            start=start_time,
            duration=session.duration_seconds,
            parties=party_indices or list(range(len(self.vcon.parties))),
            mediatype=mimetype,
            url=url,
        )

        index = len(self.vcon.dialog)
        self.vcon.add_dialog(dialog, session=self.session_index)
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
            type="recording",
            start=start_time,
            duration=session.duration_seconds,
            parties=party_indices or list(range(len(self.vcon.parties))),
            mediatype="video/mp4",
            filename=video_path.name,
            body=encoded,
            encoding="base64url",
        )

        index = len(self.vcon.dialog)
        self.vcon.add_dialog(dialog, session=self.session_index)
        return index

    def add_chat_dialog(
        self,
        messages: list[ChatMessage],
        session: IETFSession,
        as_text: bool = True,
    ) -> int:
        """Add chat log as a text dialog. Returns dialog index.

        No Dialog ``meta`` is written: core-05 defines none.
        """
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
        )

        index = len(self.vcon.dialog)
        self.vcon.add_dialog(dialog, session=self.session_index)

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
        """Add a meeting material as an attachment.

        Three shapes, in order of preference:

        1. ``inline`` with content -- the bytes are carried in the vCon.
        2. content available -- a proper external reference: url, mediatype,
           filename and content_hash as core-02 registers them, so any vCon
           reader sees a linked, integrity-checked document. core-02 requires
           url and content_hash together, which is why this needs the bytes.
        3. otherwise -- a reference in the body. This is the only option for a
           mutable landing page, and the fallback when a fetch fails.

        core-05 defines no attachment ``meta``, so the title and the Datatracker
        page of an external reference are recorded in the ``materials`` member
        of the ``meeting_metadata`` attachment instead.
        """
        if inline and content:
            self.vcon.add_attachment(
                purpose=material.type,
                party=self.attachment_party,
                session=self.session_index,
                body=base64.urlsafe_b64encode(content).decode("ascii"),
                encoding="base64url",
                mediatype=material.mimetype or "application/octet-stream",
                filename=material.filename,
                content_hash=content_hash_token(content),
            )
        elif content and not material.landing_page and (
            external_url := self._external_url(material)
        ):
            # The reference points at the file, not at the page about it. The
            # Datatracker material URL renders Markdown as HTML and converts
            # PowerPoint to PDF, so a content_hash of the published bytes
            # cannot describe what it serves; www.ietf.org/proceedings returns
            # them verbatim. The page is still what a human should open, so it
            # is kept alongside.
            self.vcon.add_attachment(
                purpose=material.type,
                party=self.attachment_party,
                session=self.session_index,
                url=external_url,
                content_hash=content_hash_token(content),
                mediatype=material.mimetype,
                filename=material.filename,
            )
            # An external reference has no body to carry a title, and core-05
            # has no attachment meta, so the details go to meeting_metadata.
            # `url` is always written: the same filename can be published under
            # two paths (an agenda and minutes both named "ntp.txt"). Every
            # external reference gets an entry, so the list matches the
            # referenced attachments one for one.
            info: dict[str, Any] = {}
            if material.filename:
                info["filename"] = material.filename
            info["url"] = external_url
            if material.title:
                info["title"] = material.title
            if material.file_url and material.file_url != material.url:
                info["datatracker_url"] = material.url
            self._materials_info.append(info)
            self._write_meeting_metadata()
        else:
            # A "json" body is the JSON value itself (core-05 section 2.3.2).
            self.vcon.add_attachment(
                purpose=material.type,
                party=self.attachment_party,
                session=self.session_index,
                body=json_body(
                    {
                        "url": material.url,
                        "mimetype": material.mimetype,
                        "filename": material.filename,
                        "title": material.title,
                    }
                ),
                encoding="json",
                mediatype="application/json",
            )

        return self

    @staticmethod
    def _external_url(material: IETFMaterial) -> str | None:
        """The URL for a hashed external reference, or None if none can be formed.

        ``url``, ``filename``, ``mediatype`` and ``content_hash`` must describe
        one file: the one whose bytes were read. ``material.filename`` is that
        file's name, but ``file_url`` is built from the Datatracker's
        ``uploaded_filename``, which can name a different sibling (the record
        says ``.pdf`` where the mirror and ietf.org hold only the ``.pptx``). So
        when the names differ the URL is rebuilt from the name actually read,
        in the same proceedings directory. A document with no ``file_url`` (an
        older one) keeps the Datatracker page as its URL, as before. If a
        differing name cannot be put into a URL, the caller falls back to an
        unhashed body reference.
        """
        url = material.file_url or material.url
        if not material.file_url or not material.filename:
            return url
        parsed = urlparse(url)
        if unquote(parsed.path.rsplit("/", 1)[-1]) == material.filename:
            return url
        if parsed.scheme not in ("http", "https") or not parsed.netloc or "/" not in parsed.path:
            logger.warning(
                "No URL serves %s as read for %s (%s); keeping an unhashed body reference",
                material.filename, material.title or material.type, url,
            )
            return None
        rebuilt = parsed._replace(
            path=parsed.path.rsplit("/", 1)[0] + "/" + quote(material.filename),
            query="", fragment="",
        ).geturl()
        logger.info(
            "%s: the file read is %s, not %s; the URL names it",
            material.title or material.type, material.filename,
            unquote(parsed.path.rsplit("/", 1)[-1]),
        )
        return rebuilt

    def add_materials(
        self,
        materials: list[IETFMaterial],
        inline: bool = False,
        downloader=None,
    ) -> "VConBuilder":
        """Add multiple materials as attachments.

        The bytes are fetched whenever a downloader is available, not only for
        inline mode: an external reference needs a content_hash, and the hash
        needs the bytes.
        """
        for material in materials:
            content = None
            if downloader and not material.landing_page:
                content, mediatype, filename = downloader.fetch_material(material)
                # Prefer what the file actually is over what the doc name implied.
                if mediatype:
                    material.mimetype = mediatype
                if filename:
                    material.filename = filename
            self.add_material_attachment(material, content=content, inline=inline)
        return self

    def _analysis_scope(self, dialog_index: int | None) -> dict[str, Any]:
        """What an analysis describes: a Dialog if there is one, else the Session."""
        if dialog_index is not None and 0 <= dialog_index < len(self.vcon.dialog):
            return {"dialog": dialog_index}
        return {"sessions": self.session_index}

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

        # draft-howe-vcon-wtf-extension section 6.1: "Transcription analysis
        # results MUST be stored as vCon analysis using the standard analysis
        # object structure." The prose in section 10.2 of that draft says
        # "attachment", which contradicts its own normative text and the
        # examples; the analysis object is what this follows. vcon-lib's
        # add_wtf_transcription_attachment() writes an attachment, so the
        # analysis object is assembled here instead.
        #
        # vcon-core requires a string body, paired with encoding "json" when
        # the content is JSON, and requires vendor on every analysis object.
        body = {
            "transcript": {
                "text": transcript.text,
                "language": transcript.language or "en",
                "duration": transcript.duration or 0.0,
                "confidence": avg_confidence,
            },
            "segments": segments,
            "metadata": {
                "created_at": datetime.now(UTC).isoformat(),
                "processed_at": datetime.now(UTC).isoformat(),
                "provider": transcript.provider,
                "model": transcript.model or "unknown",
            },
        }
        self.vcon.add_analysis(
            type="wtf_transcription",
            **self._analysis_scope(dialog_index),
            vendor=transcript.provider or "unknown",
            product=transcript.model or None,
            body=json_body(body),
            encoding="json",
            mediatype="application/json",
        )
        self.vcon.add_extension("wtf_transcription")
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
            **self._analysis_scope(dialog_index),
            vendor=vendor or "unknown",
            body=body,
            encoding="json" if isinstance(body, dict) else "none",
            **({"mediatype": "application/json"} if isinstance(body, dict) else {}),
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
            party=self.attachment_party,
            session=self.session_index,
            encoding="json",
            mediatype="application/json",
            body=json_body({
                "source": source,
                "converter_version": __version__,
                "converted_at": datetime.now(UTC).isoformat(),
                **kwargs,
            }),
        )
        return self

    def add_lawful_basis(
        self,
        lawful_basis: str,
        purpose_grants: list[dict] | None = None,
        proof_mechanisms: list[dict] | None = None,
        terms_of_service: str | None = None,
        terms_of_service_name: str | None = None,
        jurisdiction: str | None = None,
        controller: str | None = None,
        expiration: datetime | None = None,
        notes: str | None = None,
        granted_at: datetime | None = None,
    ) -> "VConBuilder":
        """Add a lawful basis attachment using vcon-lib's lawful basis extension.

        ``granted_at`` dates every grant; when omitted it is the session start
        known from set_meeting_metadata(), and the conversion time only if no
        session date is known at all.
        """
        granted_at = granted_at or self._session_start or datetime.now(UTC)
        grants = []
        for grant in (purpose_grants or []):
            grants.append({
                "purpose": grant.get("purpose", ""),
                "granted": grant.get("status", "granted") == "granted",
                "granted_at": granted_at.isoformat(),
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
            party_index=self.attachment_party,
            session_index=self.session_index,
        )
        attachment = self.vcon.vcon_dict["attachments"][-1]
        attachment.setdefault("mediatype", "application/json")
        body = attachment.get("body")
        if isinstance(body, str):
            body = json.loads(body)
        if isinstance(body, dict):
            # A basis with no proof_mechanisms[] is hollow: it asserts a basis
            # without saying what evidences it. vcon-lib's helper has no
            # parameter for the field, so it is set on the body here. Each
            # entry carries the fields draft-howe-vcon-lawful-basis requires
            # (proof_type, timestamp, proof_data); the timestamp defaults to
            # the grant time.
            if proof_mechanisms:
                body["proof_mechanisms"] = [
                    {**p, "timestamp": p.get("timestamp") or granted_at.isoformat()}
                    for p in proof_mechanisms
                ]
                self._lawful_proofs = [dict(p) for p in body["proof_mechanisms"]]
            attachment["body"] = json_body(body)
            attachment["encoding"] = "json"
        return self

    def add_ietf_note_well(self, session_start: datetime | None = None) -> "VConBuilder":
        """Add IETF Note Well as a lawful basis attachment.

        The IETF Note Well establishes the legal basis for recording and
        processing IETF meeting sessions.
        """
        proof_mechanisms = [
            {
                "proof_type": "external_system",
                "proof_data": {
                    "description": (
                        "IETF Note Well, the IETF's published policy for meeting "
                        "participation, at https://www.ietf.org/about/note-well/. "
                        "Participation in an IETF session constitutes agreement to it."
                    ),
                },
            },
        ]
        # The second mechanism asserts that the session was held and that its
        # bluesheets are referenced. That is false for a session whose status
        # says it did not meet (cancelled, not meeting, ...), so it is left
        # out there; and it is true only if this vCon carries a bluesheets
        # attachment, which build() checks once every attachment is known. An
        # unknown status keeps it, as before.
        if self._session_status in (None, MET_STATUS):
            proof_mechanisms.append({
                "proof_type": "external_system",
                "proof_data": {
                    "description": (
                        "IETF Datatracker session record, including the bluesheets "
                        "attendance document referenced by this vCon, evidencing that "
                        "the session was held and published under that policy."
                    ),
                },
            })
        self._lawful_proofs = [dict(p) for p in proof_mechanisms]
        result = self.add_lawful_basis(
            lawful_basis="legitimate_interests",
            granted_at=session_start,
            purpose_grants=[
                {"purpose": "recording", "status": "granted"},
                {"purpose": "transcription", "status": "granted"},
                {"purpose": "publication", "status": "granted"},
                {"purpose": "archival", "status": "granted"},
                {"purpose": "analysis", "status": "granted"},
            ],
            # What actually evidences the basis: the Note Well is a published
            # policy, and the Datatracker's own session record, including the
            # bluesheets attachment carried by this vCon, is the record of the
            # session having been held under it. Both are checkable by a third
            # party, which is the point of the field.
            proof_mechanisms=proof_mechanisms,
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
        self._lawful_basis_index = len(self.vcon.vcon_dict["attachments"]) - 1
        return result

    def _settle_lawful_basis(self) -> None:
        """Keep the second proof entry only if this vCon carries a bluesheets attachment.

        The entry refers to "the bluesheets attendance document referenced by
        this vCon". Materials are added after the lawful basis, so whether the
        reference is true is only known once the attachments are final. The
        attachment keeps its position; the wording of the entries is not changed.
        """
        if self._lawful_basis_index is None or not self._lawful_proofs:
            return
        attachment = self.vcon.vcon_dict["attachments"][self._lawful_basis_index]
        body = attachment["body"]
        if isinstance(body, str):
            body = json.loads(body)
        has_bluesheets = any(
            isinstance(a, dict) and a.get("purpose") == "bluesheets"
            for a in self.vcon.vcon_dict["attachments"]
        )
        body["proof_mechanisms"] = (
            self._lawful_proofs if has_bluesheets else self._lawful_proofs[:1]
        )
        attachment["body"] = json_body(body)

    def build(self) -> Vcon:
        """Build and return the final vCon."""
        self.vcon.vcon_dict["updated_at"] = datetime.now(UTC).isoformat()
        self._finish_session()
        self._settle_lawful_basis()
        return self.vcon

    def _finish_session(self) -> None:
        """Fill in the Session's receivers and originator.

        Parties are added chairs first, then the attendees party, so the first
        party is the organizer when one is known and is the implied originator
        (core-05 4.2.4); ``originator`` is therefore never written. With no
        chair known the attendees party is first and, by the same rule, is the
        implied originator: the Datatracker names no one else. Receivers are
        every party in the vCon.
        """
        session = self.vcon.vcon_dict["sessions"][self.session_index]
        count = len(self.vcon.parties)
        if count:
            session["parties"] = list(range(count))
        else:
            session.pop("parties", None)

    def to_json(self, indent: int = 2) -> str:
        """Build and serialize to JSON."""
        self.build()
        return self.vcon.to_json()

    def to_dict(self) -> dict[str, Any]:
        """Build and serialize to dictionary."""
        self.build()
        return self.vcon.to_dict()
