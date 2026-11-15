"""Chairs must be the people who chaired the session, not today's chairs.

`get_group_chairs()` used to return current role holders with no date filter,
so regenerating a 2021 session credited whoever chairs the group in 2026.

The rule under test: a group-history snapshot is a copy of the state an edit is
about to replace, and its `time` is when that state began. A snapshot is in
force until the next one; the newest until the group's own `time`; and from
`group.time` on, the live role table is the truth.

All people and addresses here are invented.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from ietf2vcon.datatracker import DataTrackerClient
from ietf2vcon.errors import DatatrackerError

from .fakeapi import listing

GROUP_ID = 1812
ALEX, SAM, JO, PAT, KIM = "Alex Rivera", "Sam Okafor", "Jo Lindqvist", "Pat Moreau", "Kim Haddad"
PEOPLE = {1: ALEX, 2: SAM, 3: JO, 4: PAT, 5: KIM}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def role(role_id, person, email=None):
    return {
        "id": role_id,
        "person": f"/api/v1/person/person/{person}/",
        "email": f"/api/v1/person/email/{email or f'p{person}@example.net'}/",
    }


class FakeAPI:
    """A tiny Datatracker: one group, its snapshots, role rows and people."""

    def __init__(
        self, group_time="2026-02-16T12:00:00Z", snapshots=None, roles_by_snapshot=None,
        live=None, page_size=1000, fail=(), acronym="6man", group_found=True,
        shuffle=False, ignore_offset=False, fail_snapshot_roles=(),
    ):
        self.group_time = group_time
        self.snapshots = snapshots if snapshots is not None else [
            {"id": 100, "time": "2014-05-01T00:00:00Z"},
            {"id": 200, "time": "2021-02-01T00:00:00Z"},
            {"id": 300, "time": "2025-06-01T00:00:00Z"},
        ]
        self.roles_by_snapshot = roles_by_snapshot if roles_by_snapshot is not None else {
            100: [role(1000, 1)],
            200: [role(2000, 2), role(2001, 3)],
            300: [role(3000, 4)],
        }
        self.live = live if live is not None else [role(9000, 5)]
        self.page_size = page_size
        self.fail = set(fail)
        self.acronym = acronym
        self.group_found = group_found
        self.shuffle = shuffle
        self.ignore_offset = ignore_offset
        self.fail_snapshot_roles = set(fail_snapshot_roles)
        self.calls = []

    def count(self, prefix):
        return sum(1 for path, _ in self.calls if path.startswith(prefix))

    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, dict(params)))
        for failing in self.fail:
            if path.startswith(failing):
                raise DatatrackerError(f"datatracker down: {path}")
        if path == "/api/v1/group/group/":
            objects = [{"id": GROUP_ID, "acronym": self.acronym, "time": self.group_time}]
            return listing(path, objects if self.group_found else [], params)
        if path == "/api/v1/group/grouphistory/":
            assert str(params["group"]) == str(GROUP_ID), "history must be filtered by group id"
            assert "acronym" not in params
            rows = [
                {**snap, "group": f"/api/v1/group/group/{GROUP_ID}/", "acronym": "oldname"}
                for snap in self.snapshots
            ]
            return listing(
                path, rows, params, page_cap=self.page_size,
                shuffle=self.shuffle, ignore_offset=self.ignore_offset,
            )
        if path == "/api/v1/group/rolehistory/":
            if int(params["group"]) in self.fail_snapshot_roles:
                raise DatatrackerError(f"datatracker down: role history {params['group']}")
            return listing(path, self.roles_by_snapshot.get(int(params["group"]), []), params)
        if path == "/api/v1/group/role/":
            if "group__acronym" not in params:
                assert str(params["group"]) == str(GROUP_ID)
            return listing(path, self.live, params)
        if path.startswith("/api/v1/person/person/"):
            return {"name": PEOPLE[int(path.rstrip("/").rsplit("/", 1)[1])]}
        if path.startswith("/api/v1/person/email/"):
            return {"address": path.rstrip("/").rsplit("/", 1)[1]}
        raise AssertionError(f"unexpected path {path}")


@pytest.fixture
def api(monkeypatch):
    def install(**kwargs):
        fake = FakeAPI(**kwargs)
        monkeypatch.setattr(
            DataTrackerClient, "_get", lambda self, path, params=None: fake.get(path, params)
        )
        return fake

    return install


def names(chairs):
    return [p.name for p in chairs]


def chairs_at(api, when, **kwargs):
    DataTrackerClient.clear_chair_cache()  # each call is a fresh fake world
    fake = api(**kwargs)
    return names(DataTrackerClient().get_group_chairs("6man", session_date=when)), fake


class TestWhichStateIsInForce:
    def test_after_group_time_the_live_roles_win_even_with_snapshots(self, api):
        found, fake = chairs_at(api, utc(2026, 3, 20, 9))
        assert found == [KIM]
        assert fake.count("/api/v1/group/rolehistory/") == 0

    def test_between_snapshots_the_earlier_one_is_in_force(self, api):
        found, _ = chairs_at(api, utc(2021, 3, 8, 9))
        assert found == [SAM, JO]

    def test_later_in_the_day_edit_means_snapshot_not_live(self, api):
        """The group was edited at noon; a 09:00 session predates the edit."""
        found, _ = chairs_at(api, utc(2026, 2, 16, 9))
        assert found == [PAT]

    def test_exactly_at_group_time_is_live(self, api):
        found, _ = chairs_at(api, utc(2026, 2, 16, 12, 0, 0))
        assert found == [KIM]
        found, _ = chairs_at(api, utc(2026, 2, 16, 11, 59, 59))
        assert found == [PAT]

    def test_tied_snapshots_of_one_person_take_the_highest_integer_id(self, api):
        """Same person, different addresses: not ambiguous; the highest id's email is used."""
        snapshots = [
            {"id": 999, "time": "2020-01-01T00:00:00Z"},
            {"id": 1000, "time": "2020-01-01T00:00:00Z"},
            {"id": 50, "time": "2020-01-01T00:00:00Z"},
        ]
        roles = {
            999: [role(1, 2, email="from999@example.net")],
            1000: [role(2, 2, email="from1000@example.net")],
            50: [role(3, 2, email="from50@example.net")],
        }
        DataTrackerClient.clear_chair_cache()
        api(snapshots=snapshots, roles_by_snapshot=roles)
        chairs = DataTrackerClient().get_group_chairs("6man", session_date=utc(2021, 1, 1))
        assert names(chairs) == [SAM]
        assert chairs[0].email == "from1000@example.net"  # id 1000, not the string-wise larger "999"

    def test_group_created_after_the_session_has_no_chairs(self, api):
        snapshots = [{"id": 300, "time": "2025-06-01T00:00:00Z"}]
        found, fake = chairs_at(
            api, utc(2020, 1, 1), snapshots=snapshots, group_time="2025-09-01T00:00:00Z"
        )
        assert found == []
        assert fake.count("/api/v1/group/role/") == 0  # no fallback to current chairs

    def test_session_before_the_recorded_history_has_no_chairs(self, api):
        found, fake = chairs_at(api, utc(2006, 7, 10))
        assert found == []
        assert fake.count("/api/v1/group/role/") == 0

    def test_no_snapshots_and_group_edited_after_the_session(self, api):
        found, fake = chairs_at(api, utc(2021, 3, 8), snapshots=[])
        assert found == []
        assert fake.count("/api/v1/group/role/") == 0

    def test_no_snapshots_and_group_edited_before_the_session(self, api):
        found, _ = chairs_at(api, utc(2026, 3, 8), snapshots=[])
        assert found == [KIM]

    def test_snapshot_with_no_chair_rows_gives_no_chairs(self, api):
        found, fake = chairs_at(api, utc(2021, 3, 8), roles_by_snapshot={200: []})
        assert found == []
        assert fake.count("/api/v1/group/role/") == 0


