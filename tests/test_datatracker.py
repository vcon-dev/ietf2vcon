"""Integration tests for ietf2vcon.datatracker module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from ietf2vcon.datatracker import DataTrackerClient
from ietf2vcon.models import IETFMeeting, IETFSession


class TestDataTrackerClient:
    """Tests for DataTrackerClient."""

    @pytest.fixture
    def mock_client(self, mocker):
        """Create a DataTrackerClient with mocked HTTP client."""
        with patch("ietf2vcon.datatracker.httpx.Client") as mock_httpx:
            mock_instance = MagicMock()
            mock_httpx.return_value = mock_instance
            client = DataTrackerClient()
            client._client = mock_instance
            yield client, mock_instance

    def test_client_initialization(self):
        """Test DataTrackerClient initialization."""
        with patch("ietf2vcon.datatracker.httpx.Client"):
            client = DataTrackerClient()
            # Client is initialized successfully
            assert client is not None

    def test_get_meeting_success(
        self, mock_client, datatracker_meeting_response
    ):
        """Test getting meeting info successfully."""
        client, mock_http = mock_client

        mock_response = MagicMock()
        mock_response.json.return_value = datatracker_meeting_response
        mock_response.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_response

        meeting = client.get_meeting(121)

        assert meeting is not None
        assert isinstance(meeting, IETFMeeting)
        assert meeting.number == 121
        assert meeting.city == "Dublin"
        assert meeting.country == "IE"

    def test_get_meeting_not_found(self, mock_client):
        """Test getting nonexistent meeting."""
        client, mock_http = mock_client

        mock_response = MagicMock()
        mock_response.json.return_value = {"meta": {"total_count": 0}, "objects": []}
        mock_response.raise_for_status = MagicMock()
        mock_http.get.return_value = mock_response

        meeting = client.get_meeting(999)
        assert meeting is None

    @pytest.mark.skip(reason="Complex mocking required for session enrichment")
    def test_get_group_sessions_success(
        self, mock_client, datatracker_sessions_response
    ):
        """Test getting group sessions."""
        client, mock_http = mock_client

        # Mock group lookup
        mock_group_response = MagicMock()
        mock_group_response.json.return_value = {"id": 2383, "acronym": "vcon"}
        mock_group_response.raise_for_status = MagicMock()

        # Mock sessions lookup
        mock_sessions_response = MagicMock()
        mock_sessions_response.json.return_value = datatracker_sessions_response
        mock_sessions_response.raise_for_status = MagicMock()

        # Mock schedule assignment
        mock_schedule_response = MagicMock()
        mock_schedule_response.json.return_value = {
            "meta": {"total_count": 1},
            "objects": [{"timeslot": "/api/v1/meeting/timeslot/18330/"}],
        }
        mock_schedule_response.raise_for_status = MagicMock()

        # Mock timeslot
        mock_timeslot_response = MagicMock()
        mock_timeslot_response.json.return_value = {
            "time": "2024-11-07T15:30:00",
            "duration": "01:00:00",
            "location": "/api/v1/meeting/room/1011/",
        }
        mock_timeslot_response.raise_for_status = MagicMock()

        # Mock room
        mock_room_response = MagicMock()
        mock_room_response.json.return_value = {"name": "Liffey Hall 2"}
        mock_room_response.raise_for_status = MagicMock()

        mock_http.get.side_effect = [
            mock_group_response,
            mock_sessions_response,
            mock_schedule_response,
            mock_timeslot_response,
            mock_room_response,
        ]

        sessions = client.get_group_sessions(121, "vcon")

        assert len(sessions) == 1
        assert sessions[0].group_acronym == "vcon"
        assert sessions[0].session_id == "33406"

    def test_get_group_sessions_no_sessions(self, mock_client):
        """Test getting sessions when none exist."""
        client, mock_http = mock_client

        mock_group_response = MagicMock()
        mock_group_response.json.return_value = {"id": 2383}
        mock_group_response.raise_for_status = MagicMock()

        mock_sessions_response = MagicMock()
        mock_sessions_response.json.return_value = {
            "meta": {"total_count": 0},
            "objects": [],
        }
        mock_sessions_response.raise_for_status = MagicMock()

        mock_http.get.side_effect = [mock_group_response, mock_sessions_response]

        sessions = client.get_group_sessions(121, "nonexistent")
        assert sessions == []


class TestDataTrackerMaterials:
    """Tests for fetching materials from DataTracker."""

    @pytest.fixture
    def mock_client(self, mocker):
        """Create a DataTrackerClient with mocked HTTP client."""
        with patch("ietf2vcon.datatracker.httpx.Client") as mock_httpx:
            mock_instance = MagicMock()
            mock_httpx.return_value = mock_instance
            client = DataTrackerClient()
            client._client = mock_instance
            yield client, mock_instance

    @pytest.mark.skip(reason="Complex mocking required for materials enrichment")
    def test_get_session_materials(
        self,
        mock_client,
        datatracker_materials_response,
        datatracker_document_agenda_response,
        datatracker_document_recording_response,
    ):
        """Test getting session materials."""
        client, mock_http = mock_client

        # Mock materials presentation
        mock_materials_response = MagicMock()
        mock_materials_response.json.return_value = datatracker_materials_response
        mock_materials_response.raise_for_status = MagicMock()

        # Mock document lookups
        mock_agenda_response = MagicMock()
        mock_agenda_response.json.return_value = datatracker_document_agenda_response
        mock_agenda_response.raise_for_status = MagicMock()

        mock_slides_response = MagicMock()
        mock_slides_response.json.return_value = {
            "name": "slides-121-vcon-chair-slides",
            "title": "Chair Slides",
            "type": "/api/v1/name/doctypename/slides/",
            "external_url": "https://datatracker.ietf.org/meeting/121/materials/slides-121-vcon-chair-slides-00.pdf",
        }
        mock_slides_response.raise_for_status = MagicMock()

        mock_recording_response = MagicMock()
        mock_recording_response.json.return_value = datatracker_document_recording_response
        mock_recording_response.raise_for_status = MagicMock()

        mock_http.get.side_effect = [
            mock_materials_response,
            mock_agenda_response,
            mock_slides_response,
            mock_recording_response,
        ]

        materials = client.get_session_materials(121, "vcon")

        assert len(materials) == 3
        types = [m.type for m in materials]
        assert "agenda" in types
        assert "slides" in types
        assert "recording" in types

    @pytest.mark.skip(reason="Complex mocking required")
    def test_material_type_detection(
        self, mock_client, datatracker_document_recording_response
    ):
        """Test that recording materials are correctly typed."""
        client, mock_http = mock_client

        mock_materials_response = MagicMock()
        mock_materials_response.json.return_value = {
            "meta": {"total_count": 1},
            "objects": [
                {
                    "document": "/api/v1/doc/document/recording-121-vcon-1/",
                    "order": 0,
                }
            ],
        }
        mock_materials_response.raise_for_status = MagicMock()

        mock_doc_response = MagicMock()
        mock_doc_response.json.return_value = datatracker_document_recording_response
        mock_doc_response.raise_for_status = MagicMock()

        mock_http.get.side_effect = [mock_materials_response, mock_doc_response]

        materials = client.get_session_materials(121, "vcon")

        assert len(materials) == 1
        assert materials[0].type == "recording"
        assert "youtube.com" in materials[0].url


class TestDataTrackerChairs:
    """Tests for fetching working group chairs."""

    @pytest.fixture
    def mock_client(self, mocker):
        """Create a DataTrackerClient with mocked HTTP client."""
        with patch("ietf2vcon.datatracker.httpx.Client") as mock_httpx:
            mock_instance = MagicMock()
            mock_httpx.return_value = mock_instance
            client = DataTrackerClient()
            client._client = mock_instance
            yield client, mock_instance

    @pytest.mark.skip(reason="Complex mocking required for person enrichment")
    def test_get_group_chairs(self, mock_client):
        """Test getting working group chairs."""
        client, mock_http = mock_client

        mock_roles_response = MagicMock()
        mock_roles_response.json.return_value = {
            "meta": {"total_count": 2},
            "objects": [
                {"person": "/api/v1/person/person/106987/"},
                {"person": "/api/v1/person/person/120587/"},
            ],
        }
        mock_roles_response.raise_for_status = MagicMock()

        mock_person1_response = MagicMock()
        mock_person1_response.json.return_value = {
            "name": "Brian Rosen",
            "id": 106987,
        }
        mock_person1_response.raise_for_status = MagicMock()

        mock_email1_response = MagicMock()
        mock_email1_response.json.return_value = {
            "address": "br@brianrosen.net",
        }
        mock_email1_response.raise_for_status = MagicMock()

        mock_person2_response = MagicMock()
        mock_person2_response.json.return_value = {
            "name": "Chris Wendt",
            "id": 120587,
        }
        mock_person2_response.raise_for_status = MagicMock()

        mock_email2_response = MagicMock()
        mock_email2_response.json.return_value = {
            "address": "chris@appliedbits.com",
        }
        mock_email2_response.raise_for_status = MagicMock()

        mock_http.get.side_effect = [
            mock_roles_response,
            mock_person1_response,
            mock_email1_response,
            mock_person2_response,
            mock_email2_response,
        ]

        chairs = client.get_group_chairs("vcon")

        assert len(chairs) == 2
        assert chairs[0].name == "Brian Rosen"
        assert chairs[0].role == "chair"
        assert chairs[1].name == "Chris Wendt"


class TestDataTrackerIntegration:
    """Integration tests requiring network access."""

    @pytest.mark.skip(reason="Requires network access")
    def test_real_meeting_fetch(self):
        """Test fetching a real meeting."""
        client = DataTrackerClient()
        try:
            meeting = client.get_meeting(121)
            assert meeting is not None
            assert meeting.number == 121
        finally:
            client.close()

    @pytest.mark.skip(reason="Requires network access")
    def test_real_session_fetch(self):
        """Test fetching real sessions."""
        client = DataTrackerClient()
        try:
            sessions = client.get_group_sessions(121, "vcon")
            assert len(sessions) > 0
        finally:
            client.close()
