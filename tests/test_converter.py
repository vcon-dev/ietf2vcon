"""Integration tests for ietf2vcon.converter module."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ietf2vcon.converter import ConversionOptions, ConversionResult, IETFSessionConverter


class TestConversionOptions:
    """Tests for ConversionOptions dataclass."""

    def test_default_options(self):
        """Test default conversion options."""
        options = ConversionOptions()

        assert options.include_video is True
        assert options.video_source == "youtube"
        assert options.download_video is False
        assert options.include_materials is True
        assert options.include_transcript is True
        assert options.transcription_source == "auto"
        assert options.include_chat is True

    def test_custom_options(self, tmp_path):
        """Test custom conversion options."""
        options = ConversionOptions(
            include_video=False,
            include_transcript=False,
            output_dir=tmp_path,
            whisper_model="large",
        )

        assert options.include_video is False
        assert options.include_transcript is False
        assert options.output_dir == tmp_path
        assert options.whisper_model == "large"


class TestConversionResult:
    """Tests for ConversionResult dataclass."""

    def test_result_creation(self):
        """Test creating a ConversionResult."""
        result = ConversionResult(
            vcon=MagicMock(),
            meeting_number=121,
            group_acronym="vcon",
            session_id="33406",
        )

        assert result.meeting_number == 121
        assert result.group_acronym == "vcon"

    def test_result_with_errors(self):
        """Test ConversionResult with errors."""
        result = ConversionResult(
            vcon=MagicMock(),
            meeting_number=121,
            group_acronym="vcon",
            session_id="33406",
            errors=["Error 1", "Error 2"],
        )

        assert len(result.errors) == 2


class TestIETFSessionConverter:
    """Tests for IETFSessionConverter."""

    @pytest.fixture
    def converter(self, tmp_path):
        """Create a converter with temporary output directory."""
        options = ConversionOptions(
            output_dir=tmp_path,
            include_video=False,
            include_transcript=False,
            include_chat=False,
        )
        return IETFSessionConverter(options)

    @pytest.fixture
    def mock_datatracker(self, mocker, sample_ietf_meeting, sample_ietf_session):
        """Mock DataTrackerClient."""
        mock = mocker.patch("ietf2vcon.converter.DataTrackerClient")
        instance = mock.return_value
        instance.get_meeting.return_value = sample_ietf_meeting
        instance.get_group_sessions.return_value = [sample_ietf_session]
        instance.get_group_chairs.return_value = []
        instance.get_session_materials.return_value = []
        instance.close = MagicMock()
        return instance

    def test_converter_initialization(self, converter):
        """Test converter initialization."""
        assert converter.options is not None
        assert converter.options.output_dir.exists()

    def test_convert_session_minimal(
        self, converter, mock_datatracker, sample_ietf_meeting, sample_ietf_session
    ):
        """Test minimal session conversion (no video, transcript, or chat)."""
        result = converter.convert_session(121, "vcon")

        assert result is not None
        assert result.meeting_number == 121
        assert result.group_acronym == "vcon"
        assert result.vcon is not None

    def test_convert_session_creates_vcon(
        self, converter, mock_datatracker, sample_ietf_meeting, sample_ietf_session
    ):
        """Test that conversion creates valid vCon structure."""
        result = converter.convert_session(121, "vcon")
        vcon = result.vcon

        # Check basic structure
        assert vcon.uuid is not None
        assert "IETF 121" in vcon.subject
        assert "VCON" in vcon.subject

    def test_convert_session_adds_note_well(
        self, converter, mock_datatracker, sample_ietf_meeting, sample_ietf_session
    ):
        """Test that conversion adds IETF Note Well."""
        result = converter.convert_session(121, "vcon")
        vcon = result.vcon

        # Find lawful_basis attachment
        lb_att = next(
            (a for a in vcon.attachments if a.type == "lawful_basis"), None
        )
        assert lb_att is not None
        assert lb_att.body["terms_of_service_name"] == "IETF Note Well"

    def test_convert_session_adds_ingress_info(
        self, converter, mock_datatracker, sample_ietf_meeting, sample_ietf_session
    ):
        """Test that conversion adds ingress info."""
        result = converter.convert_session(121, "vcon")
        vcon = result.vcon

        ing_att = next(
            (a for a in vcon.attachments if a.type == "ingress_info"), None
        )
        assert ing_att is not None
        assert ing_att.body["source"] == "ietf2vcon"
        assert ing_att.body["meeting_number"] == 121

    def test_convert_session_with_materials(
        self,
        converter,
        mock_datatracker,
        sample_ietf_meeting,
        sample_ietf_session,
        sample_materials,
    ):
        """Test conversion with materials."""
        mock_datatracker.get_session_materials.return_value = sample_materials

        result = converter.convert_session(121, "vcon")
        # Recording is extracted for video URL, so only 2 materials counted
        assert result.materials_count >= 2

    @pytest.mark.skip(reason="Requires more complex mocking")
    def test_convert_session_session_not_found(self, converter, mock_datatracker):
        """Test conversion when session not found."""
        mock_datatracker.get_group_sessions.return_value = []

        result = converter.convert_session(121, "vcon")

        assert "No sessions found" in result.errors[0]

    def test_save_vcon(self, converter, mock_datatracker, tmp_path):
        """Test saving vCon to file."""
        result = converter.convert_session(121, "vcon")
        output_path = converter.save_vcon(result)

        assert output_path.exists()
        assert output_path.suffix == ".json"

        # Verify JSON is valid
        with open(output_path) as f:
            data = json.load(f)
        assert "uuid" in data
        assert "vcon" in data

    def test_save_vcon_custom_path(self, converter, mock_datatracker, tmp_path):
        """Test saving vCon to custom path."""
        result = converter.convert_session(121, "vcon")
        custom_path = tmp_path / "custom_name.vcon.json"
        output_path = converter.save_vcon(result, custom_path)

        assert output_path == custom_path
        assert output_path.exists()


class TestConverterWithTranscript:
    """Tests for converter with transcript functionality."""

    @pytest.fixture
    def converter_with_transcript(self, tmp_path):
        """Create a converter with transcript enabled."""
        options = ConversionOptions(
            output_dir=tmp_path,
            include_video=False,
            include_transcript=True,
            transcription_source="youtube",
            include_chat=False,
        )
        return IETFSessionConverter(options)

    @pytest.fixture
    def mock_all_services(
        self, mocker, sample_ietf_meeting, sample_ietf_session, sample_materials
    ):
        """Mock all external services."""
        # Mock DataTracker
        mock_dt = mocker.patch("ietf2vcon.converter.DataTrackerClient")
        dt_instance = mock_dt.return_value
        dt_instance.get_meeting.return_value = sample_ietf_meeting
        dt_instance.get_group_sessions.return_value = [sample_ietf_session]
        dt_instance.get_group_chairs.return_value = []
        dt_instance.get_session_materials.return_value = sample_materials
        dt_instance.close = MagicMock()

        # Mock YouTube resolver
        mock_yt = mocker.patch("ietf2vcon.converter.YouTubeResolver")
        yt_instance = mock_yt.return_value
        yt_instance.search_session_video.return_value = None
        yt_instance.download_captions.return_value = None

        return dt_instance, yt_instance

    @pytest.mark.skip(reason="Requires complex mocking of YouTubeResolver")
    def test_converter_attempts_youtube_captions(
        self, converter_with_transcript, mock_all_services, sample_materials
    ):
        """Test that converter attempts to get YouTube captions."""
        dt_instance, yt_instance = mock_all_services

        # Add recording URL to materials
        sample_materials[2].url = "https://www.youtube.com/watch?v=DfNKgMvbn1o"
        dt_instance.get_session_materials.return_value = sample_materials

        result = converter_with_transcript.convert_session(121, "vcon")

        # Should have attempted to download captions
        assert yt_instance.download_captions.called or result.warnings


class TestConverterEndToEnd:
    """End-to-end tests (require network)."""

    @pytest.mark.skip(reason="Requires network access and yt-dlp")
    def test_full_conversion(self, tmp_path):
        """Test full conversion of a real session."""
        options = ConversionOptions(
            output_dir=tmp_path,
            include_video=True,
            include_transcript=True,
            include_chat=False,
        )
        converter = IETFSessionConverter(options)
        result = converter.convert_session(121, "vcon")

        assert result.vcon is not None
        assert result.video_url is not None
        assert result.has_transcript is True

        # Save and verify
        output_path = converter.save_vcon(result)
        assert output_path.exists()

        with open(output_path) as f:
            data = json.load(f)

        assert len(data.get("dialog", [])) > 0
        assert len(data.get("analysis", [])) > 0