class TestReadFailures:
    """A failed read fails the session; it never turns into today's chairs or a quiet omission."""

    @pytest.mark.parametrize("failing", [
        "/api/v1/group/group/", "/api/v1/group/grouphistory/", "/api/v1/group/rolehistory/",
    ])
    def test_a_failed_dated_read_raises_and_never_asks_for_current_roles(self, api, failing):
        fake = api(fail=[failing])
        client = DataTrackerClient()
        when = utc(2021, 3, 8, 9)
        with pytest.raises(DatatrackerError):
            client._chair_roles_at("6man", when)
        with pytest.raises(DatatrackerError, match="6man"):
            client.get_group_chairs("6man", session_date=when)
        assert fake.count("/api/v1/group/role/") == 0  # current roles never asked for

    def test_a_failed_live_role_read_raises(self, api):
        api(fail=["/api/v1/group/role/"])
        with pytest.raises(DatatrackerError):
            DataTrackerClient().get_group_chairs("6man", session_date=utc(2026, 3, 20))

    def test_a_call_with_no_date_still_returns_current_roles(self, api):
        fake = api(fail=["/api/v1/group/group/"])
        found = names(DataTrackerClient().get_group_chairs("6man"))

        assert found == [KIM]
        assert fake.count("/api/v1/group/role/") == 1

    def test_an_unknown_group_raises_rather_than_asserting_no_chairs(self, api):
        api(group_found=False)
        with pytest.raises(DatatrackerError, match="no group with acronym"):
            DataTrackerClient()._chair_roles_at("nogroup", utc(2021, 1, 1))

    def test_a_chair_whose_person_record_cannot_be_read_fails_the_lookup(self, api):
        fake = api()
        fake.fail = {"/api/v1/person/person/"}
        with pytest.raises(DatatrackerError, match="could not read a chair of 6man"):
            DataTrackerClient().get_group_chairs("6man", session_date=utc(2026, 3, 20))

    def test_a_chair_whose_email_cannot_be_read_fails_the_lookup(self, api):
        fake = api()
        fake.fail = {"/api/v1/person/email/"}
        with pytest.raises(DatatrackerError, match="could not read a chair of 6man"):
            DataTrackerClient().get_group_chairs("6man", session_date=utc(2026, 3, 20))


