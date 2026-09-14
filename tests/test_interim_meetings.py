"""Interim meetings are identified by name, not number.

The Datatracker's meeting `number` field holds both forms: 124 for a numbered
meeting, interim-2026-vcon-02 for an interim. Everything downstream has to
carry a string without coercing it or prefixing it as though it were a number.
"""

from click.testing import CliRunner

from ietf2vcon.cli import _meeting_number
from ietf2vcon.converter import IETFSessionConverter
from ietf2vcon.models import IETFMeeting, IETFSession
from ietf2vcon.vcon_builder import VConBuilder
from ietf2vcon.youtube import YouTubeResolver

INTERIM = "interim-2026-vcon-02"


class TestMeetingNumberOption:
    """The CLI accepts both forms and preserves the numeric type."""

    def test_digits_normalize_to_int(self):
        # A numbered meeting must stay an int so the meeting number written
        # into the vCon keeps the type it has always had.
        assert _meeting_number(None, None, "124") == 124
        assert isinstance(_meeting_number(None, None, "124"), int)

    def test_interim_name_passes_through(self):
        assert _meeting_number(None, None, INTERIM) == INTERIM

    def test_none_passes_through(self):
        assert _meeting_number(None, None, None) is None


class TestModelsAcceptInterim:
    """Pydantic must not reject a named meeting."""

    def test_meeting_accepts_interim_name(self):
        assert IETFMeeting(number=INTERIM).number == INTERIM

    def test_meeting_keeps_int(self):
        assert IETFMeeting(number=124).number == 124

    def test_session_accepts_interim_name(self):
        s = IETFSession(
            meeting_number=INTERIM, group_acronym="vcon", session_id="35825"
        )
        assert s.meeting_number == INTERIM


class TestFilenames:
    """An interim identifier already names the meeting."""

    def test_numbered_meeting_keeps_ietf_prefix(self):
        c = IETFSessionConverter()
        assert c._meeting_slug(124) == "ietf124"
        assert (
            c._session_filename(124, "vcon", "33406")
            == "ietf124_vcon_33406.vcon.json"
        )

    def test_interim_is_not_double_prefixed(self):
        c = IETFSessionConverter()
        assert c._meeting_slug(INTERIM) == INTERIM
        assert "ietfinterim" not in c._session_filename(INTERIM, "vcon", "35825")
        assert (
            c._session_filename(INTERIM, "vcon", "35825")
            == f"{INTERIM}_vcon_35825.vcon.json"
        )


class TestSubject:
    """An interim is labelled as one rather than read as a meeting number."""

    def _subject(self, number):
        session = IETFSession(
            meeting_number=number, group_acronym="vcon", session_id="1"
        )
        builder = VConBuilder()
        builder.set_meeting_metadata(IETFMeeting(number=number), session)
        return builder.vcon.vcon_dict["subject"]

    def test_numbered_meeting_subject_unchanged(self):
        assert self._subject(124) == "IETF 124 - VCON Working Group Session"

    def test_interim_subject_says_interim(self):
        subject = self._subject(INTERIM)
        assert "Interim" in subject
        assert INTERIM in subject
        assert not subject.startswith(f"IETF {INTERIM}")


class TestYouTubeSearchSkipped:
    """Interims are not in the per-meeting playlists, so do not go looking."""

    def test_non_numeric_meeting_returns_none_without_subprocess(self, monkeypatch):
        def explode(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("yt-dlp must not run for an interim")

        monkeypatch.setattr("ietf2vcon.youtube.subprocess.run", explode)
        assert (
            YouTubeResolver().search_session_video(INTERIM, "vcon", "2026-09-09")
            is None
        )
