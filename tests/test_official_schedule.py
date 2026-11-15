"""Session times come from the meeting's official schedule, and only from it.

A meeting has many schedules in the Datatracker (the official one and working
drafts). The first assignment found for a session is often from a draft, which
gave most sessions a wrong start, duration and room.
"""

from datetime import datetime, timezone

import pytest

from ietf2vcon.datatracker import DataTrackerClient
from ietf2vcon.errors import DatatrackerError

from .fakeapi import listing

MEETING_ID = 4360
OFFICIAL, DRAFT = 5428, 5001


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def ts(slot_id, time, duration="1:30:00", room=None):
    return {
        "id": slot_id, "time": time, "duration": duration,
        "location": f"/api/v1/meeting/room/{room}/" if room else None,
    }


_assignment_ids = iter(range(1000, 100000))


def assignment(schedule, session, slot):
    return {
        "id": next(_assignment_ids),
        "schedule": f"/api/v1/meeting/schedule/{schedule}/",
        "session": f"/api/v1/meeting/session/{session}/",
        "timeslot": f"/api/v1/meeting/timeslot/{slot}/",
    }


def event(event_id, session, status, time="2026-06-01T10:00:00Z"):
    return {
        "id": event_id, "time": time,
        "session": f"/api/v1/meeting/session/{session}/",
        "status": f"/api/v1/name/sessionstatusname/{status}/",
    }


class FakeAPI:
    def __init__(
        self, schedule=OFFICIAL, time_zone="Europe/Vienna", number="126",
        shuffle=False, ignore_offset=False, page_cap=None, base=None,
    ):
        self.number = number
        self.shuffle, self.ignore_offset, self.page_cap = shuffle, ignore_offset, page_cap
        self.base = base
        self.record = {
            "id": MEETING_ID, "number": number, "time_zone": time_zone,
            "city": "Vienna", "country": "AT", "date": "2026-07-18",
            "schedule": f"/api/v1/meeting/schedule/{schedule}/" if schedule else None,
        }
        self.sessions = {"vcon": [{"id": 100, "group": "/api/v1/group/group/1/"}],
                         "6man": [{"id": 200, "group": "/api/v1/group/group/2/"},
                                  {"id": 201, "group": "/api/v1/group/group/2/"}]}
        self.assignments = [
            # the draft schedule is listed first, with different times
            assignment(DRAFT, 100, 1), assignment(DRAFT, 200, 1),
            assignment(OFFICIAL, 100, 2), assignment(OFFICIAL, 200, 3),
        ]
        self.timeslots = [
            ts(1, "2026-07-20T07:00:00Z", room=10),
            ts(2, "2026-07-22T12:00:00Z", "2:00:00", room=11),
            ts(3, "2026-07-20T14:30:00Z", "1:00:00", room=10),
        ]
        self.rooms = [{"id": 10, "name": "Draft Hall"}, {"id": 11, "name": "Room A"}]
        # session 201 has no scheduling event at all
        self.events = [event(1, 100, "sched"), event(2, 200, "sched")]
        self.fail = set()
        self.calls = []

    def count(self, prefix):
        return sum(1 for path, _ in self.calls if path.startswith(prefix))

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if any(path.startswith(f) for f in self.fail):
            raise DatatrackerError(f"datatracker down: {path}")
        paged = dict(
            page_cap=self.page_cap, shuffle=self.shuffle, ignore_offset=self.ignore_offset
        )
        if path == "/api/v1/meeting/meeting/":
            assert str(params["number"]) == self.number
            return listing(path, [self.record], params)
        if path == "/api/v1/meeting/session/":
            if "group__acronym" in params:
                rows = self.sessions.get(params["group__acronym"], [])
            else:
                rows = [row for group in self.sessions.values() for row in group]
            return listing(path, rows, params)
        if path.startswith("/api/v1/group/group/"):
            gid = path.rstrip("/").rsplit("/", 1)[1]
            return {"name": f"Group {gid}", "acronym": f"g{gid}"}
        if path.startswith("/api/v1/meeting/schedule/"):
            record = {"id": int(path.rstrip("/").rsplit("/", 1)[1]), "name": "final"}
            if self.base:
                record["base"] = f"/api/v1/meeting/schedule/{self.base}/"
            return record
        if path == "/api/v1/meeting/schedtimesessassignment/":
            assert "schedule" in params and "session" not in params, "must filter by schedule"
            want = f"/api/v1/meeting/schedule/{params['schedule']}/"
            rows = [a for a in self.assignments if a["schedule"] == want]
            return listing(path, rows, params, **paged)
        if path == "/api/v1/meeting/schedulingevent/":
            assert str(params["session__meeting__number"]) == self.number
            return listing(path, self.events, params, **paged)
        if path == "/api/v1/meeting/timeslot/":
            assert params["meeting"] == MEETING_ID
            return listing(path, self.timeslots, params, **paged)
        if path == "/api/v1/meeting/room/":
            assert params["meeting"] == MEETING_ID
            return listing(path, self.rooms, params, **paged)
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