class TestCutoffs:
    def test_a_date_is_the_end_of_that_day_utc(self, api):
        found, _ = chairs_at(api, date(2026, 2, 16), group_time="2026-02-16T23:59:59Z")
        assert found == [KIM]
        found, _ = chairs_at(api, date(2026, 2, 16), group_time="2026-02-17T00:00:00Z")
        assert found == [PAT]

    def test_a_non_utc_offset_compares_as_an_instant(self, api):
        """23:00+02:00 is 21:00Z, before a 22:00Z edit; strings would say after."""
        plus_two = timezone(timedelta(hours=2))
        found, _ = chairs_at(
            api, datetime(2026, 2, 16, 23, 0, tzinfo=plus_two), group_time="2026-02-16T22:00:00Z"
        )
        assert found == [PAT]
        found, _ = chairs_at(
            api, datetime(2026, 2, 17, 1, 0, tzinfo=plus_two), group_time="2026-02-16T22:00:00Z"
        )
        assert found == [KIM]


class TestHistoryReading:
    def test_history_split_over_pages_is_fully_read(self, api):
        snapshots = [{"id": 10 + n, "time": f"2020-0{n + 1}-01T00:00:00Z"} for n in range(5)]
        roles = {10 + n: [role(100 + n, 1 + n)] for n in range(5)}
        # the newest snapshot at or before the cutoff is on the third page
        found, fake = chairs_at(
            api, utc(2020, 5, 15), snapshots=snapshots, roles_by_snapshot=roles, page_size=2
        )
        assert found == [KIM]  # snapshot 14, role of person 5
        assert fake.count("/api/v1/group/grouphistory/") == 3

    def test_history_is_read_in_id_order_so_pages_cannot_repeat_or_skip(self, api):
        snapshots = [{"id": 10 + n, "time": f"2020-0{n + 1}-01T00:00:00Z"} for n in range(7)]
        roles = {10 + n: [role(100 + n, 1 + n % 5)] for n in range(7)}
        # unordered pages of this fake come back shuffled per offset
        found, fake = chairs_at(
            api, utc(2020, 6, 15), snapshots=snapshots, roles_by_snapshot=roles,
            page_size=2, shuffle=True,
        )
        history = [p for path, p in fake.calls if path == "/api/v1/group/grouphistory/"]
        assert all(p["order_by"] == "id" for p in history)
        assert found == [ALEX]  # snapshot 15 (June), person 1

    def test_a_server_that_ignores_offset_ends_the_loop_and_the_short_read_is_an_error(self, api):
        snapshots = [{"id": 10 + n, "time": f"2020-0{n + 1}-01T00:00:00Z"} for n in range(5)]
        roles = {10 + n: [role(100 + n, 1 + n)] for n in range(5)}
        DataTrackerClient.clear_chair_cache()
        fake = api(
            snapshots=snapshots, roles_by_snapshot=roles, page_size=2, ignore_offset=True,
            group_time="2025-01-01T00:00:00Z",
        )
        with pytest.raises(DatatrackerError, match="short read: got 2 of 5 rows"):
            DataTrackerClient()._chair_roles_at("6man", utc(2020, 5, 15))
        # only the first page ever arrives; the loop stops at the repeat
        assert fake.count("/api/v1/group/grouphistory/") == 2

    def test_repeated_rows_are_counted_once(self, api):
        snapshots = [{"id": 10, "time": "2020-01-01T00:00:00Z"}] * 3
        found, _ = chairs_at(
            api, utc(2021, 1, 1), snapshots=snapshots, roles_by_snapshot={10: [role(1, 2)]}
        )
        assert found == [SAM]

    def test_history_is_found_by_group_id_for_a_renamed_group(self, api):
        """Snapshot rows carry an older acronym; the group id still finds them."""
        found, fake = chairs_at(api, utc(2021, 3, 8))
        assert found == [SAM, JO]
        history = [p for path, p in fake.calls if path == "/api/v1/group/grouphistory/"]
        assert history and all(p["group"] == GROUP_ID and "acronym" not in p for p in history)

    def test_roles_are_requested_in_id_order(self, api):
        _, fake = chairs_at(api, utc(2021, 3, 8))
        role_calls = [p for path, p in fake.calls if path == "/api/v1/group/rolehistory/"]
        assert role_calls[0]["order_by"] == "id" and role_calls[0]["name__slug"] == "chair"


