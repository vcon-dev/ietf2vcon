"""convert-all regenerates a group's published session, whichever sorts first."""

import json
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from ietf2vcon import converter as converter_module
from ietf2vcon.cli import main
from ietf2vcon.converter import ConversionOptions, IETFSessionConverter
from ietf2vcon.models import IETFMeeting, IETFSession


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def session(session_id, start=None, status="sched", group="dnsop"):
    return IETFSession(
        meeting_number=100, group_acronym=group, session_id=str(session_id),
        start_time=start, status=status,
    )


class FakeDataTracker:
    """Answers with a fixed list of sessions per group, in the order given."""

    sessions = {}

    def get_meeting(self, number):
        return IETFMeeting(number=number, start_date=datetime(2017, 11, 12))

    def get_group_sessions(self, meeting_number, group_acronym):
        return list(self.sessions.get(group_acronym, []))

    def get_group_chairs(self, group_acronym, session_date=None):
        return []

    def get_session_materials(self, *a, **k):
        return []

    def get_recording_url(self, *a, **k):
        return None

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    FakeDataTracker.sessions = {}
    monkeypatch.setattr(converter_module, "DataTrackerClient", FakeDataTracker)
    return FakeDataTracker


def published(tmp_path, group, session_id, uuid):
    path = tmp_path / f"ietf100_{group}_{session_id}.vcon.json"
    path.write_text(json.dumps({"vcon": "0.5.0", "uuid": uuid, "created_at": "2018-01-01T00:00:00+00:00"}))
    return path


def options(tmp_path):
    return ConversionOptions(
        output_dir=tmp_path, include_video=False, include_transcript=False, include_chat=False,
    )


class TestChooseSession:
    def test_the_earliest_session_that_met_wins_over_a_lower_id(self):
        sessions = [
            session(1, utc(2017, 11, 16, 7)), session(2, utc(2017, 11, 13, 1)),
            session(3, utc(2017, 11, 14, 1)),
        ]
        assert IETFSessionConverter.choose_session(sessions).session_id == "2"

    def test_a_session_that_did_not_meet_is_passed_over(self):
        sessions = [session(1, None, "canceled"), session(2, utc(2017, 11, 16, 7))]
        assert IETFSessionConverter.choose_session(sessions).session_id == "2"

    def test_equal_starts_and_ids_compare_as_integers(self):
        sessions = [session(1000, utc(2017, 11, 13)), session(999, utc(2017, 11, 13))]
        assert IETFSessionConverter.choose_session(sessions).session_id == "999"

    def test_an_unknown_status_counts_as_met(self):
        sessions = [session(5, None, "canceled"), session(6, utc(2017, 11, 14), None)]
        assert IETFSessionConverter.choose_session(sessions).session_id == "6"

    def test_when_none_met_the_lowest_id(self):
        sessions = [session(30, None, "canceled"), session(20, None, "notmeet")]
        assert IETFSessionConverter.choose_session(sessions).session_id == "20"

    def test_met_sessions_without_a_start_follow_those_with_one(self):
        sessions = [session(1, None), session(9, utc(2017, 11, 20))]
        assert IETFSessionConverter.choose_session(sessions).session_id == "9"