def sessions_of(group, number="126"):
    return DataTrackerClient().get_group_sessions(number, group)


def test_the_official_schedule_wins_over_a_draft_listed_first(api):
    fake = api()
    [session] = sessions_of("vcon")

    assert session.start_time == utc(2026, 7, 22, 12)
    assert session.duration_seconds == 7200
    assert session.room == "Room A"
    asked = [p["schedule"] for path, p in fake.calls if "schedtimesess" in path]
    assert asked == [OFFICIAL]


def test_a_session_absent_from_the_official_schedule_has_no_time_duration_or_room(api):
    api()
    first, second = sessions_of("6man")

    assert first.start_time == utc(2026, 7, 20, 14, 30) and first.room == "Draft Hall"
    # session 201 has no assignment anywhere in the official schedule
    assert second.start_time is None
    assert second.duration_seconds is None
    assert second.room is None


def test_a_session_only_in_a_draft_schedule_gets_nothing(api):
    fake = api()
    fake.assignments = [assignment(DRAFT, 100, 1)]
    [session] = sessions_of("vcon")
    assert session.start_time is None and session.room is None


def test_a_meeting_without_an_official_schedule_is_time_unknown_with_one_warning(
    api, caplog
):
    fake = api(schedule=None)
    with caplog.at_level("INFO", logger="ietf2vcon.datatracker"):
        [vcon] = sessions_of("vcon")
        six = sessions_of("6man")

    for session in [vcon, *six]:
        assert session.start_time is None and session.duration_seconds is None
        assert session.room is None
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warned) == 1 and "126" in warned[0]
    assert fake.count("/api/v1/meeting/schedtimesessassignment/") == 0


def test_an_unreadable_schedule_fails_the_session_and_is_retried_later(api):
    """A failed read is an error, not a quiet "no time"; nothing is cached."""
    fake = api()
    fake.fail = {"/api/v1/meeting/timeslot/"}
    with pytest.raises(DatatrackerError, match="official schedule"):
        sessions_of("vcon")
    with pytest.raises(DatatrackerError, match="official schedule"):
        sessions_of("6man")

    fake.fail = set()
    [again] = sessions_of("vcon")
    assert again.start_time == utc(2026, 7, 22, 12)


def test_a_session_missing_from_a_schedule_that_was_read_is_still_a_real_answer(api):
    """The distinction: the read succeeded and the session is not in it."""
    api()
    _, second = sessions_of("6man")
    assert second.start_time is None and second.room is None


def test_an_unreadable_meeting_record_fails(api):
    fake = api()
    fake.fail = {"/api/v1/meeting/meeting/"}
    with pytest.raises(DatatrackerError):
        sessions_of("vcon")
    with pytest.raises(DatatrackerError):
        DataTrackerClient().get_meeting("126")


def test_an_unreadable_group_record_fails_rather_than_naming_the_group_unknown(api):
    fake = api()
    fake.fail = {"/api/v1/group/group/"}
    with pytest.raises(DatatrackerError):
        sessions_of("vcon")
    with pytest.raises(DatatrackerError):
        DataTrackerClient().get_meeting_sessions("126")


class TestTimeslotTimeFormat:
    """The API writes Z times; offsets and naive values are handled anyway."""

    def _start(self, api, written, time_zone="Europe/Vienna"):
        fake = api(time_zone=time_zone)
        fake.timeslots[1]["time"] = written
        [session] = sessions_of("vcon")
        return session.start_time

    def test_a_z_suffix_is_utc(self, api):
        start = self._start(api, "2026-07-22T12:00:00Z")
        assert start == utc(2026, 7, 22, 12) and start.utcoffset().total_seconds() == 0

    def test_an_offset_is_converted_to_utc(self, api):
        start = self._start(api, "2026-07-22T14:00:00+02:00")
        assert start == utc(2026, 7, 22, 12) and start.utcoffset().total_seconds() == 0

    def test_a_naive_time_is_the_meeting_local_time(self, api):
        # 14:00 in Vienna in July (UTC+2) is 12:00 UTC
        start = self._start(api, "2026-07-22T14:00:00")
        assert start == utc(2026, 7, 22, 12) and start.tzinfo is not None

    def test_a_naive_time_with_no_usable_zone_is_unknown(self, api):
        assert self._start(api, "2026-07-22T14:00:00", time_zone=None) is None
        assert self._start(api, "2026-07-22T14:00:00", time_zone="Not/AZone") is None