class TestPeople:
    def test_one_person_in_two_rows_is_one_chair_with_the_lowest_role_id(self, api, caplog):
        live = [role(31, 1, email="alex.work@example.net"), role(30, 1, email="alex.home@example.net")]
        api(live=live)
        with caplog.at_level("INFO", logger="ietf2vcon.datatracker"):
            chairs = DataTrackerClient().get_group_chairs("6man", session_date=utc(2026, 3, 1))

        assert names(chairs) == [ALEX]
        assert chairs[0].email == "alex.home@example.net"  # role 30
        assert any("duplicate chair role 31" in r.getMessage() for r in caplog.records)

    def test_two_people_with_the_same_name_stay_two_chairs(self, api, monkeypatch):
        monkeypatch.setitem(PEOPLE, 2, ALEX)
        found, _ = chairs_at(api, utc(2026, 3, 1), live=[role(1, 1), role(2, 2)])
        assert found == [ALEX, ALEX]

    def test_the_email_is_the_role_records_email(self, api):
        api(live=[role(1, 1, email="role.address@example.net")])
        chairs = DataTrackerClient().get_group_chairs("6man", session_date=utc(2026, 3, 1))
        assert chairs[0].email == "role.address@example.net"


class TestCaching:
    def test_two_sessions_of_a_group_cost_one_set_of_requests(self, api):
        fake = api()
        DataTrackerClient().get_group_chairs("6man", session_date=utc(2021, 3, 8, 9))
        first = len(fake.calls)
        DataTrackerClient().get_group_chairs("6man", session_date=utc(2021, 3, 9, 14))
        assert first > 0 and len(fake.calls) == first

    def test_failed_reads_are_not_cached(self, api):
        fake = api(fail=["/api/v1/group/rolehistory/"])
        client = DataTrackerClient()
        with pytest.raises(DatatrackerError):
            client._chair_roles_at("6man", utc(2021, 3, 8))
        fake.fail.clear()
        assert client._chair_roles_at("6man", utc(2021, 3, 8)) is not None


def test_no_date_still_returns_current_chairs(api):
    fake = api()
    chairs = DataTrackerClient().get_group_chairs("6man")
    assert names(chairs) == [KIM]
    assert fake.count("/api/v1/group/grouphistory/") == 0