class TestConvertSession:
    def test_a_published_id_selects_that_session_even_when_another_sorts_first(
        self, fake, tmp_path
    ):
        fake.sessions = {"dnsop": [
            session(25891, utc(2017, 11, 13, 1, 30)),
            session(25892, None, "canceled"),
        ]}
        result = IETFSessionConverter(options(tmp_path)).convert_session(
            100, "dnsop", session_id="25892"
        )
        assert result.session_id == "25892" and not result.published_session_missing

    def test_a_missing_published_id_converts_nothing_and_warns(self, fake, tmp_path, caplog):
        fake.sessions = {"dnsop": [session(25891, utc(2017, 11, 13, 1, 30))]}
        with caplog.at_level("WARNING", logger="ietf2vcon.converter"):
            result = IETFSessionConverter(options(tmp_path)).convert_session(
                100, "dnsop", session_id="12345"
            )
        assert result.published_session_missing is True and not result.errors
        warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("ietf100_dnsop_12345.vcon.json" in m for m in warned)

    def test_with_no_previous_file_the_first_session_that_met_is_converted(self, fake, tmp_path):
        fake.sessions = {"dnsop": [
            session(25891, utc(2017, 11, 13, 1, 30)), session(25892, None, "canceled"),
        ]}
        result = IETFSessionConverter(options(tmp_path)).convert_session(100, "dnsop")
        assert result.session_id == "25891"

    def test_an_explicit_index_still_means_position_in_id_order(self, fake, tmp_path):
        fake.sessions = {"dnsop": [session(25892, None, "canceled"), session(25891, utc(2017, 11, 13))]}
        result = IETFSessionConverter(options(tmp_path)).convert_session(100, "dnsop", 1)
        assert result.session_id == "25892"

    def test_previous_session_ids_only_match_this_meeting_and_group(self, tmp_path):
        for name in (
            "ietf100_dnsop_25891.vcon.json", "ietf100_dnsop_25892.vcon.json",
            "ietf100_dnsop2_1.vcon.json", "ietf99_dnsop_7.vcon.json", "ietf100_dnsop_x.vcon.json",
            "ietf100_dnsop_25893.vcon.json.bak",
        ):
            (tmp_path / name).write_text("{}")
        found = IETFSessionConverter(options(tmp_path)).previous_session_ids(100, "dnsop")
        assert found == ["25891", "25892"]


class TestConvertAll:
    def _run(self, tmp_path, *groups):
        args = ["convert-all", "--meeting", "100", "--no-transcript", "--no-video",
                "--output-dir", str(tmp_path), "--parallel", "1"]
        for g in groups:
            args += ["--groups", g]
        return CliRunner().invoke(main, args)

    def test_the_published_file_is_regenerated_in_place_with_its_uuid(self, fake, tmp_path):
        fake.sessions = {"dnsop": [
            session(25892, None, "canceled"),   # sorts and is listed first
            session(25891, utc(2017, 11, 13, 1, 30)),
        ]}
        path = published(tmp_path, "dnsop", 25891, "019d3273-65c9-8df3-9dd8-dd37220d739c")
        result = self._run(tmp_path, "dnsop")

        assert result.exit_code == 0, result.output
        assert sorted(p.name for p in tmp_path.glob("*.json")) == [path.name]
        data = json.loads(path.read_text())
        assert data["uuid"] == "019d3273-65c9-8df3-9dd8-dd37220d739c"
        assert data["sessions"][0]["start"] == "2017-11-13T01:30:00+00:00"

    def test_two_previous_files_for_one_group_are_both_regenerated(self, fake, tmp_path):
        fake.sessions = {"nmrg": [
            session(25976, utc(2017, 11, 14, 1, 30), group="nmrg"),
            session(26027, utc(2017, 11, 13, 9, 40), group="nmrg"),
        ]}
        a = published(tmp_path, "nmrg", 25976, "019d3273-65c9-8df3-9dd8-dd37220d7391")
        b = published(tmp_path, "nmrg", 26027, "019d3273-65c9-8df3-9dd8-dd37220d7392")
        result = self._run(tmp_path, "nmrg")

        assert result.exit_code == 0, result.output
        assert sorted(p.name for p in tmp_path.glob("*.json")) == sorted([a.name, b.name])
        assert json.loads(a.read_text())["uuid"].endswith("7391")
        assert json.loads(b.read_text())["uuid"].endswith("7392")
        assert json.loads(a.read_text())["sessions"][0]["start"].startswith("2017-11-14")
        assert json.loads(b.read_text())["sessions"][0]["start"].startswith("2017-11-13")

    def test_a_missing_published_session_leaves_the_file_alone_and_is_counted(
        self, fake, tmp_path
    ):
        fake.sessions = {"dnsop": [session(25891, utc(2017, 11, 13, 1, 30))]}
        path = published(tmp_path, "dnsop", 11111, "019d3273-65c9-8df3-9dd8-dd37220d739c")
        before = path.read_bytes()
        result = self._run(tmp_path, "dnsop")

        assert path.read_bytes() == before
        assert sorted(p.name for p in tmp_path.glob("*.json")) == [path.name]
        assert "Published session not found for 1 file" in flat(result)
        assert "dnsop" in flat(result)

    def test_a_new_meeting_group_gets_the_first_session_that_met(self, fake, tmp_path):
        fake.sessions = {"dnsop": [
            session(25892, None, "canceled"), session(25891, utc(2017, 11, 13, 1, 30)),
        ]}
        result = self._run(tmp_path, "dnsop")

        assert result.exit_code == 0, result.output
        assert [p.name for p in tmp_path.glob("*.json")] == ["ietf100_dnsop_25891.vcon.json"]