def test_two_groups_of_one_meeting_cost_one_schedule_fetch(api):
    fake = api()
    sessions_of("vcon")
    sessions_of("6man")

    assert fake.count("/api/v1/meeting/meeting/") == 1
    assert fake.count("/api/v1/meeting/schedule/") == 1
    assert fake.count("/api/v1/meeting/schedtimesessassignment/") == 1
    assert fake.count("/api/v1/meeting/timeslot/") == 1
    assert fake.count("/api/v1/meeting/room/") == 1


def test_get_meeting_shares_the_cached_record(api):
    fake = api()
    assert DataTrackerClient().get_meeting("126").time_zone == "Europe/Vienna"
    sessions_of("vcon")
    assert fake.count("/api/v1/meeting/meeting/") == 1


def test_requests_per_session_after_the_first_group(api):
    fake = api()
    sessions_of("vcon")
    before = len(fake.calls)
    sessions_of("6man")  # two sessions, one group name
    assert len(fake.calls) - before == 2  # the session list and one group name


def test_an_interim_meeting_name_works(api):
    fake = api(number="interim-2026-vcon-02", schedule=5529)
    fake.assignments = [assignment(5529, 100, 3)]
    fake.timeslots = [ts(3, "2026-09-09T14:00:00Z", "2:00:00", room=None)]
    [session] = sessions_of("vcon", "interim-2026-vcon-02")

    assert session.start_time == utc(2026, 9, 9, 14)
    assert session.duration_seconds == 7200
    assert session.room is None  # an interim timeslot has no room


class TestStablePaging:
    """Schedule, timeslot and room reads use order_by=id and tolerate odd servers."""

    def test_every_read_is_ordered_by_id(self, api):
        fake = api()
        sessions_of("vcon")
        for prefix in ("schedtimesessassignment", "timeslot", "room"):
            calls = [p for path, p in fake.calls if path == f"/api/v1/meeting/{prefix}/"]
            assert calls and all(p["order_by"] == "id" for p in calls), prefix

    def test_small_shuffled_pages_still_give_every_session_its_slot(self, api):
        fake = api(shuffle=True, page_cap=1)
        fake.sessions["vcon"] = [{"id": 100, "group": "/api/v1/group/group/1/"}]
        [session] = sessions_of("vcon")
        assert session.start_time == utc(2026, 7, 22, 12) and session.room == "Room A"
        assert fake.count("/api/v1/meeting/timeslot/") == 3  # one row per page

    def test_a_server_that_ignores_offset_does_not_loop_and_the_short_read_is_an_error(self, api):
        fake = api(ignore_offset=True, page_cap=1)
        with pytest.raises(DatatrackerError, match="short read"):
            sessions_of("vcon")
        # the first page repeats, so the read stops on its second request
        assert fake.count("/api/v1/meeting/schedtimesessassignment/") == 2


