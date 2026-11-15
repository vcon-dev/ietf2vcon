"""A published file is replaced only by a complete, verified result.

Anything else is an error for that session and leaves the published file
untouched: interrupted writes, unreadable previous files, lost identity or
carried content, and failed reads.
"""

import json
import os
from datetime import datetime, timezone

import httpx
import pytest
from click.testing import CliRunner

from ietf2vcon import converter as converter_module
from ietf2vcon.cli import main
from ietf2vcon.converter import (
    ConversionOptions,
    ConversionResult,
    IETFSessionConverter,
    atomic_write_text,
    finalize_vcon,
    remove_stale_partials,
)
from ietf2vcon.errors import CarryOverError, DatatrackerError, MaterialFetchError
from ietf2vcon.materials import MaterialsDownloader
from ietf2vcon.models import IETFMaterial, IETFMeeting, IETFSession
from ietf2vcon.vcon_builder import VConBuilder

UUID = "019d3273-65c9-8df3-9dd8-dd37220d739c"
CREATED = "2026-03-16T03:30:00+00:00"
RECORDING = {
    "type": "recording", "start": "2026-07-22T12:00:00+00:00", "duration": 5400,
    "parties": [0, 1], "mediatype": "video/mp4", "url": "https://youtu.be/abc",
}
TRANSCRIPT = {
    "type": "wtf_transcription", "dialog": 0, "vendor": "whisper",
    "url": "https://example.net/t.wtf.json", "content_hash": "sha512-abc",
    "mediatype": "application/json",
}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def options(tmp_path, **kwargs):
    return ConversionOptions(
        output_dir=tmp_path, include_video=False, include_transcript=False,
        include_chat=False, **kwargs,
    )


def previous_file(tmp_path, name="ietf125_6lo_35225.vcon.json", **overrides):
    data = {
        "vcon": "0.5.0", "uuid": UUID, "created_at": CREATED, "updated_at": CREATED,
        "parties": [{"name": "a"}, {"name": "b"}], "dialog": [dict(RECORDING)],
        "attachments": [], "analysis": [dict(TRANSCRIPT)], "sessions": [{"dialog": [0]}],
        **overrides,
    }
    path = tmp_path / name
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def new_result(**extra):
    builder = VConBuilder()
    builder.add_party("a")
    vcon = builder.build()
    vcon.vcon_dict.update(extra)
    return ConversionResult(
        vcon=vcon, meeting_number=125, group_acronym="6lo", session_id="35225"
    )


def tree(path):
    return sorted(p.name for p in path.iterdir())


class TestAtomicWrite:
    def test_a_crash_between_the_temporary_write_and_the_replace_leaves_the_original(
        self, tmp_path, monkeypatch
    ):
        target = previous_file(tmp_path)
        before = target.read_bytes()

        def crash(src, dst):
            raise OSError("killed")

        monkeypatch.setattr(os, "replace", crash)
        with pytest.raises(OSError):
            atomic_write_text(target, "{ new content")

        assert target.read_bytes() == before
        assert tree(tmp_path) == [target.name]  # no temporary file left behind

    def test_the_temporary_file_is_hidden_and_never_looks_like_a_vcon(self, tmp_path, monkeypatch):
        seen = []
        real_replace = os.replace
        monkeypatch.setattr(os, "replace", lambda s, d: (seen.append(os.path.basename(s)), real_replace(s, d)))
        target = tmp_path / "ietf125_6lo_35225.vcon.json"
        atomic_write_text(target, "{}\n")

        [name] = seen
        assert name.startswith(".") and name.endswith(".partial")
        assert "ietf125_6lo_35225.vcon.json" in name
        assert not name.endswith(".vcon.json")
        assert tree(tmp_path) == [target.name]

    def test_stale_temporary_files_from_a_killed_run_are_removed_and_logged(
        self, tmp_path, caplog
    ):
        target = previous_file(tmp_path)
        stale = tmp_path / ".ietf125_6lo_35225.vcon.json.4242.partial"
        stale.write_text("{ truncated")
        with caplog.at_level("WARNING", logger="ietf2vcon.converter"):
            removed = remove_stale_partials(tmp_path)

        assert [p.name for p in removed] == [stale.name]
        assert tree(tmp_path) == [target.name]
        assert any(stale.name in r.getMessage() for r in caplog.records)

    def test_a_stale_temporary_file_is_not_a_published_file(self, tmp_path):
        (tmp_path / ".ietf125_6lo_35225.vcon.json.4242.partial").write_text("{")
        converter = IETFSessionConverter(options(tmp_path))
        assert converter.previous_session_ids(125, "6lo") == []

    def test_a_good_write_replaces_the_target_completely(self, tmp_path):
        target = previous_file(tmp_path)
        atomic_write_text(target, '{"new": true}\n')
        assert json.loads(target.read_text()) == {"new": True}
        assert tree(tmp_path) == [target.name]


