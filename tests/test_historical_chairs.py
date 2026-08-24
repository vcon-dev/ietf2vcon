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
