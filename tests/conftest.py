"""Pytest configuration and fixtures for ietf2vcon tests."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Path to test fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to the fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture
def youtube_captions_json(fixtures_dir: Path) -> dict:
    """Load sample YouTube captions JSON3 file."""
    with open(fixtures_dir / "youtube_captions.json3") as f:
        return json.load(f)


@pytest.fixture
def youtube_captions_path(fixtures_dir: Path) -> Path:
    """Return path to sample YouTube captions file."""
    return fixtures_dir / "youtube_captions.json3"


@pytest.fixture
def datatracker_meeting_response(fixtures_dir: Path) -> dict:
    """Load sample Datatracker meeting API response."""
    with open(fixtures_dir / "datatracker_meeting.json") as f:
        return json.load(f)


@pytest.fixture
def datatracker_sessions_response(fixtures_dir: Path) -> dict:
    """Load sample Datatracker sessions API response."""
    with open(fixtures_dir / "datatracker_sessions.json") as f:
        return json.load(f)


@pytest.fixture
def datatracker_materials_response(fixtures_dir: Path) -> dict:
    """Load sample Datatracker materials API response."""
    with open(fixtures_dir / "datatracker_materials.json") as f:
        return json.load(f)


@pytest.fixture
def datatracker_document_agenda_response(fixtures_dir: Path) -> dict:
    """Load sample Datatracker agenda document response."""
    with open(fixtures_dir / "datatracker_document_agenda.json") as f:
        return json.load(f)


@pytest.fixture
def datatracker_document_recording_response(fixtures_dir: Path) -> dict:
    """Load sample Datatracker recording document response."""
    with open(fixtures_dir / "datatracker_document_recording.json") as f:
        return json.load(f)


@pytest.fixture
def sample_ietf_meeting():
    """Create a sample IETFMeeting object."""
    from ietf2vcon.models import IETFMeeting

    return IETFMeeting(
        number=121,
        city="Dublin",
        country="IE",
        start_date=datetime(2024, 11, 2),
        end_date=datetime(2024, 11, 8),
        time_zone="Europe/Dublin",
    )


@pytest.fixture
def sample_ietf_session():
    """Create a sample IETFSession object."""
    from ietf2vcon.models import IETFSession

    return IETFSession(
        meeting_number=121,
        group_acronym="vcon",
        session_id="33406",
        name="VCON Working Group",
        start_time=datetime(2024, 11, 7, 15, 30),
        duration_seconds=3600,
        room="Liffey Hall 2",
    )


@pytest.fixture
def sample_transcript_segments():
    """Create sample transcript segments."""
    from ietf2vcon.models import TranscriptSegment

    return [
        TranscriptSegment(id=0, start=0.0, end=5.0, text="Welcome to the session."),
        TranscriptSegment(id=1, start=5.0, end=9.5, text="Today we discuss vCon."),
        TranscriptSegment(id=2, start=9.5, end=12.5, text="Let's begin.", confidence=0.95),
    ]


@pytest.fixture
def sample_transcription_result(sample_transcript_segments):
    """Create a sample TranscriptionResult."""
    from ietf2vcon.transcription import TranscriptionResult

    return TranscriptionResult(
        text="Welcome to the session. Today we discuss vCon. Let's begin.",
        segments=sample_transcript_segments,
        language="en",
        duration=12.5,
        provider="youtube",
        model="auto-generated",
    )


@pytest.fixture
def sample_video_metadata():
    """Create sample VideoMetadata."""
    from ietf2vcon.youtube import VideoMetadata

    return VideoMetadata(
        video_id="DfNKgMvbn1o",
        title="IETF 121 - VCON Working Group Session",
        url="https://www.youtube.com/watch?v=DfNKgMvbn1o",
        duration_seconds=3600,
        upload_date="20241108",
    )


@pytest.fixture
def sample_chat_messages():
    """Create sample chat messages."""
    from ietf2vcon.models import ChatMessage

    return [
        ChatMessage(
            timestamp=datetime(2024, 11, 7, 15, 35),
            sender="Alice",
            content="Great presentation!",
            sender_email="alice@example.com",
            stream="vcon",
        ),
        ChatMessage(
            timestamp=datetime(2024, 11, 7, 15, 40),
            sender="Bob",
            content="I have a question about the draft.",
            sender_email="bob@example.com",
            stream="vcon",
        ),
    ]


@pytest.fixture
def sample_materials():
    """Create sample IETF materials."""
    from ietf2vcon.models import IETFMaterial

    return [
        IETFMaterial(
            type="agenda",
            title="Agenda for IETF 121 VCON",
            url="https://datatracker.ietf.org/meeting/121/materials/agenda-121-vcon-00",
            filename="agenda-121-vcon-00.md",
            mimetype="text/markdown",
            order=0,
        ),
        IETFMaterial(
            type="slides",
            title="VCON Chair Slides",
            url="https://datatracker.ietf.org/meeting/121/materials/slides-121-vcon-chair-slides-00.pdf",
            filename="slides-121-vcon-chair-slides-00.pdf",
            mimetype="application/pdf",
            order=1,
        ),
        IETFMaterial(
            type="recording",
            title="Video Recording",
            url="https://www.youtube.com/watch?v=DfNKgMvbn1o",
            order=2,
        ),
    ]


@pytest.fixture
def mock_httpx_client(mocker):
    """Create a mock httpx client."""
    mock_client = MagicMock()
    mocker.patch("httpx.Client", return_value=mock_client)
    return mock_client


@pytest.fixture
def temp_output_dir(tmp_path: Path) -> Path:
    """Create a temporary output directory."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return output_dir
