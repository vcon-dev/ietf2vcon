"""Unit tests for ietf2vcon.models module."""

from datetime import datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from ietf2vcon.models import (
    ChatMessage,
    DialogType,
    IETFMaterial,
    IETFMeeting,
    IETFPerson,
    IETFSession,
    LawfulBasisAttachment,
    LawfulBasisType,
    PartyRole,
    PurposeGrant,
    TranscriptSegment,
    VCon,
    VConAnalysis,
    VConAttachment,
    VConDialog,
    VConParty,
)


class TestIETFModels:
    """Tests for IETF data models."""

    def test_ietf_meeting_creation(self):
        """Test creating an IETFMeeting."""
        meeting = IETFMeeting(
            number=121,
            city="Dublin",
            country="IE",
        )
        assert meeting.number == 121
        assert meeting.city == "Dublin"
        assert meeting.country == "IE"

    def test_ietf_meeting_optional_fields(self):
        """Test IETFMeeting with all optional fields."""
        meeting = IETFMeeting(
            number=121,
            city="Dublin",
            country="IE",
            start_date=datetime(2024, 11, 2),
            end_date=datetime(2024, 11, 8),
            time_zone="Europe/Dublin",
        )
        assert meeting.start_date == datetime(2024, 11, 2)
        assert meeting.time_zone == "Europe/Dublin"

    def test_ietf_session_creation(self, sample_ietf_session):
        """Test creating an IETFSession."""
        assert sample_ietf_session.meeting_number == 121
        assert sample_ietf_session.group_acronym == "vcon"
        assert sample_ietf_session.session_id == "33406"

    def test_ietf_material_creation(self):
        """Test creating an IETFMaterial."""
        material = IETFMaterial(
            type="slides",
            title="Chair Slides",
            url="https://example.com/slides.pdf",
        )
        assert material.type == "slides"
        assert material.title == "Chair Slides"

    def test_ietf_person_creation(self):
        """Test creating an IETFPerson."""
        person = IETFPerson(
            name="John Doe",
            email="john@example.com",
            affiliation="Example Corp",
            role="chair",
        )
        assert person.name == "John Doe"
        assert person.role == "chair"


class TestVConModels:
    """Tests for vCon data models."""

    def test_vcon_party_creation(self):
        """Test creating a VConParty."""
        party = VConParty(
            name="Test User",
            mailto="test@example.com",
            role="speaker",
        )
        assert party.name == "Test User"
        assert party.mailto == "test@example.com"

    def test_dialog_type_enum(self):
        """Test DialogType enum values."""
        assert DialogType.VIDEO == "video"
        assert DialogType.AUDIO == "audio"
        assert DialogType.TEXT == "text"
        assert DialogType.RECORDING == "recording"

    def test_vcon_dialog_creation(self):
        """Test creating a VConDialog."""
        dialog = VConDialog(
            type=DialogType.VIDEO,
            start=datetime(2024, 11, 7, 15, 30),
            parties=[0, 1],
            duration=3600,
            url="https://youtube.com/watch?v=abc123",
        )
        assert dialog.type == DialogType.VIDEO
        assert dialog.duration == 3600
        assert len(dialog.parties) == 2

    def test_vcon_attachment_creation(self):
        """Test creating a VConAttachment."""
        attachment = VConAttachment(
            type="slides",
            mimetype="application/pdf",
            url="https://example.com/slides.pdf",
        )
        assert attachment.type == "slides"
        assert attachment.mimetype == "application/pdf"

    def test_vcon_attachment_with_body(self):
        """Test VConAttachment with inline body."""
        attachment = VConAttachment(
            type="meeting_metadata",
            encoding="none",
            body={"meeting": 121, "group": "vcon"},
        )
        assert attachment.body["meeting"] == 121

    def test_transcript_segment_creation(self):
        """Test creating a TranscriptSegment."""
        segment = TranscriptSegment(
            id=0,
            start=0.0,
            end=5.5,
            text="Hello world",
            confidence=0.95,
        )
        assert segment.start == 0.0
        assert segment.end == 5.5
        assert segment.confidence == 0.95

    def test_transcript_segment_with_speaker(self):
        """Test TranscriptSegment with speaker attribution."""
        segment = TranscriptSegment(
            id=1,
            start=5.5,
            end=10.0,
            text="Response text",
            speaker=1,
        )
        assert segment.speaker == 1

    def test_vcon_analysis_creation(self):
        """Test creating a VConAnalysis."""
        analysis = VConAnalysis(
            type="wtf_transcription",
            dialog=0,
            vendor="youtube",
            spec="draft-howe-wtf-transcription-00",
            body={"transcript": {"text": "Hello"}},
            encoding="none",
        )
        assert analysis.type == "wtf_transcription"
        assert analysis.spec == "draft-howe-wtf-transcription-00"

    def test_vcon_creation(self):
        """Test creating a VCon."""
        vcon = VCon(subject="Test Conversation")
        assert vcon.subject == "Test Conversation"
        assert vcon.vcon == "0.0.1"
        assert isinstance(vcon.uuid, UUID)
        assert isinstance(vcon.created_at, datetime)

    def test_vcon_to_json(self):
        """Test VCon JSON serialization."""
        vcon = VCon(subject="Test")
        json_str = vcon.to_json()
        assert '"subject": "Test"' in json_str
        assert '"vcon": "0.0.1"' in json_str

    def test_vcon_to_dict(self):
        """Test VCon dictionary serialization."""
        vcon = VCon(subject="Test")
        data = vcon.to_dict()
        assert data["subject"] == "Test"
        assert "uuid" in data