class TestUnreadablePreviousFile:
    def _carry(self, tmp_path, **opts):
        converter = IETFSessionConverter(options(tmp_path, **opts))
        vcon = VConBuilder().build()
        converter._carry_over_previous(vcon, 125, "6lo", "35225", [])
        return vcon

    def test_a_truncated_previous_file_fails_and_is_left_unchanged(self, tmp_path):
        target = previous_file(tmp_path)
        half = target.read_bytes()[: target.stat().st_size // 2]
        target.write_bytes(half)

        with pytest.raises(CarryOverError, match="ietf125_6lo_35225.vcon.json exists but cannot be read"):
            self._carry(tmp_path)
        assert target.read_bytes() == half
        assert tree(tmp_path) == [target.name]

    def test_a_previous_file_that_is_not_an_object_fails(self, tmp_path):
        target = tmp_path / "ietf125_6lo_35225.vcon.json"
        target.write_text("[1, 2]")
        with pytest.raises(CarryOverError, match="not a vCon object"):
            self._carry(tmp_path)

    def test_a_previous_file_with_no_uuid_fails(self, tmp_path):
        target = tmp_path / "ietf125_6lo_35225.vcon.json"
        target.write_text(json.dumps({"vcon": "0.5.0", "dialog": []}))
        with pytest.raises(CarryOverError, match="no uuid"):
            self._carry(tmp_path)

    def test_a_failed_migration_fails_instead_of_keeping_only_the_uuid(
        self, tmp_path, monkeypatch
    ):
        target = previous_file(tmp_path, vcon="0.4.0")
        before = target.read_bytes()

        def broken(_):
            raise ValueError("cannot migrate")

        monkeypatch.setattr("vcon.migration.migrate_0_4_0_to_0_5_0", broken)
        with pytest.raises(CarryOverError, match="cannot be migrated to 0.5.0"):
            self._carry(tmp_path)
        assert target.read_bytes() == before

    def test_convert_session_reports_it_as_an_error_and_saves_nothing(
        self, tmp_path, monkeypatch
    ):
        target = previous_file(tmp_path)
        target.write_bytes(target.read_bytes()[:100])
        truncated = target.read_bytes()

        class OneSession:
            def get_meeting(self, number):
                return IETFMeeting(number=number)

            def get_group_sessions(self, meeting_number, group_acronym):
                return [IETFSession(
                    meeting_number=meeting_number, group_acronym=group_acronym,
                    session_id="35225", start_time=utc(2026, 7, 22, 12), status="sched",
                )]

            def get_group_chairs(self, *a, **k):
                return []

            def get_session_materials(self, *a, **k):
                return []

            def close(self):
                pass

        monkeypatch.setattr(converter_module, "DataTrackerClient", OneSession)
        converter = IETFSessionConverter(options(tmp_path))
        result = converter.convert_session(125, "6lo", session_id="35225")

        assert len(result.errors) == 1
        assert "6lo session 35225 at IETF 125" in result.errors[0]
        assert "ietf125_6lo_35225.vcon.json" in result.errors[0]
        with pytest.raises(CarryOverError, match="not saving"):
            converter.save_vcon(result)
        assert target.read_bytes() == truncated
        assert tree(tmp_path) == [target.name]

    def test_save_vcon_refuses_to_overwrite_an_unreadable_file_too(self, tmp_path):
        target = tmp_path / "ietf125_6lo_35225.vcon.json"
        target.write_text("{ broken")
        converter = IETFSessionConverter(options(tmp_path))
        with pytest.raises(CarryOverError, match="cannot be read"):
            converter.save_vcon(new_result())
        assert target.read_text() == "{ broken"


class TestIdentityAndContentGuards:
    def test_a_new_uuid_must_not_replace_a_published_file(self, tmp_path):
        target = previous_file(tmp_path)
        before = target.read_bytes()
        result = new_result()  # a fresh uuid
        with pytest.raises(CarryOverError, match="uuid would change"):
            IETFSessionConverter(options(tmp_path)).save_vcon(result)
        assert target.read_bytes() == before and tree(tmp_path) == [target.name]

    def test_a_changed_created_at_must_not_replace_a_published_file(self, tmp_path):
        target = previous_file(tmp_path)
        result = new_result(uuid=UUID, dialog=[dict(RECORDING)], analysis=[dict(TRANSCRIPT)])
        with pytest.raises(CarryOverError, match="created_at would change"):
            IETFSessionConverter(options(tmp_path)).save_vcon(result)

    def test_losing_the_recording_dialog_must_not_replace_a_published_file(self, tmp_path):
        target = previous_file(tmp_path)
        before = target.read_bytes()
        result = new_result(uuid=UUID, created_at=CREATED, analysis=[dict(TRANSCRIPT)])
        with pytest.raises(CarryOverError, match="recording Dialog and the new document has none"):
            IETFSessionConverter(options(tmp_path)).save_vcon(result)
        assert target.read_bytes() == before

    def test_fewer_analysis_entries_must_not_replace_a_published_file(self, tmp_path):
        target = previous_file(tmp_path, analysis=[dict(TRANSCRIPT), dict(TRANSCRIPT, type="summary")])
        result = new_result(uuid=UUID, created_at=CREATED, dialog=[dict(RECORDING)],
                            analysis=[dict(TRANSCRIPT)])
        with pytest.raises(CarryOverError, match="2 analysis entries and the new document has 1"):
            IETFSessionConverter(options(tmp_path)).save_vcon(result)

    def test_a_document_that_kept_everything_replaces_the_file(self, tmp_path):
        target = previous_file(tmp_path)
        result = new_result(uuid=UUID, created_at=CREATED, dialog=[dict(RECORDING)],
                            analysis=[dict(TRANSCRIPT)])
        IETFSessionConverter(options(tmp_path)).save_vcon(result)
        data = json.loads(target.read_text())
        assert data["uuid"] == UUID and data["created_at"] == CREATED
        assert tree(tmp_path) == [target.name]

    def test_the_guard_is_applied_at_the_end_of_carry_over_too(self, tmp_path):
        """A 0.4.0 file whose dialog cannot be carried (carry-over runs, none survives)."""
        previous_file(tmp_path, dialog=[dict(RECORDING)], analysis=[dict(TRANSCRIPT, dialog=5)])
        converter = IETFSessionConverter(options(tmp_path))
        with pytest.raises(CarryOverError, match="analysis entries"):
            converter._carry_over_previous(VConBuilder().build(), 125, "6lo", "35225", [])

    def test_a_result_with_errors_is_never_saved(self, tmp_path):
        result = new_result()
        result.errors.append("something failed")
        with pytest.raises(CarryOverError, match="not saving"):
            IETFSessionConverter(options(tmp_path)).save_vcon(result)
        assert tree(tmp_path) == []

    def test_choosing_a_new_uuid_on_purpose_is_still_possible(self, tmp_path):
        target = previous_file(tmp_path)
        converter = IETFSessionConverter(options(tmp_path, preserve_uuid=False, preserve_analysis=False))
        converter.save_vcon(new_result())
        assert json.loads(target.read_text())["uuid"] != UUID


class FakeDataTracker:
    """A datatracker that can fail one named read."""

    fail = None

    def get_meeting(self, number):
        self._maybe("meeting")
        return IETFMeeting(number=number, start_date=datetime(2026, 7, 18))

    def get_group_sessions(self, meeting_number, group_acronym):
        self._maybe("sessions")
        return [IETFSession(
            meeting_number=meeting_number, group_acronym=group_acronym, session_id="35225",
            start_time=utc(2026, 7, 22, 12), duration_seconds=5400, status="sched",
        )]

    def get_group_chairs(self, group_acronym, session_date=None):
        self._maybe("chairs")
        return []

    def get_session_materials(self, *a, **k):
        self._maybe("materials")
        return []

    def _maybe(self, what):
        if self.fail == what:
            raise DatatrackerError(f"datatracker down while reading {what}")

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    FakeDataTracker.fail = None
    monkeypatch.setattr(converter_module, "DataTrackerClient", FakeDataTracker)
    return FakeDataTracker


class TestFailedReadsFailTheSession:
    @pytest.mark.parametrize("what", ["meeting", "sessions", "chairs", "materials"])
    def test_a_failed_read_is_an_error_naming_what_failed_and_saves_nothing(
        self, fake, tmp_path, what
    ):
        fake.fail = what
        converter = IETFSessionConverter(ConversionOptions(
            output_dir=tmp_path, include_video=False, include_transcript=False,
            include_chat=False,
        ))
        result = converter.convert_session(125, "6lo")

        assert len(result.errors) == 1
        assert f"while reading {what}" in result.errors[0]
        assert "6lo" in result.errors[0] and "IETF 125" in result.errors[0]
        with pytest.raises(CarryOverError):
            converter.save_vcon(result)
        assert tree(tmp_path) == []

    def test_the_session_id_is_named_once_it_is_known(self, fake, tmp_path):
        fake.fail = "chairs"
        converter = IETFSessionConverter(ConversionOptions(
            output_dir=tmp_path, include_video=False, include_transcript=False, include_chat=False,
        ))
        result = converter.convert_session(125, "6lo")
        assert "6lo session 35225 at IETF 125" in result.errors[0]

    def test_a_missing_meeting_record_is_an_error(self, monkeypatch, tmp_path):
        class NoMeeting(FakeDataTracker):
            def get_meeting(self, number):
                return None

        monkeypatch.setattr(converter_module, "DataTrackerClient", NoMeeting)
        result = IETFSessionConverter(options(tmp_path)).convert_session(999, "6lo")
        assert result.errors and "no meeting 999" in result.errors[0]

    def test_a_published_file_survives_a_failed_read_untouched(self, fake, tmp_path):
        target = previous_file(tmp_path)
        before = target.read_bytes()
        fake.fail = "materials"
        runner = CliRunner().invoke(main, [
            "convert-all", "--meeting", "125", "--groups", "6lo", "--no-transcript", "--no-video",
            "--output-dir", str(tmp_path), "--parallel", "1",
        ])
        assert runner.exit_code == 1
        output = " ".join(runner.output.split())
        assert "1 session(s) failed" in output
        assert "while reading materials" in output
        assert target.read_bytes() == before and tree(tmp_path) == [target.name]

    def test_a_passing_session_in_the_same_run_is_still_written(self, fake, tmp_path, monkeypatch):
        class Selective(FakeDataTracker):
            def get_session_materials(self, meeting, group):
                if group == "bad":
                    raise DatatrackerError("down")
                return []

            def get_group_sessions(self, meeting_number, group_acronym):
                return [IETFSession(
                    meeting_number=meeting_number, group_acronym=group_acronym,
                    session_id="7" if group_acronym == "bad" else "8",
                    start_time=utc(2026, 7, 22, 12), status="sched",
                )]

        monkeypatch.setattr(converter_module, "DataTrackerClient", Selective)
        runner = CliRunner().invoke(main, [
            "convert-all", "--meeting", "125", "--groups", "bad", "--groups", "good",
            "--no-transcript", "--no-video", "--output-dir", str(tmp_path), "--parallel", "1",
        ])
        assert runner.exit_code == 1
        assert tree(tmp_path) == ["ietf125_good_8.vcon.json"]


class TestMaterialFetch:
    MATERIAL = IETFMaterial(
        type="slides", title="t", url="https://datatracker.ietf.org/meeting/126/materials/s",
        file_url="https://www.ietf.org/proceedings/126/slides/s-00.pdf", filename="s-00.pdf",
        mimetype="application/pdf",
    )

    def _fetch(self, tmp_path, monkeypatch, responses):
        from ietf2vcon import httpclient

        monkeypatch.setattr(httpclient.time, "sleep", lambda s: None)
        queue = list(responses)

        def handler(request):
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        downloader = MaterialsDownloader(download_dir=tmp_path / "m")
        downloader.client = httpx.Client(transport=httpx.MockTransport(handler))
        return downloader

    @pytest.mark.parametrize("status", [403, 404, 410])
    def test_a_definitive_not_available_keeps_the_body_reference_behaviour(
        self, tmp_path, monkeypatch, status
    ):
        downloader = self._fetch(tmp_path, monkeypatch, [httpx.Response(status)])
        assert downloader.fetch_material(self.MATERIAL) == (None, None, None)

    def test_a_not_available_material_still_becomes_a_body_reference(self, tmp_path, monkeypatch):
        downloader = self._fetch(tmp_path, monkeypatch, [httpx.Response(404)])
        builder = VConBuilder()
        builder.add_materials([self.MATERIAL], downloader=downloader)
        att = builder.vcon.vcon_dict["attachments"][-1]
        assert att["encoding"] == "json" and "url" not in att

    @pytest.mark.parametrize("failure", [
        httpx.ReadTimeout("slow"), httpx.ConnectError("refused"),
    ])
    def test_a_timeout_or_connection_error_after_retries_is_a_session_error(
        self, tmp_path, monkeypatch, failure
    ):
        downloader = self._fetch(tmp_path, monkeypatch, [failure] * 5)
        with pytest.raises(MaterialFetchError, match="could not fetch"):
            downloader.fetch_material(self.MATERIAL)

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_429_and_5xx_after_retries_are_a_session_error(self, tmp_path, monkeypatch, status):
        downloader = self._fetch(tmp_path, monkeypatch, [httpx.Response(status)] * 5)
        with pytest.raises(MaterialFetchError, match=f"HTTP {status}"):
            downloader.fetch_material(self.MATERIAL)

    def test_a_transient_failure_that_recovers_is_not_an_error(self, tmp_path, monkeypatch):
        downloader = self._fetch(tmp_path, monkeypatch, [
            httpx.Response(503), httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}),
        ])
        content, mediatype, _ = downloader.fetch_material(self.MATERIAL)
        assert content == b"%PDF" and mediatype == "application/pdf"

    def test_a_transient_material_failure_fails_the_session_and_saves_nothing(
        self, tmp_path, monkeypatch
    ):
        from ietf2vcon import httpclient

        monkeypatch.setattr(httpclient.time, "sleep", lambda s: None)

        class WithSlides(FakeDataTracker):
            def get_session_materials(self, *a, **k):
                return [TestMaterialFetch.MATERIAL]

        monkeypatch.setattr(converter_module, "DataTrackerClient", WithSlides)

        def always_500(request):
            return httpx.Response(500)

        real_init = MaterialsDownloader.__init__

        def init(self, *a, **k):
            real_init(self, *a, **k)
            self.client = httpx.Client(transport=httpx.MockTransport(always_500))

        monkeypatch.setattr(MaterialsDownloader, "__init__", init)
        converter = IETFSessionConverter(ConversionOptions(
            output_dir=tmp_path, include_video=False, include_transcript=False, include_chat=False,
        ))
        result = converter.convert_session(125, "6lo")
        assert result.errors and "could not fetch" in result.errors[0]
        assert tree(tmp_path) == []

    def test_mirror_and_http_counts_are_kept(self, tmp_path, monkeypatch):
        mirror = tmp_path / "mirror" / "proceedings" / "126" / "slides"
        mirror.mkdir(parents=True)
        (mirror / "s-00.pdf").write_bytes(b"%PDF")
        downloader = MaterialsDownloader(download_dir=tmp_path / "m", mirror_dir=tmp_path / "mirror")
        material = IETFMaterial(
            type="slides", title="t", filename="s-00.pdf", uploaded_filename="s-00.pdf",
            url="https://datatracker.ietf.org/meeting/126/materials/s",
        )
        downloader.fetch_material(material)
        assert (MaterialsDownloader.mirror_served, MaterialsDownloader.http_fetched) == (1, 0)

        MaterialsDownloader.reset_counters()
        plain = self._fetch(tmp_path, monkeypatch, [httpx.Response(200, content=b"x")])
        plain.fetch_material(self.MATERIAL)
        assert (MaterialsDownloader.mirror_served, MaterialsDownloader.http_fetched) == (0, 1)


