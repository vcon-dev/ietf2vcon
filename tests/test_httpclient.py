"""A polite client: shared throttle, Retry-After, and backoff with jitter."""

import email.utils
import time

import httpx
import pytest

from ietf2vcon import httpclient
from ietf2vcon.httpclient import Throttle, get_with_retries


class FakeClock:
    """A clock the sleep function advances, so spacing can be checked without waiting."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def client_for(responses):
    """An httpx client answering from a list: a Response, or an exception to raise."""
    queue = list(responses)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def get(client, **kwargs):
    clock = FakeClock()
    throttle = Throttle(max_rps=0, clock=clock.time, sleep=clock.sleep)
    waits = []
    kwargs.setdefault("rng", lambda: 1.0)
    result = get_with_retries(
        client, "https://example.net/x", throttle=throttle,
        sleep=lambda s: (waits.append(s), clock.sleep(s)), **kwargs,
    )
    return result, waits


class TestThrottle:
    def test_requests_are_spaced_at_least_the_minimum_interval(self):
        clock = FakeClock()
        throttle = Throttle(max_rps=8, clock=clock.time, sleep=clock.sleep)
        starts = []
        for _ in range(5):
            throttle.wait()
            starts.append(clock.now)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert all(abs(g - 0.125) < 1e-9 for g in gaps)

    def test_no_wait_when_requests_are_already_further_apart(self):
        clock = FakeClock()
        throttle = Throttle(max_rps=8, clock=clock.time, sleep=clock.sleep)
        throttle.wait()
        clock.now += 5
        throttle.wait()
        assert clock.slept == []

    def test_zero_turns_the_throttle_off(self):
        clock = FakeClock()
        throttle = Throttle(max_rps=0, clock=clock.time, sleep=clock.sleep)
        for _ in range(10):
            throttle.wait()
        assert clock.slept == []

    def test_the_rate_can_be_changed(self):
        clock = FakeClock()
        throttle = Throttle(max_rps=8, clock=clock.time, sleep=clock.sleep)
        throttle.set_rate(2)
        throttle.wait()
        throttle.wait()
        assert clock.slept == [pytest.approx(0.5)]

    def test_default_is_eight_per_second_and_the_environment_overrides_it(self, monkeypatch):
        monkeypatch.delenv(httpclient.ENV_VAR, raising=False)
        assert Throttle().min_interval == pytest.approx(0.125)
        monkeypatch.setenv(httpclient.ENV_VAR, "2")
        assert Throttle().min_interval == pytest.approx(0.5)
        monkeypatch.setenv(httpclient.ENV_VAR, "not a number")
        assert Throttle().min_interval == pytest.approx(0.125)

    def test_every_request_goes_through_the_throttle(self):
        clock = FakeClock()
        throttle = Throttle(max_rps=1, clock=clock.time, sleep=clock.sleep)
        client, calls = client_for([httpx.Response(200, json={}), httpx.Response(200, json={})])
        for _ in range(2):
            get_with_retries(client, "https://example.net/x", throttle=throttle, sleep=clock.sleep)
        assert clock.slept == [pytest.approx(1.0)] and len(calls) == 2


class TestRetries:
    def test_a_success_needs_one_request(self):
        client, calls = client_for([httpx.Response(200, json={"ok": True})])
        response, waits = get(client)
        assert response.json() == {"ok": True} and waits == [] and len(calls) == 1

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
    def test_a_4xx_other_than_429_is_a_definitive_answer_at_once(self, status):
        client, calls = client_for([httpx.Response(status)])
        with pytest.raises(httpx.HTTPStatusError) as caught:
            get(client)
        assert caught.value.response.status_code == status and len(calls) == 1

    def test_retry_after_is_honoured_on_429(self):
        client, calls = client_for([
            httpx.Response(429, headers={"Retry-After": "17"}), httpx.Response(200, json={}),
        ])
        _, waits = get(client)
        assert waits == [17.0] and len(calls) == 2

    def test_retry_after_is_honoured_on_503(self):
        client, _ = client_for([
            httpx.Response(503, headers={"Retry-After": "3"}), httpx.Response(200, json={}),
        ])
        _, waits = get(client)
        assert waits == [3.0]

    def test_retry_after_is_capped_at_five_minutes(self):
        client, _ = client_for([
            httpx.Response(429, headers={"Retry-After": "86400"}), httpx.Response(200, json={}),
        ])
        _, waits = get(client)
        assert waits == [300.0]

    def test_retry_after_may_be_an_http_date(self):
        when = email.utils.formatdate(time.time() + 40, usegmt=True)
        client, _ = client_for([
            httpx.Response(429, headers={"Retry-After": when}), httpx.Response(200, json={}),
        ])
        _, waits = get(client)
        assert 30 <= waits[0] <= 40

    def test_backoff_grows_exponentially_to_about_a_minute_over_five_attempts(self):
        client, calls = client_for([httpx.Response(500)] * 4 + [httpx.Response(200, json={})])
        _, waits = get(client)  # rng() == 1.0: the full nominal wait
        assert waits == [5.0, 10.0, 20.0, 40.0] and len(calls) == 5

    def test_backoff_is_jittered(self):
        client, _ = client_for([httpx.Response(502), httpx.Response(200, json={})])
        _, waits = get(client, rng=lambda: 0.0)
        assert waits == [2.5]  # half the nominal 5 seconds

    def test_a_503_without_retry_after_backs_off(self):
        client, _ = client_for([httpx.Response(503), httpx.Response(200, json={})])
        _, waits = get(client)
        assert waits == [5.0]

    def test_timeouts_and_connection_errors_are_retried(self):
        client, calls = client_for([
            httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), httpx.Response(200, json={}),
        ])
        _, waits = get(client)
        assert waits == [5.0, 10.0] and len(calls) == 3

    def test_after_the_fifth_attempt_a_5xx_raises(self):
        client, calls = client_for([httpx.Response(500)] * 5)
        with pytest.raises(httpx.HTTPStatusError) as caught:
            get(client)
        assert caught.value.response.status_code == 500 and len(calls) == 5

    def test_after_the_fifth_attempt_a_429_raises(self):
        client, calls = client_for([httpx.Response(429)] * 5)
        with pytest.raises(httpx.HTTPStatusError):
            get(client)
        assert len(calls) == 5

    def test_after_the_fifth_attempt_a_timeout_is_re_raised(self):
        client, calls = client_for([httpx.ReadTimeout("slow")] * 5)
        with pytest.raises(httpx.ReadTimeout):
            get(client)
        assert len(calls) == 5

    def test_each_retry_is_logged_as_a_warning_with_status_and_wait(self, caplog):
        client, _ = client_for([
            httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={}),
        ])
        with caplog.at_level("WARNING", logger="ietf2vcon.httpclient"):
            get(client)
        [record] = [r for r in caplog.records if r.levelname == "WARNING"]
        assert "429" in record.getMessage() and "7s" in record.getMessage()
        assert "Retry-After" in record.getMessage()
