"""Unit tests for ietf2vcon.transcription module."""

from datetime import datetime
from pathlib import Path

import pytest

from ietf2vcon.models import TranscriptSegment
from ietf2vcon.transcription import (
    MeetechoTranscriptLoader,
    TranscriptionResult,
    YouTubeCaptionLoader,
    _seconds_to_srt_time,
    _seconds_to_webvtt_time,
    transcript_to_srt,
    transcript_to_webvtt,
    transcription_to_vcon_analysis,
)


class TestTranscriptionResult:
    """Tests for TranscriptionResult dataclass."""

    def test_transcription_result_creation(self, sample_transcript_segments):
        """Test creating a TranscriptionResult."""
        result = TranscriptionResult(
            text="Test transcript",
            segments=sample_transcript_segments,
            language="en",
            provider="test",
        )
        assert result.text == "Test transcript"
        assert result.language == "en"
        assert len(result.segments) == 3

    def test_transcription_result_defaults(self, sample_transcript_segments):
        """Test TranscriptionResult default values."""
        result = TranscriptionResult(
            text="Test",
            segments=sample_transcript_segments,
        )
        assert result.language is None
        assert result.duration is None
        assert result.provider == "unknown"
        assert result.model is None


class TestYouTubeCaptionLoader:
    """Tests for YouTubeCaptionLoader."""

    def test_load_captions_success(self, youtube_captions_path):
        """Test loading YouTube captions successfully."""
        loader = YouTubeCaptionLoader()
        result = loader.load_captions(youtube_captions_path)

        assert result is not None
        assert result.provider == "youtube"
        assert result.model == "auto-generated"
        assert len(result.segments) == 5

    def test_load_captions_segment_content(self, youtube_captions_path):
        """Test that caption segments have correct content."""
        loader = YouTubeCaptionLoader()
        result = loader.load_captions(youtube_captions_path)

        # First segment
        assert result.segments[0].start == 0.0
        assert result.segments[0].end == 5.0
        assert "Welcome" in result.segments[0].text

    def test_load_captions_timing(self, youtube_captions_path):
        """Test caption timing conversion from milliseconds."""
        loader = YouTubeCaptionLoader()
        result = loader.load_captions(youtube_captions_path)

        # Second segment starts at 5000ms = 5.0s
        assert result.segments[1].start == 5.0
        # Third segment starts at 9500ms = 9.5s
        assert result.segments[2].start == 9.5

    def test_load_captions_full_text(self, youtube_captions_path):
        """Test that full text is concatenated."""
        loader = YouTubeCaptionLoader()
        result = loader.load_captions(youtube_captions_path)

        assert "Welcome" in result.text
        assert "agenda" in result.text.lower()

    def test_load_captions_nonexistent_file(self):
        """Test loading from nonexistent file returns None."""
        loader = YouTubeCaptionLoader()
        result = loader.load_captions(Path("/nonexistent/file.json3"))
        assert result is None

    def test_load_captions_duration(self, youtube_captions_path):
        """Test that duration is calculated from last segment."""
        loader = YouTubeCaptionLoader()
        result = loader.load_captions(youtube_captions_path)

        # Last segment ends at 16500 + 3500 = 20000ms = 20.0s
        assert result.duration == 20.0


class TestMeetechoTranscriptLoader:
    """Tests for MeetechoTranscriptLoader."""

    def test_parse_timestamp_hhmmss(self):
        """Test parsing HH:MM:SS timestamp."""
        loader = MeetechoTranscriptLoader()
        assert loader._parse_timestamp("01:30:45") == 5445.0

    def test_parse_timestamp_mmss(self):
        """Test parsing MM:SS timestamp."""
        loader = MeetechoTranscriptLoader()
        assert loader._parse_timestamp("05:30") == 330.0

    def test_parse_timestamp_seconds(self):
        """Test parsing raw seconds."""
        loader = MeetechoTranscriptLoader()
        assert loader._parse_timestamp("123.5") == 123.5

    def test_parse_timestamp_invalid(self):
        """Test parsing invalid timestamp returns 0."""
        loader = MeetechoTranscriptLoader()
        assert loader._parse_timestamp("invalid") == 0.0