def test_session_with_an_empty_snapshot_has_the_attendees_party_first(tmp_path, monkeypatch):
    """The converter treats [] as "no chairs for this session"."""
    from ietf2vcon import converter as converter_module
    from ietf2vcon.converter import ConversionOptions, IETFSessionConverter
    from ietf2vcon.models import IETFMeeting, IETFSession

    fake = FakeAPI(roles_by_snapshot={200: []})

    class EmptySnapshotDataTracker(DataTrackerClient):
        def __init__(self):
            pass

        def _get(self, path, params=None):
            return fake.get(path, params)

        def get_meeting(self, number):
            return IETFMeeting(number=number)

        def get_group_sessions(self, meeting_number, group_acronym):
            return [IETFSession(
                meeting_number=meeting_number, group_acronym=group_acronym,
                session_id="1", start_time=utc(2021, 3, 8, 14),
            )]

        def get_session_materials(self, *a, **k):
            return []

        def close(self):
            pass

    monkeypatch.setattr(converter_module, "DataTrackerClient", EmptySnapshotDataTracker)
    options = ConversionOptions(
        output_dir=tmp_path, include_video=False,
        include_transcript=False, include_chat=False,
    )
    result = IETFSessionConverter(options).convert_session(110, "6man")

    parties = result.vcon.vcon_dict["parties"]
    assert [p["name"] for p in parties] == ["IETF Attendees"]


class TestNoFabricatedSessions:
    """A failed lookup must not become a plausible-looking record.

    convert_session used to synthesize a session -- id "<group>-<meeting>",
    start_time = now -- whenever the session lookup came back empty. A single
    batch run of IETF 125 produced seven vCons stamped with that day's date,
    each with a fresh uuid and a filename no published record matched, and the
    run reported 159/159 successful.
    """

    def _converter(self, tmp_path, sessions_result, monkeypatch):
        from ietf2vcon import converter as converter_module
        from ietf2vcon.converter import ConversionOptions, IETFSessionConverter
        from ietf2vcon.models import IETFMeeting

        class FakeDataTracker:
            def get_meeting(self, number):
                return IETFMeeting(number=number)

            def get_group_sessions(self, meeting_number, group_acronym):
                return sessions_result

            def close(self):
                pass

        monkeypatch.setattr(converter_module, "DataTrackerClient", FakeDataTracker)
        return IETFSessionConverter(ConversionOptions(output_dir=tmp_path))

    def test_failed_lookup_is_an_error(self, tmp_path, monkeypatch):
        result = self._converter(tmp_path, None, monkeypatch).convert_session(125, "ccamp")
        assert result.errors
        assert "look up sessions" in result.errors[0]

    def test_group_that_did_not_meet_is_an_error(self, tmp_path, monkeypatch):
        result = self._converter(tmp_path, [], monkeypatch).convert_session(125, "ccamp")
        assert result.errors
        assert "no sessions" in result.errors[0]

    def test_neither_case_invents_a_session_id(self, tmp_path, monkeypatch):
        for sessions in (None, []):
            result = self._converter(tmp_path, sessions, monkeypatch).convert_session(125, "ccamp")
            assert result.session_id != "ccamp-125"


def test_unknown_session_time_leaves_chairs_unset(tmp_path, monkeypatch):
    """A failed schedule lookup must not fall through to today's chairs.

    Two IETF 125 sessions came back with no start time, and the undated call
    then credited the 2026 chairs to a March session.
    """
    from ietf2vcon import converter as converter_module
    from ietf2vcon.converter import ConversionOptions, IETFSessionConverter
    from ietf2vcon.models import IETFMeeting, IETFPerson, IETFSession

    asked = []

    class FakeDataTracker:
        def get_meeting(self, number):
            return IETFMeeting(number=number)

        def get_group_sessions(self, meeting_number, group_acronym):
            return [IETFSession(
                meeting_number=meeting_number, group_acronym=group_acronym,
                session_id="35232", start_time=None,
            )]

        def get_group_chairs(self, group_acronym, session_date=None):
            asked.append(session_date)
            return [IETFPerson(name="Todays Chair", role="chair")]

        def get_recording_url(self, *a, **k):
            return None

        def get_session_materials(self, *a, **k):
            return []

        def close(self):
            pass

    monkeypatch.setattr(converter_module, "DataTrackerClient", FakeDataTracker)
    options = ConversionOptions(
        output_dir=tmp_path, include_video=False,
        include_transcript=False, include_chat=False,
    )
    result = IETFSessionConverter(options).convert_session(125, "nfsv4")

    assert asked == [], "chairs must not be looked up without a session time"
    names = [p.get("name") for p in result.vcon.vcon_dict["parties"]]
    assert "Todays Chair" not in names
    assert any("start time unknown" in w for w in result.warnings)