class TestFinalShape:
    def test_encoding_is_dropped_wherever_there_is_no_body(self):
        doc = {
            "dialog": [
                {"type": "recording", "url": "https://x/r", "encoding": "none"},
                {"type": "text", "body": "hi", "encoding": "none"},
            ],
            "attachments": [
                {"purpose": "slides", "url": "https://x/s.pdf", "encoding": "none"},
                {"purpose": "meeting_metadata", "body": "{}", "encoding": "json"},
                {"purpose": "empty", "body": "", "encoding": "none"},
            ],
            "analysis": [{"type": "wtf_transcription", "url": "https://x/t", "encoding": "none"}],
        }
        finalize_vcon(doc)

        assert "encoding" not in doc["dialog"][0] and doc["dialog"][1]["encoding"] == "none"
        assert "encoding" not in doc["attachments"][0]
        assert doc["attachments"][1]["encoding"] == "json" and doc["attachments"][2]["encoding"] == "none"
        assert "encoding" not in doc["analysis"][0]

    def test_the_library_default_encoding_is_removed_from_an_external_reference(self):
        builder = VConBuilder()
        builder.add_party("a")
        builder.add_material_attachment(TestMaterialFetch.MATERIAL, content=b"%PDF")
        vcon = builder.build()
        external = vcon.vcon_dict["attachments"][-1]
        assert external.get("encoding") == "none"  # the library's default, before finalising
        finalize_vcon(vcon.vcon_dict)
        assert "encoding" not in external and external["url"]

    def test_all_five_arrays_are_always_present(self):
        for start in ({}, {"parties": None, "dialog": None}, {"analysis": "x"}):
            doc = finalize_vcon(dict(start))
            for key in ("parties", "dialog", "attachments", "analysis", "sessions"):
                assert doc[key] == [] or isinstance(doc[key], list)

    def test_a_written_file_has_the_arrays_and_no_stray_encoding(self, tmp_path):
        result = new_result()
        result.vcon.vcon_dict.pop("dialog")
        result.vcon.vcon_dict["attachments"].append(
            {"purpose": "x", "url": "https://x/y", "encoding": "none", "party": 0, "session": 0}
        )
        path = IETFSessionConverter(options(tmp_path)).save_vcon(result)
        data = json.loads(path.read_text())
        assert data["dialog"] == [] and data["analysis"] == [] and data["sessions"]
        assert all("encoding" not in a for a in data["attachments"] if "body" not in a)