class ListingDataTracker(FakeDataTracker):
    """FakeDataTracker that also lists the meeting's sessions."""

    listed = []
    listing_error = None

    def get_meeting_sessions(self, meeting_number):
        if self.listing_error:
            raise self.listing_error
        return list(self.listed)


@pytest.fixture
def listing(monkeypatch):
    import ietf2vcon.datatracker as datatracker_module

    ListingDataTracker.sessions = {}
    ListingDataTracker.listed = []
    ListingDataTracker.listing_error = None
    monkeypatch.setattr(converter_module, "DataTrackerClient", ListingDataTracker)
    monkeypatch.setattr(datatracker_module, "DataTrackerClient", ListingDataTracker)
    return ListingDataTracker


def flat(result):
    """Output with the console's line wrapping undone."""
    return " ".join(result.output.split())


def run_all(tmp_path, *extra):
    return CliRunner().invoke(main, [
        "convert-all", "--meeting", "100", "--no-transcript", "--no-video",
        "--output-dir", str(tmp_path), "--parallel", "1", *extra,
    ])


class TestWorkList:
    def test_a_failed_session_listing_is_an_error_not_zero_of_zero(self, listing, tmp_path):
        from ietf2vcon.errors import DatatrackerError

        listing.listing_error = DatatrackerError("GET /api/v1/meeting/session/ failed: HTTP 500")
        result = run_all(tmp_path)
        assert result.exit_code == 1
        assert "could not list the sessions of IETF 100" in flat(result)
        assert "HTTP 500" in flat(result)
        assert list(tmp_path.glob("*.json")) == []

    def test_an_empty_session_listing_is_an_error(self, listing, tmp_path):
        result = run_all(tmp_path)
        assert result.exit_code == 1
        assert "lists no sessions for IETF 100" in flat(result)

    def test_a_published_group_the_datatracker_no_longer_lists_is_reported_and_untouched(
        self, listing, tmp_path
    ):
        listing.listed = [session(25891, utc(2017, 11, 13, 1, 30))]
        listing.sessions = {"dnsop": [session(25891, utc(2017, 11, 13, 1, 30))]}
        gone = published(tmp_path, "gone", 111, "019d3273-65c9-8df3-9dd8-dd37220d7399")
        before = gone.read_bytes()
        result = run_all(tmp_path)

        assert result.exit_code == 0, result.output
        assert gone.read_bytes() == before
        assert "Published session not found for 1 file(s)" in flat(result) and "gone" in flat(result)
        assert (tmp_path / "ietf100_dnsop_25891.vcon.json").exists()  # a listed group is created

    def test_the_work_list_is_the_union_of_listed_groups_and_published_files(
        self, listing, tmp_path
    ):
        listing.listed = [session(1, utc(2017, 11, 13), group="a"), session(2, utc(2017, 11, 13), group="b")]
        listing.sessions = {
            "a": [session(1, utc(2017, 11, 13), group="a")],
            "b": [session(2, utc(2017, 11, 13), group="b")],
        }
        published(tmp_path, "c", 3, "019d3273-65c9-8df3-9dd8-dd37220d7393")
        result = run_all(tmp_path)

        assert result.exit_code == 0, result.output
        assert sorted(p.name for p in tmp_path.glob("*.json")) == [
            "ietf100_a_1.vcon.json", "ietf100_b_2.vcon.json", "ietf100_c_3.vcon.json",
        ]
        assert "3/3 successful" in flat(result)

    def test_existing_only_creates_no_new_file_and_lists_what_it_skipped(self, listing, tmp_path):
        listing.listed = [session(1, utc(2017, 11, 13), group="a"), session(2, utc(2017, 11, 13), group="b")]
        listing.sessions = {
            "a": [session(1, utc(2017, 11, 13), group="a")],
            "b": [session(2, utc(2017, 11, 13), group="b")],
        }
        existing = published(tmp_path, "a", 1, "019d3273-65c9-8df3-9dd8-dd37220d7391")
        result = run_all(tmp_path, "--existing-only")

        assert result.exit_code == 0, result.output
        assert sorted(p.name for p in tmp_path.glob("*.json")) == [existing.name]
        assert json.loads(existing.read_text())["uuid"].endswith("7391")  # regenerated in place
        assert json.loads(existing.read_text())["vcon"] == "0.5.0"
        assert "No published file for 1 group(s)" in flat(result) and "b" in flat(result)

    def test_existing_only_with_nothing_to_regenerate_is_an_error(self, listing, tmp_path):
        listing.listed = [session(1, utc(2017, 11, 13), group="a")]
        listing.sessions = {"a": [session(1, utc(2017, 11, 13), group="a")]}
        result = run_all(tmp_path, "--existing-only")
        assert result.exit_code == 1
        assert "found no published file to regenerate" in flat(result)
        assert list(tmp_path.glob("*.json")) == []

    def test_stale_temporary_files_are_removed_when_a_run_starts(self, listing, tmp_path):
        listing.listed = [session(1, utc(2017, 11, 13), group="a")]
        listing.sessions = {"a": [session(1, utc(2017, 11, 13), group="a")]}
        stale = tmp_path / ".ietf100_a_1.vcon.json.99.partial"
        stale.write_text("{ cut off")
        run_all(tmp_path)
        assert not stale.exists()

    def test_published_files_are_found_for_interim_names(self, tmp_path):
        from ietf2vcon.cli import published_files

        (tmp_path / "interim-2026-vcon-02_vcon_5009.vcon.json").write_text("{}")
        (tmp_path / "ietf100_dnsop_1.vcon.json").write_text("{}")
        assert published_files(tmp_path, "interim-2026-vcon-02") == {"vcon": ["5009"]}


