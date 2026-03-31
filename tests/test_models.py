"""Unit tests for ietf2vcon.models module."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from ietf2vcon.models import (
    ChatMessage,
    IETFMaterial,
    IETFMeeting,
    IETFPerson,
    IETFSession,
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