class TestShortReads:
    """A list that ends with fewer rows than the server's count is an error."""

    def _client(self, monkeypatch, pages):
        queue = list(pages)

        def fake_get(self, path, params=None):
            return queue.pop(0)

        monkeypatch.setattr(DataTrackerClient, "_get", fake_get)
        return DataTrackerClient()

    @staticmethod
    def page(ids, total, nxt=True):
        return {
            "meta": {"limit": 2, "next": "/more" if nxt else None, "offset": 0,
                     "previous": None, "total_count": total},
            "objects": [{"id": i} for i in ids],
        }

    def test_fewer_rows_than_total_count_raise(self, monkeypatch):
        client = self._client(monkeypatch, [self.page([1, 2], 5), self.page([], 5, nxt=False)])
        with pytest.raises(DatatrackerError, match="short read: got 2 of 5 rows from /x"):
            client._get_all("/x", {"a": 1}, limit=2)

    def test_exactly_total_count_rows_is_complete(self, monkeypatch):
        client = self._client(monkeypatch, [self.page([1, 2], 3), self.page([3], 3, nxt=False)])
        assert [r["id"] for r in client._get_all("/x", limit=2)] == [1, 2, 3]

    def test_a_count_that_grows_between_pages_is_followed(self, monkeypatch):
        client = self._client(monkeypatch, [
            self.page([1, 2], 3), self.page([3, 4], 4), self.page([], 4, nxt=False),
        ])
        assert [r["id"] for r in client._get_all("/x", limit=2)] == [1, 2, 3, 4]

    def test_more_rows_than_first_reported_are_accepted(self, monkeypatch):
        client = self._client(monkeypatch, [self.page([1, 2, 3], 2, nxt=False)])
        assert len(client._get_all("/x", limit=5)) == 3

    def test_reading_stops_once_the_reported_count_is_reached(self, monkeypatch):
        client = self._client(monkeypatch, [self.page([1, 2], 2)])
        assert [r["id"] for r in client._get_all("/x", limit=2)] == [1, 2]

    def test_a_count_that_grows_and_is_not_reached_is_short(self, monkeypatch):
        client = self._client(monkeypatch, [
            self.page([1, 2], 3), self.page([], 5, nxt=False),
        ])
        with pytest.raises(DatatrackerError, match="short read: got 2 of 5"):
            client._get_all("/x", limit=2)

    def test_a_list_with_no_count_cannot_be_short(self, monkeypatch):
        page = {"meta": {}, "objects": [{"id": 1}]}
        client = self._client(monkeypatch, [page])
        assert [r["id"] for r in client._get_all("/x", limit=10)] == [1]

    def test_a_short_read_fails_a_session_through_the_normal_path(self, api):
        fake = api(ignore_offset=True, page_cap=1)
        fake.sessions["vcon"] = [{"id": 100, "group": "/api/v1/group/group/1/"}]
        with pytest.raises(DatatrackerError):
            sessions_of("vcon")


class TestBaseSchedule:
    def test_assignments_of_a_base_schedule_are_included(self, api):
        fake = api()
        fake.base = 4000
        # session 300 is only in the base schedule
        fake.assignments.append(assignment(4000, 300, 3))
        fake.sessions["dance"] = [{"id": 300, "group": "/api/v1/group/group/3/"}]
        [session] = sessions_of("dance")
        assert session.start_time == utc(2026, 7, 20, 14, 30)

    def test_the_official_schedules_own_assignment_wins(self, api):
        fake = api()
        fake.base = 4000
        fake.assignments.append(assignment(4000, 100, 3))  # the base says 07-20 14:30
        [session] = sessions_of("vcon")  # the official schedule says 07-22 12:00
        assert session.start_time == utc(2026, 7, 22, 12)

    def test_a_schedule_with_no_base_reads_only_itself(self, api):
        fake = api()
        sessions_of("vcon")
        asked = [p["schedule"] for path, p in fake.calls if "schedtimesess" in path]
        assert asked == [OFFICIAL]


class TestSessionStatus:
    """A session that holds a slot but did not meet has no time (status from scheduling events)."""

    def test_a_scheduled_session_keeps_its_time_and_status(self, api):
        api()
        [session] = sessions_of("vcon")
        assert session.status == "sched"
        assert session.start_time == utc(2026, 7, 22, 12) and session.room == "Room A"

    def test_a_cancelled_session_with_an_assignment_has_no_time_and_records_the_status(self, api):
        fake = api()
        fake.events.append(event(3, 100, "canceled", "2026-07-01T10:00:00Z"))
        [session] = sessions_of("vcon")

        assert session.status == "canceled"
        assert session.start_time is None
        assert session.duration_seconds is None
        assert session.room is None

    @pytest.mark.parametrize(
        "slug", ["canceledpa", "notmeet", "resched", "deleted", "disappr", "apprw", "schedw", "appr", "scheda"],
    )
    def test_every_status_other_than_sched_means_it_did_not_meet(self, api, slug):
        fake = api()
        fake.events = [event(1, 100, slug)]
        [session] = sessions_of("vcon")
        assert session.status == slug and session.start_time is None

    def test_the_latest_event_wins_and_the_highest_id_breaks_a_time_tie(self, api):
        fake = api()
        fake.events = [
            event(5, 100, "canceled", "2026-06-01T10:00:00Z"),
            event(6, 100, "sched", "2026-06-01T10:00:00Z"),   # same time, higher id
            event(99, 100, "notmeet", "2026-05-01T10:00:00Z"),  # older time, highest id
        ]
        [session] = sessions_of("vcon")
        assert session.status == "sched" and session.start_time is not None

    def test_a_session_with_no_event_is_judged_by_the_schedule(self, api):
        api()
        first, second = sessions_of("6man")
        assert first.status == "sched" and first.start_time is not None
        assert second.status is None and second.start_time is None  # not on the schedule

    def test_a_meeting_with_no_events_at_all_uses_the_schedule(self, api, caplog):
        fake = api()
        fake.events = []
        with caplog.at_level("INFO", logger="ietf2vcon.datatracker"):
            [session] = sessions_of("vcon")
        assert session.status is None and session.start_time == utc(2026, 7, 22, 12)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_an_unreadable_status_fails_the_session_and_is_not_cached(self, api):
        fake = api()
        fake.fail = {"/api/v1/meeting/schedulingevent/"}
        fake.events.append(event(3, 100, "canceled", "2026-07-01T10:00:00Z"))
        with pytest.raises(DatatrackerError, match="session statuses"):
            sessions_of("vcon")

        fake.fail = set()  # a failed read is not cached
        [again] = sessions_of("vcon")
        assert again.status == "canceled" and again.start_time is None

    def test_events_are_read_once_per_meeting(self, api):
        fake = api()
        sessions_of("vcon")
        sessions_of("6man")
        assert fake.count("/api/v1/meeting/schedulingevent/") == 1
        calls = [p for path, p in fake.calls if path == "/api/v1/meeting/schedulingevent/"]
        assert calls[0]["order_by"] == "id"
        assert calls[0]["session__meeting__number"] == "126"

    def test_requests_per_session_after_the_first_group_are_unchanged(self, api):
        fake = api()
        sessions_of("vcon")
        before = len(fake.calls)
        sessions_of("6man")
        assert len(fake.calls) - before == 2  # session list and one group name


