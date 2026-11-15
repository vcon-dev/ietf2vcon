"""Polite HTTP for the Datatracker and ietf.org.

Every request the converter makes goes through ``get_with_retries``, which
spaces requests with a shared throttle and retries the way a well-behaved
client should: it waits as long as the server asks (``Retry-After``) on 429
and 503, backs off exponentially with jitter on timeouts and 5xx, and treats
any other 4xx as a definitive answer.

The default is 8 requests per second. Change it with the environment variable
``IETF2VCON_MAX_RPS`` (0 turns the throttle off) or the ``--max-rps`` option.
"""

import email.utils
import logging
import os
import random
import threading
import time
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MAX_RPS = 8.0
ENV_VAR = "IETF2VCON_MAX_RPS"
MAX_ATTEMPTS = 5
BACKOFF_BASE = 5.0       # seconds before the second attempt, doubling after that
BACKOFF_MAX = 60.0
RETRY_AFTER_CAP = 300.0  # never wait longer than five minutes for Retry-After


def rate_from_environment() -> float:
    """Requests per second from the environment, else the default."""
    raw = os.environ.get(ENV_VAR)
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_RPS
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring %s=%r: not a number", ENV_VAR, raw)
        return DEFAULT_MAX_RPS


class Throttle:
    """A minimum interval between requests, shared by every thread."""

    def __init__(
        self,
        max_rps: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0
        self.min_interval = 0.0
        self.set_rate(rate_from_environment() if max_rps is None else max_rps)

    def set_rate(self, max_rps: float) -> None:
        """Requests per second; zero or less means no throttle."""
        self.min_interval = 1.0 / max_rps if max_rps and max_rps > 0 else 0.0

    def wait(self) -> None:
        """Block until this request may start."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self.min_interval
        if start > now:
            self._sleep(start - now)


THROTTLE = Throttle()


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds the server asked us to wait, capped, or None."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - time.time()
    return min(max(seconds, 0.0), RETRY_AFTER_CAP)


def _backoff(attempt: int, rng: Callable[[], float]) -> float:
    """Exponential backoff with jitter, in [half, full] of the nominal wait."""
    nominal = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (attempt - 1))
    return nominal * (0.5 + 0.5 * rng())


def get_with_retries(
    client: httpx.Client,
    url: str,
    params: dict | None = None,
    *,
    throttle: Throttle | None = None,
    sleep: Callable[[float], None] | None = None,
    rng: Callable[[], float] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """GET with the shared throttle and retries; raises httpx errors when it gives up.

    Returns a response with a non-error status. A 4xx other than 429 raises
    ``httpx.HTTPStatusError`` at once. After the last attempt a 429 or 5xx raises
    it too, and a timeout or connection error is re-raised.
    """
    throttle = throttle or THROTTLE
    sleep = sleep or time.sleep
    rng = rng or random.random
    for attempt in range(1, MAX_ATTEMPTS + 1):
        throttle.wait()
        try:
            response = client.get(url, params=params, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            wait = _backoff(attempt, rng)
            logger.warning(
                "GET %s: %s; retry %d of %d in %.0fs",
                url, type(exc).__name__, attempt, MAX_ATTEMPTS - 1, wait,
            )
            sleep(wait)
            continue

        status = response.status_code
        if status == 429 or status >= 500:
            if attempt == MAX_ATTEMPTS:
                response.raise_for_status()
            asked = _retry_after(response) if status in (429, 503) else None
            wait = asked if asked is not None else _backoff(attempt, rng)
            logger.warning(
                "GET %s: HTTP %d; retry %d of %d in %.0fs%s",
                url, status, attempt, MAX_ATTEMPTS - 1, wait,
                " (Retry-After)" if asked is not None else "",
            )
            sleep(wait)
            continue

        response.raise_for_status()
        return response
    raise AssertionError("unreachable")  # pragma: no cover
