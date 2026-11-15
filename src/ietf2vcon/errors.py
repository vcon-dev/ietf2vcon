"""Errors that make a session fail instead of shaping the output."""


class SessionReadError(RuntimeError):
    """Something a session's file depends on could not be read.

    A published file is replaced only by a complete result, so the converter
    turns this into an error for the session and saves nothing.
    """


class DatatrackerError(SessionReadError):
    """A Datatracker read failed, after the client's retries."""


class MeetingReadError(DatatrackerError):
    """A read every session of the meeting depends on failed.

    The meeting record, the official schedule (with its timeslots and rooms) and
    the session statuses are shared by all sessions, so when one of them cannot
    be read each remaining session would fail the same way, slowly. A batch run
    stops at the first one.
    """


class MaterialFetchError(SessionReadError):
    """A material could not be fetched for a reason other than being absent."""


class CarryOverError(RuntimeError):
    """A previous file exists but cannot safely be regenerated in place."""
