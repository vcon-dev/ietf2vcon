"""Chairs must be the people who chaired the session, not today's chairs.

`get_group_chairs()` used to return current role holders with no date filter,
so regenerating a 2021 session credited whoever chairs the group in 2026.
Verified against three published records before this was written: IETF 110
6man was chaired by Bob Hinden and Ole Trøan; the undated query returns Bob
Hinden and Jen Linkova, who took over later.
"""

from datetime import date

import pytest

from ietf2vcon.datatracker import DataTrackerClient


SNAPSHOTS = {
    "objects": [
        {"time": "2014-05-01T00:00:00Z", "resource_uri": "/api/v1/group/grouphistory/100/"},
        {"time": "2021-02-01T00:00:00Z", "resource_uri": "/api/v1/group/grouphistory/200/"},
        {"time": "2026-02-16T00:00:00Z", "resource_uri": "/api/v1/group/grouphistory/300/"},
    ]
}
CHAIRS_BY_SNAPSHOT = {
    "100": ["Old Chair"],
    "200": ["Session Chair", "Co Chair"],
    "300": ["Todays Chair"],
}


@pytest.fixture
def client(monkeypatch):
    """A client whose Datatracker calls are served from the fixtures above."""
    calls = []

    def fake_get(self, path, params=None):
        params = params or {}
        calls.append((path, params))
        if path == "/api/v1/group/grouphistory/":
            return SNAPSHOTS
        if path == "/api/v1/group/rolehistory/":
            names = CHAIRS_BY_SNAPSHOT.get(str(params.get("group")), [])
            return {"objects": [{"person": f"/person/{n}", "email": None} for n in names]}
        if path == "/api/v1/group/role/":
            return {"objects": [{"person": "/person/Current Chair", "email": None}]}
        if path.startswith("/person/"):
            return {"name": path.removeprefix("/person/")}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(DataTrackerClient, "_get", fake_get)
    c = DataTrackerClient()
    c.calls = calls
    return c


def names(chairs):
    return [p.name for p in chairs]


def test_uses_the_snapshot_in_effect_at_the_session(client):
    chairs = client.get_group_chairs("6man", session_date=date(2021, 3, 8))
    assert names(chairs) == ["Session Chair", "Co Chair"]


def test_a_later_session_gets_the_later_snapshot(client):
    chairs = client.get_group_chairs("6man", session_date=date(2026, 3, 20))
    assert names(chairs) == ["Todays Chair"]


def test_session_predating_all_history_gets_no_chairs(client):
    """Role history starts 2011-12-09, so a 2006 session has no resolvable chairs.

    Naming a later chair on a 2006 session would be a fabrication; an absent
    chair party is the honest record.
    """
    assert client.get_group_chairs("6man", session_date=date(2006, 7, 10)) == []
    assert all(path != "/api/v1/group/role/" for path, _ in client.calls)


def test_no_date_still_returns_current_chairs(client):
    """Undated callers keep the old behaviour rather than silently changing."""
    chairs = client.get_group_chairs("6man")
    assert names(chairs) == ["Current Chair"]
    assert all(path != "/api/v1/group/grouphistory/" for path, _ in client.calls)


def test_falls_back_to_current_when_a_group_has_no_history(client, monkeypatch):
    monkeypatch.setattr(
        DataTrackerClient,
        "_get",
        lambda self, path, params=None: (
            {"objects": []} if path == "/api/v1/group/grouphistory/"
            else {"objects": [{"person": "/person/Current Chair", "email": None}]}
            if path == "/api/v1/group/role/"
            else {"name": path.removeprefix("/person/")}
        ),
    )
    chairs = DataTrackerClient().get_group_chairs("newwg", session_date=date(2021, 3, 8))
    assert names(chairs) == ["Current Chair"]


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