class TestSessionOrder:
    def test_sessions_come_back_by_integer_id_whatever_the_server_does(self, api):
        fake = api()
        fake.sessions["6man"] = [
            {"id": 1000, "group": "/api/v1/group/group/2/"},
            {"id": 999, "group": "/api/v1/group/group/2/"},
            {"id": 25892, "group": "/api/v1/group/group/2/"},
        ]
        assert [s.session_id for s in sessions_of("6man")] == ["999", "1000", "25892"]
        asked = [p for path, p in fake.calls if path == "/api/v1/meeting/session/"]
        assert asked[0]["order_by"] == "id"


class TestListingIsOrdered:
    def test_the_meeting_listing_uses_the_ordered_reader_and_returns_ids_ascending(self, api):
        fake = api(shuffle=True, page_cap=1)
        fake.sessions = {
            "vcon": [{"id": 100, "group": "/api/v1/group/group/1/"}],
            "6man": [
                {"id": 201, "group": "/api/v1/group/group/2/"},
                {"id": 1000, "group": "/api/v1/group/group/2/"},
                {"id": 200, "group": "/api/v1/group/group/2/"},
            ],
        }
        listed = DataTrackerClient().get_meeting_sessions("126")
        assert [s.session_id for s in listed] == ["100", "200", "201", "1000"]
        asked = [p for path, p in fake.calls if path == "/api/v1/meeting/session/"]
        assert asked and all(p["order_by"] == "id" for p in asked)

    def test_no_unordered_pager_is_left(self):
        assert not hasattr(DataTrackerClient, "_get_paginated")

    def test_the_materials_list_is_read_by_the_ordered_reader(self, monkeypatch):
        calls = []

        def fake_get(self, path, params=None):
            calls.append((path, dict(params or {})))
            if path == "/api/v1/meeting/sessionpresentation/":
                return listing(path, [], params)
            raise AssertionError(path)

        monkeypatch.setattr(DataTrackerClient, "_get", fake_get)
        DataTrackerClient().get_session_materials("126", "vcon")
        assert calls[0][1]["order_by"] == "id"

    def test_a_failed_document_read_fails_the_materials_list(self, monkeypatch):
        def fake_get(self, path, params=None):
            if path == "/api/v1/meeting/sessionpresentation/":
                return listing(path, [
                    {"id": 1, "document": "/api/v1/doc/document/a/", "order": 1},
                    {"id": 2, "document": "/api/v1/doc/document/b/", "order": 2},
                ], params)
            if path == "/api/v1/doc/document/a/":
                return {"name": "slides-126-vcon-a", "title": "A", "uploaded_filename": "a.pdf"}
            raise DatatrackerError("datatracker down: document b")

        monkeypatch.setattr(DataTrackerClient, "_get", fake_get)
        with pytest.raises(DatatrackerError, match="materials of vcon at IETF 126"):
            DataTrackerClient().get_session_materials("126", "vcon")