class TestMirrorCheck:
    def test_a_mirror_root_that_does_not_exist_is_an_error(self, listing, tmp_path):
        result = run_all(tmp_path, "--rsync-mirror", str(tmp_path / "missing"))
        assert result.exit_code == 1
        assert "does not exist or is not a directory" in flat(result)
        assert list(tmp_path.glob("*.json")) == []

    def test_a_mirror_without_the_meetings_proceedings_is_an_error(self, listing, tmp_path):
        mirror = tmp_path / "mirror"
        (mirror / "proceedings" / "99").mkdir(parents=True)
        result = run_all(tmp_path / "out", "--rsync-mirror", str(mirror))
        assert result.exit_code == 1
        assert "no proceedings for IETF 100" in flat(result)

    def test_a_mirror_root_that_is_a_file_is_an_error(self, listing, tmp_path):
        mirror = tmp_path / "mirror"
        mirror.write_text("not a directory")
        result = run_all(tmp_path / "out", "--rsync-mirror", str(mirror))
        assert result.exit_code == 1

    def test_interims_are_exempt_from_the_proceedings_check(self):
        from ietf2vcon.cli import check_mirror

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as root:
            assert check_mirror(Path(root), "interim-2026-vcon-02") is None
            assert "no proceedings for IETF 100" in check_mirror(Path(root), 100)
            assert check_mirror(None, 100) is None

    def test_a_good_mirror_runs_and_the_counts_are_reported(self, listing, tmp_path):
        mirror = tmp_path / "mirror"
        (mirror / "proceedings" / "100").mkdir(parents=True)
        listing.listed = [session(1, utc(2017, 11, 13), group="a")]
        listing.sessions = {"a": [session(1, utc(2017, 11, 13), group="a")]}
        result = run_all(tmp_path / "out", "--rsync-mirror", str(mirror))
        assert result.exit_code == 0, result.output
        assert "Materials: 0 from the mirror, 0 over HTTP" in flat(result)

    def test_the_single_session_command_checks_the_mirror_too(self, tmp_path):
        result = CliRunner().invoke(main, [
            "convert", "--meeting", "100", "--group", "dnsop", "--output-dir", str(tmp_path),
            "--rsync-mirror", str(tmp_path / "missing"),
        ])
        assert result.exit_code == 1 and "does not exist or is not a directory" in flat(result)


def test_max_rps_sets_the_shared_throttle(listing, tmp_path):
    from ietf2vcon.httpclient import THROTTLE

    listing.listed = [session(1, utc(2017, 11, 13), group="a")]
    listing.sessions = {"a": [session(1, utc(2017, 11, 13), group="a")]}
    run_all(tmp_path, "--max-rps", "4")
    assert THROTTLE.min_interval == pytest.approx(0.25)
