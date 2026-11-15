"""The Datatracker client sends its requests through the shared polite client."""

import httpx
import pytest

from ietf2vcon import httpclient

from .test_httpclient import client_for


class TestDatatrackerUsesIt:
    def test_the_datatracker_client_retries_and_wraps_what_survives(self, monkeypatch):
        from ietf2vcon.datatracker import DataTrackerClient
        from ietf2vcon.errors import DatatrackerError

        client, calls = client_for([httpx.Response(500)] * 5)
        monkeypatch.setattr(httpclient.time, "sleep", lambda s: None)
        dt = DataTrackerClient()
        dt.client = client
        with pytest.raises(DatatrackerError, match="HTTP 500"):
            dt._get("https://example.net/x")
        assert len(calls) == 5

    def test_a_404_from_the_datatracker_is_an_error_at_once(self):
        from ietf2vcon.datatracker import DataTrackerClient
        from ietf2vcon.errors import DatatrackerError

        client, calls = client_for([httpx.Response(404)])
        dt = DataTrackerClient()
        dt.client = client
        with pytest.raises(DatatrackerError, match="HTTP 404"):
            dt._get("https://example.net/x")
        assert len(calls) == 1