class TestSessionWithoutStartTime:
    """T1: a session with no scheduled time is dated by its meeting's first day."""

    def _run(self, tmp_path, monkeypatch, meeting_start, session_start=None):
        from ietf2vcon import converter as converter_module
        from ietf2vcon.converter import ConversionOptions, IETFSessionConverter
        from ietf2vcon.models import IETFMeeting, IETFPerson, IETFSession

        asked = []

        class FakeDataTracker:
            def get_meeting(self, number):
                return IETFMeeting(number=number, start_date=meeting_start)

            def get_group_sessions(self, meeting_number, group_acronym):
                return [IETFSession(
                    meeting_number=meeting_number, group_acronym=group_acronym,
                    session_id="35574", start_time=session_start,
                )]

            def get_group_chairs(self, group_acronym, session_date=None):
                asked.append(session_date)
                return [IETFPerson(name="Chair One", role="chair")]

            def get_recording_url(self, *a, **k):
                return None

            def get_session_materials(self, *a, **k):
                return []

            def close(self):
                pass

        monkeypatch.setattr(converter_module, "DataTrackerClient", FakeDataTracker)
        options = ConversionOptions(
            output_dir=tmp_path, include_video=False,
            include_transcript=False, include_chat=False,
        )
        result = IETFSessionConverter(options).convert_session(126, "dance")
        return result, asked

    def test_chairs_come_from_the_end_of_the_meeting_first_day(
        self, tmp_path, monkeypatch, caplog
    ):
        with caplog.at_level("INFO", logger="ietf2vcon.converter"):
            result, asked = self._run(tmp_path, monkeypatch, datetime(2026, 7, 18))

        assert asked == [datetime(2026, 7, 18, 23, 59, 59, tzinfo=timezone.utc)]
        data = result.vcon.vcon_dict
        assert [p["name"] for p in data["parties"]] == ["Chair One", "IETF Attendees"]
        # no start time is invented for the Session
        assert "start" not in data["sessions"][0] and "duration" not in data["sessions"][0]
        assert any(
            "end of the meeting's first day" in r.getMessage() and r.levelname == "INFO"
            for r in caplog.records
        )

    def test_no_session_start_and_no_meeting_date_gives_no_chairs_and_a_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        with caplog.at_level("INFO", logger="ietf2vcon.converter"):
            result, asked = self._run(tmp_path, monkeypatch, None)

        assert asked == []
        assert [p["name"] for p in result.vcon.vcon_dict["parties"]] == ["IETF Attendees"]
        warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("35574" in m and "dance" in m for m in warned)

    def test_a_session_with_a_start_time_is_unaffected(self, tmp_path, monkeypatch, caplog):
        start = datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc)
        with caplog.at_level("INFO", logger="ietf2vcon.converter"):
            result, asked = self._run(
                tmp_path, monkeypatch, datetime(2026, 7, 18), session_start=start
            )

        assert asked == [start]
        assert result.vcon.vcon_dict["sessions"][0]["start"] == start.isoformat()
        assert not any("first day" in r.getMessage() for r in caplog.records)


def test_a_failed_chair_lookup_fails_the_session_and_saves_nothing(tmp_path, monkeypatch):
    """The session is an error naming group and session; no file is produced."""
    from ietf2vcon import converter as converter_module
    from ietf2vcon.converter import ConversionOptions, IETFSessionConverter
    from ietf2vcon.models import IETFMeeting, IETFSession

    fake = FakeAPI(fail=["/api/v1/group/grouphistory/"])

    class FailingChairs(DataTrackerClient):
        def __init__(self):
            pass

        def _get(self, path, params=None):
            return fake.get(path, params)

        def get_meeting(self, number):
            return IETFMeeting(number=number)

        def get_group_sessions(self, meeting_number, group_acronym):
            return [IETFSession(
                meeting_number=meeting_number, group_acronym=group_acronym,
                session_id="1", start_time=utc(2021, 3, 8, 14),
            )]

        def get_session_materials(self, *a, **k):
            return []

        def close(self):
            pass

    monkeypatch.setattr(converter_module, "DataTrackerClient", FailingChairs)
    options = ConversionOptions(
        output_dir=tmp_path, include_video=False,
        include_transcript=False, include_chat=False,
    )
    converter = IETFSessionConverter(options)
    result = converter.convert_session(110, "6man")

    assert len(result.errors) == 1
    assert "6man session 1 at IETF 110" in result.errors[0] and "chair history" in result.errors[0]
    with pytest.raises(Exception, match="not saving"):
        converter.save_vcon(result)
    assert list(tmp_path.glob("*.json")) == []