class TestMeetingWideFailureStopsTheRun:
    """The first failure of a read every session depends on ends the meeting's run."""

    GROUPS = ["a", "b", "c", "d", "e"]

    def _install(self, monkeypatch, failing_group, error):
        attempts = []

        class Counting(FakeDataTracker):
            def get_group_sessions(self, meeting_number, group_acronym):
                attempts.append(group_acronym)
                if group_acronym == failing_group:
                    raise error
                return [IETFSession(
                    meeting_number=meeting_number, group_acronym=group_acronym,
                    session_id=str(self.GROUPS_IDS[group_acronym]),
                    start_time=utc(2026, 7, 22, 12), status="sched",
                )]

            GROUPS_IDS = {g: n + 1 for n, g in enumerate(TestMeetingWideFailureStopsTheRun.GROUPS)}

        monkeypatch.setattr(converter_module, "DataTrackerClient", Counting)
        return attempts

    def _run(self, tmp_path, *extra):
        args = ["convert-all", "--meeting", "125", "--no-transcript", "--no-video",
                "--output-dir", str(tmp_path), "--parallel", "1", *extra]
        for g in self.GROUPS:
            args += ["--groups", g]
        return CliRunner().invoke(main, args)

    def _published(self, tmp_path, group, session_id):
        path = tmp_path / f"ietf125_{group}_{session_id}.vcon.json"
        path.write_text(json.dumps({"vcon": "0.4.0", "uuid": UUID, "created_at": CREATED}))
        return path

    def test_a_meeting_wide_failure_on_the_third_session_stops_the_run(self, monkeypatch, tmp_path):
        from ietf2vcon.errors import MeetingReadError

        attempts = self._install(
            monkeypatch, "c", MeetingReadError("IETF 125: could not read the official schedule: down")
        )
        first = self._published(tmp_path, "a", 1)
        second = self._published(tmp_path, "b", 2)
        fourth = self._published(tmp_path, "d", 4)
        fifth = self._published(tmp_path, "e", 5)
        d_before, e_before = fourth.read_bytes(), fifth.read_bytes()
        result = self._run(tmp_path)

        output = " ".join(result.output.split())
        assert result.exit_code == 1
        assert attempts == ["a", "b", "c"]  # d and e were never tried
        assert json.loads(first.read_text())["vcon"] == "0.5.0"   # earlier successes stay saved
        assert json.loads(second.read_text())["vcon"] == "0.5.0"
        assert fourth.read_bytes() == d_before and fifth.read_bytes() == e_before
        assert "Run stopped at the first meeting-wide read failure" in output
        assert "could not read the official schedule" in output
        assert "2 session(s) not attempted" in output
        assert "1 session(s) failed" in output

    def test_a_per_session_failure_does_not_stop_the_run(self, monkeypatch, tmp_path):
        attempts = self._install(monkeypatch, "c", DatatrackerError("materials of c: down"))
        result = self._run(tmp_path)

        assert result.exit_code == 1
        assert attempts == ["a", "b", "c", "d", "e"]
        assert sorted(p.name for p in tmp_path.glob("*.json")) == [
            "ietf125_a_1.vcon.json", "ietf125_b_2.vcon.json",
            "ietf125_d_4.vcon.json", "ietf125_e_5.vcon.json",
        ]
        assert "not attempted" not in " ".join(result.output.split())

    def test_sessions_of_a_group_with_several_files_are_counted_as_not_attempted(
        self, monkeypatch, tmp_path
    ):
        from ietf2vcon.errors import MeetingReadError

        class Many(FakeDataTracker):
            def get_group_sessions(self, meeting_number, group_acronym):
                raise MeetingReadError("IETF 125: could not read session statuses: down")

        monkeypatch.setattr(converter_module, "DataTrackerClient", Many)
        for sid in (7, 8, 9):
            self._published(tmp_path, "a", sid)
        self._published(tmp_path, "b", 10)
        result = CliRunner().invoke(main, [
            "convert-all", "--meeting", "125", "--groups", "a", "--groups", "b",
            "--no-transcript", "--no-video", "--output-dir", str(tmp_path), "--parallel", "1",
        ])
        output = " ".join(result.output.split())
        assert result.exit_code == 1
        assert "3 session(s) not attempted" in output  # a's other two files and b's

    @pytest.mark.parametrize("what", ["record", "schedule", "statuses"])
    def test_the_client_marks_meeting_wide_reads(self, monkeypatch, what):
        from ietf2vcon.datatracker import DataTrackerClient
        from ietf2vcon.errors import MeetingReadError

        from .test_official_schedule import FakeAPI

        fake = FakeAPI()
        fake.fail = {
            "record": {"/api/v1/meeting/meeting/"},
            "schedule": {"/api/v1/meeting/timeslot/"},
            "statuses": {"/api/v1/meeting/schedulingevent/"},
        }[what]
        monkeypatch.setattr(
            DataTrackerClient, "_get", lambda self, path, params=None: fake.get(path, params)
        )
        with pytest.raises(MeetingReadError):
            DataTrackerClient().get_group_sessions("126", "vcon")

    def test_a_failed_group_session_query_is_not_meeting_wide(self, monkeypatch):
        from ietf2vcon.datatracker import DataTrackerClient
        from ietf2vcon.errors import MeetingReadError

        from .test_official_schedule import FakeAPI

        fake = FakeAPI()
        fake.fail = {"/api/v1/meeting/session/"}
        monkeypatch.setattr(
            DataTrackerClient, "_get", lambda self, path, params=None: fake.get(path, params)
        )
        with pytest.raises(DatatrackerError) as caught:
            DataTrackerClient().get_group_sessions("126", "vcon")
        assert not isinstance(caught.value, MeetingReadError)