class TestChatMessageModel:
    """Tests for ChatMessage model."""

    def test_chat_message_creation(self):
        """Test creating a ChatMessage."""
        msg = ChatMessage(
            timestamp=datetime(2024, 11, 7, 15, 30),
            sender="Alice",
            content="Hello!",
        )
        assert msg.sender == "Alice"
        assert msg.content == "Hello!"

    def test_chat_message_with_stream(self):
        """Test ChatMessage with stream/topic info."""
        msg = ChatMessage(
            timestamp=datetime(2024, 11, 7, 15, 30),
            sender="Bob",
            content="Question here",
            sender_email="bob@example.com",
            topic="vCon Discussion",
            stream="vcon",
        )
        assert msg.stream == "vcon"
        assert msg.topic == "vCon Discussion"


class TestLawfulBasisModels:
    """Tests for lawful basis models."""

    def test_lawful_basis_type_enum(self):
        """Test LawfulBasisType enum values."""
        assert LawfulBasisType.CONSENT == "consent"
        assert LawfulBasisType.LEGITIMATE_INTERESTS == "legitimate_interests"
        assert LawfulBasisType.PUBLIC_TASK == "public_task"

    def test_purpose_grant_creation(self):
        """Test creating a PurposeGrant."""
        grant = PurposeGrant(
            purpose="recording",
            status="granted",
        )
        assert grant.purpose == "recording"
        assert grant.status == "granted"

    def test_lawful_basis_attachment_creation(self):
        """Test creating a LawfulBasisAttachment."""
        attachment = LawfulBasisAttachment(
            lawful_basis=LawfulBasisType.LEGITIMATE_INTERESTS,
            terms_of_service="https://www.ietf.org/about/note-well/",
            terms_of_service_name="IETF Note Well",
            jurisdiction="IETF",
            controller="Internet Engineering Task Force",
        )
        assert attachment.lawful_basis == LawfulBasisType.LEGITIMATE_INTERESTS
        assert attachment.terms_of_service_name == "IETF Note Well"

    def test_lawful_basis_with_purpose_grants(self):
        """Test LawfulBasisAttachment with purpose grants."""
        grants = [
            PurposeGrant(purpose="recording", status="granted"),
            PurposeGrant(purpose="transcription", status="granted"),
        ]
        attachment = LawfulBasisAttachment(
            lawful_basis=LawfulBasisType.CONSENT,
            purpose_grants=grants,
        )
        assert len(attachment.purpose_grants) == 2
        assert attachment.purpose_grants[0].purpose == "recording"


class TestPartyRole:
    """Tests for PartyRole enum."""

    def test_party_role_values(self):
        """Test PartyRole enum values."""
        assert PartyRole.CHAIR == "chair"
        assert PartyRole.PRESENTER == "presenter"
        assert PartyRole.SPEAKER == "speaker"
        assert PartyRole.ATTENDEE == "attendee"
        assert PartyRole.SCRIBE == "scribe"
