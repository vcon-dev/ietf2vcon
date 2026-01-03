"""Unit tests for ietf2vcon.vcon_builder module."""

from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest

from ietf2vcon.models import DialogType, IETFMaterial, IETFPerson
from ietf2vcon.vcon_builder import VConBuilder


class TestVConBuilderBasics:
    """Tests for basic VConBuilder functionality."""

    def test_builder_initialization(self):
        """Test VConBuilder creates a valid vCon."""
        builder = VConBuilder()
        vcon = builder.build()

        assert isinstance(vcon.uuid, UUID)
        assert vcon.vcon == "0.0.1"
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
        assert vcon.parties[0].name == "John Doe"

    def test_add_party_with_email(self):
        """Test adding party with email."""
        builder = VConBuilder()
        idx = builder.add_party(
            name="Jane Doe",
            email="jane@example.com",
            role="speaker",
        )
        vcon = builder.build()

        assert vcon.parties[idx].mailto == "jane@example.com"
        assert vcon.parties[idx].role == "speaker"

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
        assert vcon.parties[0].name == "Alice"
        assert vcon.parties[1].role == "presenter"

    def test_add_attendees_party(self):
        """Test adding generic attendees party."""
        builder = VConBuilder()
        idx = builder.add_attendees_party(count=50)
        vcon = builder.build()

        assert vcon.parties[idx].name == "IETF Attendees"
        assert vcon.parties[idx].role == "attendee"
        assert vcon.parties[idx].meta["count"] == 50


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
            (a for a in vcon.attachments if a.type == "meeting_metadata"), None
        )
        assert metadata_att is not None
        assert metadata_att.body["ietf_meeting_number"] == 121
        assert metadata_att.body["working_group"] == "vcon"


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
        assert vcon.dialog[0].type == DialogType.VIDEO
        assert vcon.dialog[0].url == sample_video_metadata.url

    def test_add_video_dialog_from_url(self, sample_ietf_session):
        """Test adding video dialog from URL."""
        builder = VConBuilder()
        idx = builder.add_video_dialog_from_url(
            url="https://youtube.com/watch?v=abc123",
            session=sample_ietf_session,
        )
        vcon = builder.build()

        assert vcon.dialog[idx].url == "https://youtube.com/watch?v=abc123"
        assert vcon.dialog[idx].type == DialogType.VIDEO

    def test_add_chat_dialog(self, sample_chat_messages, sample_ietf_session):
        """Test adding chat dialog."""
        builder = VConBuilder()
        idx = builder.add_chat_dialog(
            sample_chat_messages, sample_ietf_session, as_text=True
        )
        vcon = builder.build()

        assert idx == 0
        assert vcon.dialog[0].type == DialogType.TEXT
        assert vcon.dialog[0].meta["source"] == "zulip"
        assert vcon.dialog[0].meta["message_count"] == 2

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

        att = next((a for a in vcon.attachments if a.type == "slides"), None)
        assert att is not None
        # URL is now stored in body per vCon spec
        assert att.body["url"] == "https://example.com/slides.pdf"
        assert att.body["title"] == "Test Slides"
        assert att.encoding == "none"

    def test_add_materials(self, sample_materials):
        """Test adding multiple materials."""
        builder = VConBuilder()
        builder.add_materials(sample_materials)
        vcon = builder.build()

        # Should have agenda, slides, and recording
        types = [a.type for a in vcon.attachments]
        assert "agenda" in types
        assert "slides" in types
        assert "recording" in types


class TestVConBuilderTranscript:
    """Tests for transcript/analysis in VConBuilder."""

    def test_add_transcript(self, sample_transcription_result):
        """Test adding transcript."""
        builder = VConBuilder()
        builder.add_transcript(sample_transcription_result, dialog_index=0)
        vcon = builder.build()

        assert len(vcon.analysis) == 1
        assert vcon.analysis[0].type == "wtf_transcription"
        assert vcon.analysis[0].dialog == 0

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

        assert vcon.analysis[0].type == "summary"
        assert vcon.analysis[0].body == "This is a summary of the meeting."


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

        lb_att = next((a for a in vcon.attachments if a.type == "lawful_basis"), None)
        assert lb_att is not None
        assert lb_att.body["lawful_basis"] == "consent"
        assert lb_att.body["jurisdiction"] == "US"
        assert lb_att.meta["spec"] == "draft-howe-vcon-lawful-basis-00"

    def test_add_ietf_note_well(self):
        """Test adding IETF Note Well."""
        builder = VConBuilder()
        builder.add_ietf_note_well()
        vcon = builder.build()

        lb_att = next((a for a in vcon.attachments if a.type == "lawful_basis"), None)
        assert lb_att is not None
        assert lb_att.body["lawful_basis"] == "legitimate_interests"
        assert lb_att.body["terms_of_service"] == "https://www.ietf.org/about/note-well/"
        assert lb_att.body["terms_of_service_name"] == "IETF Note Well"
        assert lb_att.body["controller"] == "Internet Engineering Task Force (IETF)"

    def test_note_well_purpose_grants(self):
        """Test Note Well includes all required purpose grants."""
        builder = VConBuilder()
        builder.add_ietf_note_well()
        vcon = builder.build()

        lb_att = next((a for a in vcon.attachments if a.type == "lawful_basis"), None)
        purposes = [g["purpose"] for g in lb_att.body["purpose_grants"]]

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

        ing_att = next((a for a in vcon.attachments if a.type == "ingress_info"), None)
        assert ing_att is not None
        assert ing_att.body["source"] == "ietf2vcon"
        assert ing_att.body["meeting_number"] == 121
        assert "converted_at" in ing_att.body


class TestVConBuilderSerialization:
    """Tests for vCon serialization."""

    def test_to_json(self):
        """Test JSON serialization."""
        builder = VConBuilder()
        builder.set_subject("Test")
        json_str = builder.to_json()

        assert '"subject": "Test"' in json_str
        assert '"vcon": "0.0.1"' in json_str

    def test_to_dict(self):
        """Test dictionary serialization."""
        builder = VConBuilder()
        builder.set_subject("Test")
        data = builder.to_dict()

        assert data["subject"] == "Test"
        assert "uuid" in data