class TestNotAvailableCount:
    """A 403 can mean the client is blocked, so every downgrade is counted by status."""

    def _fetch_all(self, tmp_path, monkeypatch, statuses):
        from ietf2vcon import httpclient

        monkeypatch.setattr(httpclient.time, "sleep", lambda s: None)
        queue = list(statuses)
        downloader = MaterialsDownloader(download_dir=tmp_path / "m")
        downloader.client = httpx.Client(
            transport=httpx.MockTransport(lambda request: queue.pop(0))
        )
        for _ in statuses:
            downloader.fetch_material(TestMaterialFetch.MATERIAL)

    def test_downgrades_are_counted_by_status_code(self, tmp_path, monkeypatch):
        self._fetch_all(tmp_path, monkeypatch, [
            httpx.Response(404), httpx.Response(404), httpx.Response(403), httpx.Response(410),
        ])
        assert MaterialsDownloader.not_available == {404: 2, 403: 1, 410: 1}
        assert MaterialsDownloader.not_available_summary() == (
            "Materials not available: 4 (403: 1, 404: 2, 410: 1)"
        )

    def test_the_summary_with_nothing_unavailable(self):
        assert MaterialsDownloader.not_available_summary() == "Materials not available: 0"

    def test_successes_and_other_statuses_are_not_counted(self, tmp_path, monkeypatch):
        self._fetch_all(tmp_path, monkeypatch, [httpx.Response(200, content=b"x")])
        assert MaterialsDownloader.not_available == {}
        with pytest.raises(MaterialFetchError):
            self._fetch_all(tmp_path, monkeypatch, [httpx.Response(401)])
        assert MaterialsDownloader.not_available == {}

    def test_each_downgrade_is_still_logged_as_a_warning(self, tmp_path, monkeypatch, caplog):
        with caplog.at_level("WARNING", logger="ietf2vcon.materials"):
            self._fetch_all(tmp_path, monkeypatch, [httpx.Response(403)])
        assert any("HTTP 403" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)

    def test_the_convert_all_summary_has_the_line(self, tmp_path, monkeypatch):
        from ietf2vcon import httpclient

        monkeypatch.setattr(httpclient.time, "sleep", lambda s: None)

        class WithSlides(FakeDataTracker):
            def get_session_materials(self, *a, **k):
                return [TestMaterialFetch.MATERIAL]

        monkeypatch.setattr(converter_module, "DataTrackerClient", WithSlides)
        real_init = MaterialsDownloader.__init__

        def init(self, *a, **k):
            real_init(self, *a, **k)
            self.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))

        monkeypatch.setattr(MaterialsDownloader, "__init__", init)
        result = CliRunner().invoke(main, [
            "convert-all", "--meeting", "125", "--groups", "6lo", "--no-transcript", "--no-video",
            "--output-dir", str(tmp_path), "--parallel", "1",
        ])
        output = " ".join(result.output.split())
        assert result.exit_code == 0, output
        assert "Materials not available: 1 (404: 1)" in output
