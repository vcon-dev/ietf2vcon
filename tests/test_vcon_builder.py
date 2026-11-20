"""Unit tests for ietf2vcon.vcon_builder module."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from ietf2vcon.models import IETFMaterial, IETFPerson
from ietf2vcon.vcon_builder import VConBuilder


class TestVConBuilderBasics:
    """Tests for basic VConBuilder functionality."""

    def test_builder_initialization(self):
        """Test VConBuilder creates a valid vCon."""
        builder = VConBuilder()
        vcon = builder.build()

        assert isinstance(vcon.uuid, str)
        assert vcon.created_at is not None

    def test_set_subject(self):
        """Test setting vCon subject."""
        builder = VConBuilder()
        builder.set_subject("Test Subject")
        vcon = builder.build()

        assert vcon.subject == "Test Subject"

    def test_builder_chaining(self):
        """Test builder methods support chaining."""
        builder = VConBuilder()
        result = builder.set_subject("Test")

        assert result is builder


class TestVConBuilderParties:
    """Tests for party management in VConBuilder."""

    def test_add_party_basic(self):
        """Test adding a basic party."""
        builder = VConBuilder()
        idx = builder.add_party(name="John Doe")
        vcon = builder.build()

        assert idx == 0
        assert len(vcon.parties) == 1
        assert vcon.parties[0]["name"] == "John Doe"

    def test_add_party_with_email(self):
        """Test adding party with email."""
        builder = VConBuilder()
        idx = builder.add_party(
            name="Jane Doe",
            email="jane@example.com",
            role="speaker",
        )
        vcon = builder.build()

        assert vcon.parties[idx]["mailto"] == "jane@example.com"
        assert vcon.parties[idx]["role"] == "speaker"

    def test_add_party_deduplication(self):
        """Test that duplicate parties are not added."""
        builder = VConBuilder()
        idx1 = builder.add_party(name="John", email="john@example.com")
        idx2 = builder.add_party(name="John Updated", email="john@example.com")

        assert idx1 == idx2
        vcon = builder.build()
        assert len(vcon.parties) == 1

    def test_add_persons(self):
        """Test adding multiple IETFPerson objects."""
        builder = VConBuilder()
        persons = [
            IETFPerson(name="Alice", email="alice@example.com", role="chair"),
            IETFPerson(name="Bob", email="bob@example.com", role="presenter"),
        ]
        indices = builder.add_persons(persons)
        vcon = builder.build()

        assert indices == [0, 1]
        assert len(vcon.parties) == 2
        assert vcon.parties[0]["name"] == "Alice"
        assert vcon.parties[1]["role"] == "presenter"

    def test_add_attendees_party(self):
        """Test adding generic attendees party."""
        builder = VConBuilder()
        idx = builder.add_attendees_party()
        vcon = builder.build()

        assert vcon.parties[idx]["name"] == "IETF Attendees"
        assert vcon.parties[idx]["role"] == "attendee"
        assert "meta" not in vcon.parties[idx]

    def test_party_never_carries_meta(self):
        """core-05 defines no Party meta, so an affiliation is not written."""
        builder = VConBuilder()
        builder.add_persons([
            IETFPerson(name="Alice", email="alice@example.com", role="chair",
                       affiliation="Example"),
        ])
        builder.add_attendees_party()
        vcon = builder.build()

        assert all("meta" not in p for p in vcon.parties)


class TestVConBuilderMetadata:
    """Tests for meeting metadata in VConBuilder."""

    def test_set_meeting_metadata(self, sample_ietf_meeting, sample_ietf_session):
        """Test setting meeting metadata."""
        builder = VConBuilder()
        builder.set_meeting_metadata(sample_ietf_meeting, sample_ietf_session)
        vcon = builder.build()

        assert "IETF 121" in vcon.subject
        assert "VCON" in vcon.subject

        # Check metadata attachment
        metadata_att = next(
            (a for a in vcon.attachments if a.get("purpose") == "meeting_metadata"), None
        )
        assert metadata_att is not None
        assert json.loads(metadata_att["body"])["ietf_meeting_number"] == 121
        assert json.loads(metadata_att["body"])["working_group"] == "vcon"


class TestVConBuilderDialogs:
    """Tests for dialog management in VConBuilder."""

    def test_add_video_dialog(self, sample_video_metadata, sample_ietf_session):
        """Test adding video dialog."""
        builder = VConBuilder()
        builder.add_party(name="Speaker")
        idx = builder.add_video_dialog(
            sample_video_metadata, sample_ietf_session, party_indices=[0]
        )
        vcon = builder.build()

        assert idx == 0
        assert len(vcon.dialog) == 1
        # vcon-core dialog types are recording/text/transfer/incomplete.
        # "video" is not one of them; the media type carries the video-ness.
        assert vcon.dialog[0]["type"] == "recording"
        # Linked recording: url + mediatype, no content_hash, no meta (core-05
        # defines no Dialog meta). A url without hash follows WG issue #40.
        assert vcon.dialog[0]["mediatype"] == "video/mp4"
        assert vcon.dialog[0]["url"] == sample_video_metadata.url
        assert "content_hash" not in vcon.dialog[0]
        assert "body" not in vcon.dialog[0]
        assert "meta" not in vcon.dialog[0]

    def test_add_video_dialog_from_url(self, sample_ietf_session):
        """Test adding video dialog from URL."""
        builder = VConBuilder()
        idx = builder.add_video_dialog_from_url(
            url="https://youtube.com/watch?v=abc123",
            session=sample_ietf_session,
        )
        vcon = builder.build()

        d = vcon.dialog[idx]
        assert d["url"] == "https://youtube.com/watch?v=abc123"
        assert d["mediatype"] == "video/mp4"
        assert d["type"] == "recording"
        assert not ({"content_hash", "body", "encoding", "meta"} & d.keys())

    def test_add_audio_recording_from_url(self, sample_ietf_session):
        """Older meetings link an MP3; the pre-port audio/mpeg type is kept."""
        builder = VConBuilder()
        idx = builder.add_video_dialog_from_url(
            url="https://www.ietf.org/audio/ietf90/ietf90-room1-20140721-1300.mp3",
            session=sample_ietf_session,
            mimetype="audio/mpeg",
        )
        d = builder.build().dialog[idx]
        assert d["mediatype"] == "audio/mpeg"
        assert d["url"].endswith(".mp3")
        assert not ({"content_hash", "body", "encoding", "meta"} & d.keys())

    def test_add_chat_dialog(self, sample_chat_messages, sample_ietf_session):
        """Test adding chat dialog."""
        builder = VConBuilder()
        idx = builder.add_chat_dialog(
            sample_chat_messages, sample_ietf_session, as_text=True
        )
        vcon = builder.build()

        assert idx == 0
        assert vcon.dialog[0]["type"] == "text"
        # core-05 defines no Dialog meta, so none is written.
        assert "meta" not in vcon.dialog[0]
        assert "Great presentation!" in vcon.dialog[0]["body"]

    def test_add_chat_dialog_empty(self, sample_ietf_session):
        """Test adding empty chat dialog returns -1."""
        builder = VConBuilder()
        idx = builder.add_chat_dialog([], sample_ietf_session)

        assert idx == -1


class TestVConBuilderMaterials:
    """Tests for materials/attachments in VConBuilder."""

    def test_add_material_attachment(self):
        """Test adding a material attachment."""
        builder = VConBuilder()
        material = IETFMaterial(
            type="slides",
            title="Test Slides",
            url="https://example.com/slides.pdf",
            filename="slides.pdf",
            mimetype="application/pdf",
        )
        builder.add_material_attachment(material)
        vcon = builder.build()

        att = next((a for a in vcon.attachments if a.get("purpose") == "slides"), None)
        assert att is not None
        # No content to hash, so this stays a body reference -- as a JSON
        # *string*, which is what the spec requires of every body.
        body = json.loads(att["body"])
        assert body["url"] == "https://example.com/slides.pdf"
        assert body["title"] == "Test Slides"
        assert att["encoding"] == "json"

    def test_add_materials(self, sample_materials):
        """Test adding multiple materials."""
        builder = VConBuilder()
        builder.add_materials(sample_materials)
        vcon = builder.build()

        # Should have agenda, slides, and recording
        purposes = [a.get("purpose") for a in vcon.attachments]
        assert "agenda" in purposes
        assert "slides" in purposes
        assert "recording" in purposes


class TestVConBuilderTranscript:
    """Tests for transcript/analysis in VConBuilder."""

    def test_add_transcript(self, sample_transcription_result):
        """A transcript is an analysis object, not an attachment.

        draft-howe-vcon-wtf-extension section 6.1 requires the analysis
        object. vcon-core requires a string body with encoding "json", and
        vendor on every analysis object.
        """
        builder = VConBuilder()
        builder.add_transcript(sample_transcription_result, dialog_index=0)
        vcon = builder.build()

        assert not [
            a for a in vcon.attachments
            if (a.get("purpose") or a.get("type")) == "wtf_transcription"
        ], "transcript must not be written as an attachment"

        wtf = next(
            (a for a in vcon.analysis if a.get("type") == "wtf_transcription"), None
        )
        assert wtf is not None
        assert wtf["vendor"], "vendor is required on every analysis object"
        assert wtf["encoding"] == "json"
        assert isinstance(wtf["body"], str), "analysis body must be a string"
        body = json.loads(wtf["body"])
        assert body["transcript"]["text"]
        assert body["segments"]
        assert "wtf_transcription" in vcon.vcon_dict["extensions"]

    def test_party_role_declares_cc_extension(self):
        """party.role is defined by the contact-center extension, token "CC"."""
        builder = VConBuilder()
        builder.add_party(name="Alex Rivera", role="chair")
        vcon = builder.build()

        assert vcon.parties[0]["role"] == "chair"
        assert vcon.vcon_dict["extensions"] == ["CC"]

    def test_no_role_no_cc_and_never_role_or_meta(self):
        builder = VConBuilder()
        builder.add_party(name="No Role")
        builder.add_ietf_note_well()
        vcon = builder.build()

        assert "CC" not in vcon.vcon_dict["extensions"]
        assert not {"role", "meta"} & set(vcon.vcon_dict["extensions"])

    def test_inline_json_attachments_carry_mediatype(
        self, sample_ietf_meeting, sample_ietf_session, sample_transcription_result
    ):
        builder = VConBuilder()
        builder.set_meeting_metadata(sample_ietf_meeting, sample_ietf_session)
        builder.add_ingress_info()
        builder.add_ietf_note_well()
        builder.add_transcript(sample_transcription_result, 0)
        vcon = builder.build()

        for att in vcon.attachments:
            assert att["encoding"] == "json" and att["mediatype"] == "application/json"
        assert {a["purpose"] for a in vcon.attachments} >= {
            "meeting_metadata", "ingress_info", "lawful_basis",
        }
        assert vcon.analysis[0]["mediatype"] == "application/json"

    def test_grants_are_dated_by_the_session_start(
        self, sample_ietf_meeting, sample_ietf_session
    ):
        builder = VConBuilder()
        builder.set_meeting_metadata(sample_ietf_meeting, sample_ietf_session)
        builder.add_ietf_note_well()
        vcon = builder.build()

        lb = next(a for a in vcon.attachments if a["purpose"] == "lawful_basis")
        grants = json.loads(lb["body"])["purpose_grants"]
        assert grants
        assert {g["granted_at"] for g in grants} == {vcon.vcon_dict["sessions"][0]["start"]}

    def test_grants_fall_back_to_the_meeting_date_at_midnight_utc(self):
        from datetime import datetime

        from ietf2vcon.models import IETFMeeting, IETFSession

        builder = VConBuilder()
        builder.set_meeting_metadata(
            IETFMeeting(number=121, start_date=datetime(2024, 11, 4)),
            IETFSession(meeting_number=121, group_acronym="vcon", session_id="1"),
        )
        builder.add_ietf_note_well()
        vcon = builder.build()

        assert "start" not in vcon.vcon_dict["sessions"][0]
        lb = next(a for a in vcon.attachments if a["purpose"] == "lawful_basis")
        grants = json.loads(lb["body"])["purpose_grants"]
        assert {g["granted_at"] for g in grants} == {"2024-11-04T00:00:00+00:00"}

    def test_note_well_carries_proof_mechanisms(self):
        """A lawful basis with no proof_mechanisms[] is hollow."""
        builder = VConBuilder()
        builder.add_ietf_note_well()
        vcon = builder.build()

        lb = next(
            a for a in vcon.attachments
            if (a.get("purpose") or a.get("type")) == "lawful_basis"
        )
        body = json.loads(lb["body"])
        assert body["lawful_basis"] == "legitimate_interests"
        assert body["proof_mechanisms"], "proof_mechanisms[] must not be empty"
        assert all(m.get("mechanism_type") for m in body["proof_mechanisms"])
        assert all(m.get("description") for m in body["proof_mechanisms"])

    def test_add_analysis_generic(self):
        """Test adding generic analysis."""
        builder = VConBuilder()
        builder.add_analysis(
            analysis_type="summary",
            body="This is a summary of the meeting.",
            dialog_index=0,
            vendor="test-vendor",
        )
        vcon = builder.build()

        assert vcon.analysis[0]["type"] == "summary"
        assert vcon.analysis[0]["body"] == "This is a summary of the meeting."


class TestVConBuilderLawfulBasis:
    """Tests for lawful basis attachments."""

    def test_add_lawful_basis(self):
        """Test adding lawful basis attachment."""
        builder = VConBuilder()
        builder.add_lawful_basis(
            lawful_basis="consent",
            purpose_grants=[{"purpose": "recording", "status": "granted"}],
            terms_of_service="https://example.com/terms",
            jurisdiction="US",
        )
        vcon = builder.build()

        lb_att = next(
            (a for a in vcon.attachments if a.get("purpose") == "lawful_basis"), None
        )
        assert lb_att is not None
        assert json.loads(lb_att["body"])["lawful_basis"] == "consent"
        # Extra fields are in metadata sub-dict per vcon-lib API
        assert json.loads(lb_att["body"])["metadata"]["jurisdiction"] == "US"

    def test_add_ietf_note_well(self):
        """Test adding IETF Note Well."""
        builder = VConBuilder()
        builder.add_ietf_note_well()
        vcon = builder.build()

        lb_att = next(
            (a for a in vcon.attachments if a.get("purpose") == "lawful_basis"), None
        )
        assert lb_att is not None
        assert json.loads(lb_att["body"])["lawful_basis"] == "legitimate_interests"
        assert json.loads(lb_att["body"])["terms_of_service"] == "https://www.ietf.org/about/note-well/"
        assert json.loads(lb_att["body"])["metadata"]["terms_of_service_name"] == "IETF Note Well"
        assert json.loads(lb_att["body"])["metadata"]["controller"] == "Internet Engineering Task Force (IETF)"

    def test_note_well_purpose_grants(self):
        """Test Note Well includes all required purpose grants."""
        builder = VConBuilder()
        builder.add_ietf_note_well()
        vcon = builder.build()

        lb_att = next(
            (a for a in vcon.attachments if a.get("purpose") == "lawful_basis"), None
        )
        purposes = [g["purpose"] for g in json.loads(lb_att["body"])["purpose_grants"]]

        assert "recording" in purposes
        assert "transcription" in purposes
        assert "publication" in purposes
        assert "archival" in purposes
        assert "analysis" in purposes


class TestVConBuilderIngressInfo:
    """Tests for ingress info attachment."""

    def test_add_ingress_info(self):
        """Test adding ingress info."""
        builder = VConBuilder()
        builder.add_ingress_info(
            source="ietf2vcon",
            meeting_number=121,
            group_acronym="vcon",
        )
        vcon = builder.build()

        ing_att = next(
            (a for a in vcon.attachments if a.get("purpose") == "ingress_info"), None
        )
        assert ing_att is not None
        assert json.loads(ing_att["body"])["source"] == "ietf2vcon"
        assert json.loads(ing_att["body"])["meeting_number"] == 121
        assert "converted_at" in ing_att["body"]


class TestVConBuilderSerialization:
    """Tests for vCon serialization."""

    def test_to_json(self):
        """Test JSON serialization."""
        builder = VConBuilder()
        builder.set_subject("Test")
        json_str = builder.to_json()

        assert "Test" in json_str
        assert "uuid" in json_str

    def test_to_dict(self):
        """Test dictionary serialization."""
        builder = VConBuilder()
        builder.set_subject("Test")
        data = builder.to_dict()

        assert data["subject"] == "Test"
        assert "uuid" in data


class TestSessionStatusInMetadata:
    def _body(self, session, meeting=None):
        from ietf2vcon.models import IETFMeeting

        builder = VConBuilder()
        builder.set_meeting_metadata(meeting or IETFMeeting(number=100), session)
        builder.add_ietf_note_well()
        vcon = builder.build()
        attachments = {a["purpose"]: a for a in vcon.attachments}
        return vcon, json.loads(attachments["meeting_metadata"]["body"]), attachments

    def test_status_is_recorded_for_a_scheduled_session(self, sample_ietf_session):
        session = sample_ietf_session.model_copy(update={"status": "sched"})
        _, body, _ = self._body(session)
        assert body["status"] == "sched" and body["start_time"] is not None

    def test_a_session_that_did_not_meet_has_no_start_duration_or_room(self):
        from datetime import datetime

        from ietf2vcon.models import IETFMeeting, IETFSession

        session = IETFSession(
            meeting_number=100, group_acronym="dnsop", session_id="25892", status="canceled",
        )
        vcon, body, attachments = self._body(
            session, IETFMeeting(number=100, start_date=datetime(2017, 11, 12))
        )

        assert body["status"] == "canceled"
        assert body["start_time"] is None and body["duration_seconds"] is None
        assert body["room"] is None
        assert "start" not in vcon.vcon_dict["sessions"][0]
        assert "duration" not in vcon.vcon_dict["sessions"][0]
        grants = json.loads(attachments["lawful_basis"]["body"])["purpose_grants"]
        assert {g["granted_at"] for g in grants} == {"2017-11-12T00:00:00+00:00"}

    def test_no_status_key_when_the_status_is_unknown(self, sample_ietf_session):
        _, body, _ = self._body(sample_ietf_session)
        assert "status" not in body


NOTE_WELL_ENTRY = (
    "IETF Note Well, the IETF's published policy for meeting participation, at "
    "https://www.ietf.org/about/note-well/. Participation in an IETF session "
    "constitutes agreement to it."
)
BLUESHEETS_ENTRY = (
    "IETF Datatracker session record, including the bluesheets attendance document "
    "referenced by this vCon, evidencing that the session was held and published "
    "under that policy."
)


class TestLawfulBasisProofEntries:
    """The second proof entry is made only for a session that met and only if the file has bluesheets."""

    def _vcon(self, status, bluesheets, bluesheets_first=False):
        from datetime import datetime

        from ietf2vcon.models import IETFMaterial, IETFMeeting, IETFSession

        builder = VConBuilder()
        builder.set_meeting_metadata(
            IETFMeeting(number=126, start_date=datetime(2026, 7, 18)),
            IETFSession(
                meeting_number=126, group_acronym="dance", session_id="35574", status=status,
            ),
        )
        sheets = IETFMaterial(
            type="bluesheets", title="Bluesheets", filename="bluesheets-126-dance-00.txt",
            url="https://datatracker.ietf.org/meeting/126/materials/bluesheets-126-dance",
            file_url="https://www.ietf.org/proceedings/126/bluesheets/bluesheets-126-dance-00.txt",
            mimetype="text/plain",
        )
        if bluesheets and bluesheets_first:
            builder.add_material_attachment(sheets, content=b"x")
        builder.add_ietf_note_well()
        if bluesheets and not bluesheets_first:
            builder.add_material_attachment(sheets, content=b"x")
        return builder.build()

    def _proofs(self, vcon):
        lb = next(a for a in vcon.attachments if a["purpose"] == "lawful_basis")
        assert isinstance(lb["body"], str)  # still a JSON string
        return [p["description"] for p in json.loads(lb["body"])["proof_mechanisms"]]

    def test_scheduled_with_bluesheets_gives_both_entries(self):
        assert self._proofs(self._vcon("sched", True)) == [NOTE_WELL_ENTRY, BLUESHEETS_ENTRY]

    def test_scheduled_without_bluesheets_gives_only_the_note_well(self):
        assert self._proofs(self._vcon("sched", False)) == [NOTE_WELL_ENTRY]

    @pytest.mark.parametrize("status", ["notmeet", "canceled", "canceledpa", "resched", "deleted"])
    def test_not_scheduled_with_bluesheets_gives_only_the_note_well(self, status):
        assert self._proofs(self._vcon(status, True)) == [NOTE_WELL_ENTRY]

    def test_an_unknown_status_follows_the_bluesheets(self):
        assert self._proofs(self._vcon(None, True)) == [NOTE_WELL_ENTRY, BLUESHEETS_ENTRY]
        assert self._proofs(self._vcon(None, False)) == [NOTE_WELL_ENTRY]

    def test_bluesheets_added_after_the_lawful_basis_still_count(self):
        assert self._proofs(self._vcon("sched", True, bluesheets_first=False)) == [
            NOTE_WELL_ENTRY, BLUESHEETS_ENTRY,
        ]
        assert self._proofs(self._vcon("sched", True, bluesheets_first=True)) == [
            NOTE_WELL_ENTRY, BLUESHEETS_ENTRY,
        ]

    def test_the_lawful_basis_keeps_its_position(self):
        vcon = self._vcon("sched", True)
        assert [a["purpose"] for a in vcon.attachments] == [
            "meeting_metadata", "lawful_basis", "bluesheets",
        ]

    def test_building_twice_gives_the_same_body(self):
        from datetime import datetime

        from ietf2vcon.models import IETFMeeting, IETFSession

        builder = VConBuilder()
        builder.set_meeting_metadata(
            IETFMeeting(number=126, start_date=datetime(2026, 7, 18)),
            IETFSession(meeting_number=126, group_acronym="dance", session_id="1", status="sched"),
        )
        builder.add_ietf_note_well()
        first = builder.build().attachments[-1]["body"]
        assert builder.build().attachments[-1]["body"] == first

    def test_nothing_else_in_the_body_changes(self):
        with_sheets = json.loads(
            next(a for a in self._vcon("sched", True).attachments if a["purpose"] == "lawful_basis")["body"]
        )
        without = json.loads(
            next(a for a in self._vcon("sched", False).attachments if a["purpose"] == "lawful_basis")["body"]
        )
        with_sheets["proof_mechanisms"] = with_sheets["proof_mechanisms"][:1]
        with_sheets.pop("expiration"), without.pop("expiration")
        assert with_sheets == without
        assert list(with_sheets) == list(without)  # same key order too