class TestAmbiguousTies:
    """Snapshots that share a time but hold different chairs say nothing about who served."""

    T = "2012-02-26T08:21:36Z"

    def _client(self, api, **kwargs):
        DataTrackerClient.clear_chair_cache()
        fake = api(**kwargs)
        return DataTrackerClient(), fake

    def _warned(self, caplog):
        return [r.getMessage() for r in caplog.records if "is ambiguous" in r.getMessage()]

    def test_seven_tied_snapshots_with_four_chair_sets_give_no_chairs(self, api, caplog):
        snapshots = [{"id": 10 + n, "time": self.T} for n in range(7)]
        people = [1, 2, 3, 4, 1, 2, 3]
        roles = {10 + n: [role(100 + n, p)] for n, p in enumerate(people)}
        client, _ = self._client(
            api, snapshots=snapshots, roles_by_snapshot=roles, group_time="2025-01-01T00:00:00Z"
        )
        with caplog.at_level("WARNING", logger="ietf2vcon.datatracker"):
            found = client.get_group_chairs("6man", session_date=utc(2017, 11, 16))

        assert found == []
        assert client.last_chair_ambiguous is True
        [message] = self._warned(caplog)
        assert message == (
            "Chairs for 6man at 2017-11-16T00:00:00+00:00: snapshot 16 is ambiguous; "
            "snapshots 10, 11, 12, 13, 14, 15, 16 share time 2012-02-26T08:21:36+00:00 "
            "with different chairs; omitting chairs"
        )

    def test_tied_snapshots_with_the_same_chair_people_are_not_ambiguous(self, api):
        snapshots = [{"id": 10, "time": self.T}, {"id": 11, "time": self.T}]
        roles = {10: [role(1, 2), role(2, 3)], 11: [role(3, 3), role(4, 2)]}  # same two people
        client, _ = self._client(
            api, snapshots=snapshots, roles_by_snapshot=roles, group_time="2025-01-01T00:00:00Z"
        )
        found = names(client.get_group_chairs("6man", session_date=utc(2017, 11, 16)))
        assert sorted(found) == sorted([SAM, JO])
        assert client.last_chair_ambiguous is False

    def test_tied_snapshots_differing_only_in_an_address_use_the_highest_ids_email(self, api):
        snapshots = [{"id": 10, "time": self.T}, {"id": 11, "time": self.T}]
        roles = {
            10: [role(1, 2, email="old@example.net")],
            11: [role(2, 2, email="new@example.net")],
        }
        client, _ = self._client(
            api, snapshots=snapshots, roles_by_snapshot=roles, group_time="2025-01-01T00:00:00Z"
        )
        [chair] = client.get_group_chairs("6man", session_date=utc(2017, 11, 16))
        assert chair.name == SAM and chair.email == "new@example.net"
        assert client.last_chair_ambiguous is False

    def test_a_tied_snapshot_with_no_chairs_makes_it_ambiguous(self, api, caplog):
        snapshots = [{"id": 10, "time": self.T}, {"id": 11, "time": self.T}]
        roles = {10: [], 11: [role(1, 2)]}
        client, _ = self._client(
            api, snapshots=snapshots, roles_by_snapshot=roles, group_time="2025-01-01T00:00:00Z"
        )
        with caplog.at_level("WARNING", logger="ietf2vcon.datatracker"):
            assert client.get_group_chairs("6man", session_date=utc(2017, 11, 16)) == []
        assert len(self._warned(caplog)) == 1 and client.last_chair_ambiguous is True

    def test_live_roles_differing_from_a_snapshot_at_the_group_time_are_ambiguous(
        self, api, caplog
    ):
        group_time = "2025-06-01T12:00:00Z"
        snapshots = [{"id": 10, "time": "2020-01-01T00:00:00Z"}, {"id": 11, "time": group_time}]
        client, _ = self._client(
            api, snapshots=snapshots, roles_by_snapshot={10: [role(1, 2)], 11: [role(2, 3)]},
            group_time=group_time, live=[role(9000, 5)],
        )
        with caplog.at_level("WARNING", logger="ietf2vcon.datatracker"):
            found = client.get_group_chairs("6man", session_date=utc(2026, 1, 1))

        assert found == [] and client.last_chair_ambiguous is True
        [message] = self._warned(caplog)
        assert "live roles is ambiguous; snapshots 11 share time 2025-06-01T12:00:00+00:00" in message

    def test_live_roles_matching_the_tied_snapshot_are_returned(self, api):
        group_time = "2025-06-01T12:00:00Z"
        snapshots = [{"id": 11, "time": group_time}]
        client, _ = self._client(
            api, snapshots=snapshots,
            roles_by_snapshot={11: [role(2, 5, email="snapshot@example.net")]},
            group_time=group_time, live=[role(9000, 5, email="live@example.net")],
        )
        [chair] = client.get_group_chairs("6man", session_date=utc(2026, 1, 1))
        assert chair.name == KIM and chair.email == "live@example.net"
        assert client.last_chair_ambiguous is False

    def test_no_ties_means_no_extra_role_history_reads(self, api):
        client, fake = self._client(api)
        client.get_group_chairs("6man", session_date=utc(2021, 3, 8))  # snapshot branch
        assert fake.count("/api/v1/group/rolehistory/") == 1

    def test_the_live_branch_without_ties_reads_no_role_history(self, api):
        client, fake = self._client(api)
        client.get_group_chairs("6man", session_date=utc(2026, 3, 20))
        assert fake.count("/api/v1/group/rolehistory/") == 0
        assert fake.count("/api/v1/group/grouphistory/") == 1  # the snapshot list, cached

    def test_tied_role_reads_are_cached_for_later_sessions(self, api):
        snapshots = [{"id": 10, "time": self.T}, {"id": 11, "time": self.T}]
        roles = {10: [role(1, 2)], 11: [role(2, 2)]}
        client, fake = self._client(
            api, snapshots=snapshots, roles_by_snapshot=roles, group_time="2025-01-01T00:00:00Z"
        )
        client.get_group_chairs("6man", session_date=utc(2017, 11, 16))
        first = len(fake.calls)
        DataTrackerClient().get_group_chairs("6man", session_date=utc(2018, 3, 1))
        assert len(fake.calls) == first

    def test_a_failed_read_of_a_tied_snapshot_is_a_lookup_failure(self, api):
        snapshots = [{"id": 10, "time": self.T}, {"id": 11, "time": self.T}]
        roles = {10: [role(1, 2)], 11: [role(2, 2)]}
        client, _ = self._client(
            api, snapshots=snapshots, roles_by_snapshot=roles,
            group_time="2025-01-01T00:00:00Z", fail_snapshot_roles=[10],
        )
        with pytest.raises(DatatrackerError, match="6man"):
            client.get_group_chairs("6man", session_date=utc(2017, 11, 16))
        assert client.last_chair_ambiguous is False

    def test_the_run_summary_counts_ambiguous_sessions_separately(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from ietf2vcon import converter as converter_module
        from ietf2vcon.cli import main
        from ietf2vcon.models import IETFMeeting, IETFSession

        class AmbiguousChairs:
            last_chair_ambiguous = False

            def get_meeting(self, number):
                return IETFMeeting(number=number)

            def get_group_sessions(self, meeting_number, group_acronym):
                return [IETFSession(
                    meeting_number=meeting_number, group_acronym=group_acronym,
                    session_id="7", start_time=utc(2017, 11, 16, 1), status="sched",
                )]

            def get_group_chairs(self, group_acronym, session_date=None):
                self.last_chair_ambiguous = True
                return []

            def get_session_materials(self, *a, **k):
                return []

            def close(self):
                pass

        monkeypatch.setattr(converter_module, "DataTrackerClient", AmbiguousChairs)
        result = CliRunner().invoke(main, [
            "convert-all", "--meeting", "100", "--groups", "iesg", "--no-transcript",
            "--no-video", "--output-dir", str(tmp_path), "--parallel", "1",
        ])
        assert result.exit_code == 0, result.output
        assert "Chairs ambiguous, omitted for 1 session(s): iesg" in result.output
        assert "Chair lookup failed" not in result.output