class TestTranscriptionToVconAnalysis:
    """Tests for transcription_to_vcon_analysis function."""

    def test_basic_conversion(self, sample_transcription_result):
        """Test basic conversion to VConAnalysis."""
        analysis = transcription_to_vcon_analysis(sample_transcription_result)

        assert analysis.type == "wtf_transcription"
        assert analysis.dialog == 0
        assert analysis.vendor == "youtube"
        assert analysis.spec == "draft-howe-wtf-transcription-00"

    def test_wtf_body_structure(self, sample_transcription_result):
        """Test WTF body has correct structure."""
        analysis = transcription_to_vcon_analysis(sample_transcription_result)
        body = analysis.body

        assert "transcript" in body
        assert "segments" in body
        assert "metadata" in body

    def test_wtf_transcript_section(self, sample_transcription_result):
        """Test WTF transcript section content."""
        analysis = transcription_to_vcon_analysis(sample_transcription_result)
        transcript = analysis.body["transcript"]

        assert transcript["text"] == sample_transcription_result.text
        assert transcript["language"] == "en"
        assert transcript["duration"] == 12.5

    def test_wtf_segments_section(self, sample_transcription_result):
        """Test WTF segments section content."""
        analysis = transcription_to_vcon_analysis(sample_transcription_result)
        segments = analysis.body["segments"]

        assert len(segments) == 3
        assert segments[0]["id"] == 0
        assert segments[0]["start"] == 0.0
        assert segments[0]["end"] == 5.0

    def test_wtf_confidence_included(self, sample_transcription_result):
        """Test that confidence is included when present."""
        analysis = transcription_to_vcon_analysis(sample_transcription_result)
        segments = analysis.body["segments"]

        # Third segment has confidence
        assert "confidence" in segments[2]
        assert segments[2]["confidence"] == 0.95

    def test_wtf_confidence_omitted(self, sample_transcription_result):
        """Test that confidence is omitted when not present."""
        analysis = transcription_to_vcon_analysis(sample_transcription_result)
        segments = analysis.body["segments"]

        # First segment has no confidence
        assert "confidence" not in segments[0]

    def test_wtf_metadata_section(self, sample_transcription_result):
        """Test WTF metadata section content."""
        analysis = transcription_to_vcon_analysis(sample_transcription_result)
        metadata = analysis.body["metadata"]

        assert metadata["provider"] == "youtube"
        assert metadata["model"] == "auto-generated"
        assert metadata["segment_count"] == 3
        assert "created_at" in metadata

    def test_custom_dialog_index(self, sample_transcription_result):
        """Test setting custom dialog index."""
        analysis = transcription_to_vcon_analysis(
            sample_transcription_result, dialog_index=2
        )
        assert analysis.dialog == 2


class TestTranscriptToSRT:
    """Tests for transcript_to_srt function."""

    def test_srt_format(self, sample_transcription_result):
        """Test SRT output format."""
        srt = transcript_to_srt(sample_transcription_result)
        lines = srt.split("\n")

        # First entry
        assert lines[0] == "1"
        assert "-->" in lines[1]
        assert "Welcome" in lines[2]

    def test_srt_timing_format(self, sample_transcription_result):
        """Test SRT timing format (HH:MM:SS,mmm)."""
        srt = transcript_to_srt(sample_transcription_result)

        # Should use comma for milliseconds
        assert "00:00:00,000 --> 00:00:05,000" in srt

    def test_srt_sequential_numbering(self, sample_transcription_result):
        """Test SRT entries are numbered sequentially from 1."""
        srt = transcript_to_srt(sample_transcription_result)
        lines = srt.split("\n")

        # Find all entry numbers
        entry_nums = [lines[i] for i in range(0, len(lines), 4) if lines[i].isdigit()]
        assert entry_nums == ["1", "2", "3"]


class TestTranscriptToWebVTT:
    """Tests for transcript_to_webvtt function."""

    def test_webvtt_header(self, sample_transcription_result):
        """Test WebVTT starts with WEBVTT header."""
        webvtt = transcript_to_webvtt(sample_transcription_result)
        assert webvtt.startswith("WEBVTT")

    def test_webvtt_timing_format(self, sample_transcription_result):
        """Test WebVTT timing format (HH:MM:SS.mmm)."""
        webvtt = transcript_to_webvtt(sample_transcription_result)

        # Should use period for milliseconds
        assert "00:00:00.000 --> 00:00:05.000" in webvtt

    def test_webvtt_content(self, sample_transcription_result):
        """Test WebVTT includes transcript content."""
        webvtt = transcript_to_webvtt(sample_transcription_result)
        assert "Welcome to the session." in webvtt


class TestTimeConversionHelpers:
    """Tests for time conversion helper functions."""

    def test_seconds_to_srt_time_basic(self):
        """Test basic seconds to SRT time conversion."""
        assert _seconds_to_srt_time(0) == "00:00:00,000"
        assert _seconds_to_srt_time(1) == "00:00:01,000"

    def test_seconds_to_srt_time_minutes(self):
        """Test minutes in SRT time."""
        assert _seconds_to_srt_time(65) == "00:01:05,000"
        assert _seconds_to_srt_time(130.5) == "00:02:10,500"

    def test_seconds_to_srt_time_hours(self):
        """Test hours in SRT time."""
        assert _seconds_to_srt_time(3661.123) == "01:01:01,123"

    def test_seconds_to_webvtt_time_basic(self):
        """Test basic seconds to WebVTT time conversion."""
        assert _seconds_to_webvtt_time(0) == "00:00:00.000"
        assert _seconds_to_webvtt_time(1.5) == "00:00:01.500"

    def test_seconds_to_webvtt_time_hours(self):
        """Test hours in WebVTT time."""
        assert _seconds_to_webvtt_time(3723.456) == "01:02:03.456"
