"""IETF Datatracker API client.

Provides access to IETF meeting metadata, sessions, materials, and more.
API documentation: https://datatracker.ietf.org/api/
"""

import logging
import re
from pathlib import Path
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .errors import DatatrackerError, MeetingReadError
from .httpclient import get_with_retries
from .models import (
    IETFMaterial,
    IETFMeeting,
    IETFPerson,
    IETFSession,
    MeetingNumber,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://datatracker.ietf.org"

# The session status slug that means a session was scheduled and held. The
# others (canceled, canceledpa, notmeet, resched, deleted, disappr, apprw,
# schedw, appr, scheda) mean it did not meet, or is not settled.
MET_STATUS = "sched"

# A session document that is really a draft or RFC, not a meeting material.
DOC_NAME_RE = re.compile(r"^(draft-|rfc\d+$)")

# Where the IETF serves each published file verbatim. This is the tree
# rsync.ietf.org::proceedings mirrors, and unlike the Datatracker material URL
# it returns the bytes as uploaded rather than a rendered or converted view.
PROCEEDINGS_BASE = "https://www.ietf.org/proceedings"


def proceedings_url(meeting_number: int, mat_type: str, uploaded_filename: str) -> str:
    """The verbatim location of a published material."""
    return f"{PROCEEDINGS_BASE}/{meeting_number}/{mat_type}/{uploaded_filename}"
API_BASE = f"{BASE_URL}/api/v1"


class DataTrackerClient:
    """Client for the IETF Datatracker API."""

    def __init__(self, timeout: float = 30.0):
        self.client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _get(self, url: str, params: dict | None = None) -> dict[str, Any]:
        """GET and decode JSON, with the shared throttle and retries.

        Any failure that survives the retries is a DatatrackerError.
        """
        try:
            return get_with_retries(self.client, url, params).json()
        except httpx.HTTPStatusError as e:
            raise DatatrackerError(
                f"GET {url} failed: HTTP {e.response.status_code}"
            ) from e
        except (httpx.HTTPError, ValueError) as e:
            raise DatatrackerError(f"GET {url} failed: {type(e).__name__}: {e}") from e

    def _get_all(
        self, url: str, params: dict | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Read every row of a list resource, in a stable order.

        Pages by offset with ``order_by=id``: without an order the server's
        default is not defined, so rows can repeat or go missing across pages.
        Rows are de-duplicated by id, and reading stops when a page brings no
        new row (which also ends the loop if the server ignores ``offset``),
        when ``meta.total_count`` rows are in hand, or when a short page has no
        ``meta.next``.

        Ending with fewer rows than the latest ``total_count`` raises
        DatatrackerError: a truncated list would shape the file silently. A
        count that grows between pages is followed (more rows than first
        reported are accepted); one that shrinks is compared as last reported.
        """
        rows: list[dict[str, Any]] = []
        seen: set[Any] = set()
        total = None
        while True:
            data = self._get(
                url, {**(params or {}), "order_by": "id", "limit": limit, "offset": len(rows)}
            )
            page = data.get("objects", [])
            meta = data.get("meta") or {}
            total = meta.get("total_count", total)
            fresh = [r for r in page if r.get("id", r.get("resource_uri")) not in seen]
            for row in fresh:
                seen.add(row.get("id", row.get("resource_uri")))
            rows.extend(fresh)
            if not fresh:
                break
            if total is not None and len(rows) >= total:
                break
            if total is None and len(page) < limit and not meta.get("next"):
                break
        if total is not None and len(rows) < total:
            raise DatatrackerError(
                f"short read: got {len(rows)} of {total} rows from {url} {params or {}}"
            )
        return rows

    def get_meeting(self, meeting_number: MeetingNumber) -> IETFMeeting | None:
        """Get metadata for an IETF meeting by number.

        Returns None when the Datatracker has no such meeting; a failed read
        raises DatatrackerError.
        """
        meeting = self._meeting_record(meeting_number)
        if not meeting:
            return None
        return IETFMeeting(
            number=meeting_number,
            city=meeting.get("city"),
            country=meeting.get("country"),
            start_date=self._parse_date(meeting.get("date")),
            time_zone=meeting.get("time_zone"),
        )

    # Meeting records and official schedules are cached for the life of the
    # process, shared by every client: the converter makes a new client per
    # session, and a meeting has about 150 of them. Only successful reads (and
    # the stable fact that a meeting has no official schedule) are cached.
    _meeting_cache: dict[tuple, Any] = {}
    _schedule_warned: set[str] = set()

    @classmethod
    def clear_meeting_cache(cls) -> None:
        """Forget cached meeting records, schedules and group names."""
        cls._meeting_cache.clear()
        cls._schedule_warned.clear()

    def _meeting_record(self, meeting_number: MeetingNumber) -> dict | None:
        """The Datatracker's meeting record, or None when there is none."""
        key = ("meeting", str(meeting_number))
        cache = DataTrackerClient._meeting_cache
        if key not in cache:
            try:
                data = self._get("/api/v1/meeting/meeting/", {"number": meeting_number})
            except DatatrackerError as e:
                raise MeetingReadError(
                    f"IETF {meeting_number}: could not read the meeting record: {e}"
                ) from e
            objects = data.get("objects", [])
            cache[key] = objects[0] if objects else None
        return cache[key]

    @staticmethod
    def _uri_id(uri: str | None) -> int | None:
        """The numeric id at the end of a resource URI, or None."""
        if not uri:
            return None
        tail = uri.rstrip("/").rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else None

    def _timeslot_utc(self, text: str | None, time_zone: str | None) -> datetime | None:
        """A timeslot `time` as an aware UTC datetime.

        The API writes timeslot times with a Z suffix (checked for IETF 126,
        IETF 70 and an interim), so this is normally just UTC. A value with no
        offset would be the meeting's local time, so it is converted with the
        meeting record's `time_zone`; with no usable zone it is unknown rather
        than guessed.
        """
        parsed = self._parse_datetime(text)
        if parsed is None:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
        try:
            return parsed.replace(tzinfo=ZoneInfo(time_zone or "")).astimezone(UTC)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "Timeslot time %r has no offset and the meeting time zone %r is "
                "unusable; treating the time as unknown", text, time_zone,
            )
            return None

    def _official_schedule(self, meeting_number: MeetingNumber) -> dict[int, dict] | None:
        """Session id -> {start, duration, room} from the meeting's official schedule.

        A meeting has many schedules (the official one and working drafts).
        Only the one named by the meeting record's `schedule` field counts, and
        it is read once per meeting in three paged queries: its assignments,
        and the meeting's timeslots and rooms. A session with no assignment in
        it is simply absent from the result.

        Returns None, with one warning per meeting, when the meeting genuinely
        has no official schedule; every session is then time unknown. A read
        that fails raises DatatrackerError and is not cached.
        """
        key = ("schedule", str(meeting_number))
        cache = DataTrackerClient._meeting_cache
        if key in cache:
            return cache[key]

        meeting = self._meeting_record(meeting_number)
        if not meeting:
            raise MeetingReadError(f"IETF {meeting_number}: no meeting record")
        schedule_id = self._uri_id(meeting.get("schedule"))
        if schedule_id is None:
            if str(meeting_number) not in DataTrackerClient._schedule_warned:
                DataTrackerClient._schedule_warned.add(str(meeting_number))
                logger.warning(
                    "IETF %s: the meeting has no official schedule; session times, "
                    "durations and rooms are unknown", meeting_number,
                )
            cache[key] = None
            return None
        meeting_id = meeting.get("id")
        time_zone = meeting.get("time_zone")

        try:
            base_assignments, own_assignments = self._schedule_assignments(schedule_id)
            timeslots = {
                t["id"]: t for t in self._get_all(
                    "/api/v1/meeting/timeslot/", {"meeting": meeting_id}
                )
            }
            rooms = {
                r["id"]: r.get("name") for r in self._get_all(
                    "/api/v1/meeting/room/", {"meeting": meeting_id}
                )
            }
        except DatatrackerError as e:
            raise MeetingReadError(
                f"IETF {meeting_number}: could not read the official schedule: {e}"
            ) from e

        def index(assignments: list[dict]) -> dict[int, dict]:
            found: dict[int, dict] = {}
            for assignment in assignments:
                session_id = self._uri_id(assignment.get("session"))
                timeslot = timeslots.get(self._uri_id(assignment.get("timeslot")))
                if session_id is None or timeslot is None:
                    continue
                start = self._timeslot_utc(timeslot.get("time"), time_zone)
                if start is None:
                    continue
                entry = {
                    "start": start,
                    "duration": timeslot.get("duration"),
                    "room": rooms.get(self._uri_id(timeslot.get("location"))),
                }
                # A session extended over two slots has two assignments; the
                # earlier slot is when it began.
                if session_id not in found or start < found[session_id]["start"]:
                    found[session_id] = entry
            return found

        # The official schedule's own assignment wins over its base's.
        schedule = {**index(base_assignments), **index(own_assignments)}
        cache[key] = schedule
        return schedule

    def _schedule_assignments(self, schedule_id: int) -> tuple[list[dict], list[dict]]:
        """(base assignments, own assignments) of a schedule.

        A schedule can name a base schedule (``base``) whose assignments show up
        in it too; the caller lets the schedule's own assignment win for a
        session in both. The API's schedule resource does not expose ``base``
        (checked for IETF 126), so the base list is empty unless a future API
        does.
        """
        record = self._get(f"/api/v1/meeting/schedule/{schedule_id}/")
        base_id = self._uri_id(record.get("base"))
        base: list[dict] = []
        if base_id is not None and base_id != schedule_id:
            base = self._get_all(
                "/api/v1/meeting/schedtimesessassignment/", {"schedule": base_id}
            )
        own = self._get_all(
            "/api/v1/meeting/schedtimesessassignment/", {"schedule": schedule_id}
        )
        return base, own

    def _session_statuses(self, meeting_number: MeetingNumber) -> dict[int, str]:
        """Session id -> current status slug, for the whole meeting.

        A session's status is that of its latest scheduling event, newest
        ``time`` first and the highest id on a tie (as the Datatracker itself
        resolves it); the session record's own ``status`` is always null. Read
        in one paged query per meeting and cached. A session with no event has
        no entry, and a meeting with no events at all gives an empty result (a
        real answer: sessions are then judged by the schedule alone). A failed
        read raises DatatrackerError and is not cached.
        """
        key = ("status", str(meeting_number))
        cache = DataTrackerClient._meeting_cache
        if key in cache:
            return cache[key]
        try:
            events = self._get_all(
                "/api/v1/meeting/schedulingevent/",
                {"session__meeting__number": meeting_number},
            )
        except DatatrackerError as e:
            raise MeetingReadError(
                f"IETF {meeting_number}: could not read session statuses: {e}"
            ) from e

        latest: dict[int, tuple[tuple, str]] = {}
        for event in events:
            session_id = self._uri_id(event.get("session"))
            when = self._api_time(event.get("time"))
            slug = (event.get("status") or "").rstrip("/").rsplit("/", 1)[-1]
            if session_id is None or when is None or not slug:
                continue
            rank = (when, int(event.get("id", 0)))
            if session_id not in latest or rank > latest[session_id][0]:
                latest[session_id] = (rank, slug)
        statuses = {sid: slug for sid, (_, slug) in latest.items()}
        if not statuses:
            logger.info(
                "IETF %s has no scheduling events; session status is unknown", meeting_number
            )
        cache[key] = statuses
        return statuses

    def _group_record(self, group_uri: str) -> dict:
        """A group's record, cached across sessions; a failed read raises."""
        key = ("group", group_uri)
        cache = DataTrackerClient._meeting_cache
        if key not in cache:
            cache[key] = self._get(group_uri)
        return cache[key]

    def get_group_sessions(
        self, meeting_number: MeetingNumber, group_acronym: str
    ) -> list[IETFSession]:
        """Get sessions for a specific working group at a meeting, by id.

        This method queries the API directly for the specific group,
        avoiding the need to fetch all sessions.

        An empty list means the group genuinely did not meet. A read that
        fails (the session list, the official schedule, the statuses, a group
        name) raises DatatrackerError: a session's time, status and name shape
        its file, so none of them may be silently left out.
        """
        sessions = []
        schedule = self._official_schedule(meeting_number)
        statuses = self._session_statuses(meeting_number)
        # Query sessions directly filtered by group and meeting. Without
        # an order the server's is undefined, so which session came first
        # could change between runs; they are read, and kept, by id.
        data = sorted(
            self._get_all(
                "/api/v1/meeting/session/",
                {"meeting__number": meeting_number, "group__acronym": group_acronym},
            ),
            key=lambda row: int(row.get("id") or 0),
        )

        for session_data in data:
            session_id = session_data.get("id") or str(session_data.get("pk", ""))

            group_uri = session_data.get("group")
            group_name = self._group_record(group_uri).get("name") if group_uri else None

            # Time, duration and room come from the meeting's official
            # schedule only. A session absent from it was not scheduled
            # (or was cancelled) and has none; no other schedule is used.
            # A session that holds a slot but whose current status is not
            # "scheduled" (cancelled, not meeting, ...) never started and
            # has none either. With no status known the schedule decides.
            status = (statuses or {}).get(session_data.get("id"))
            met = status is None or status == MET_STATUS
            slot = (schedule or {}).get(session_data.get("id")) if met else None
            start_time = slot["start"] if slot else None
            duration = slot["duration"] if slot else None
            room = slot["room"] if slot else None

            sessions.append(
                IETFSession(
                    meeting_number=meeting_number,
                    group_acronym=group_acronym,
                    session_id=str(session_id) or f"{group_acronym}-{meeting_number}",
                    name=group_name or session_data.get("name"),
                    start_time=start_time,
                    duration_seconds=self._parse_duration(duration) if duration else None,
                    room=room,
                    status=status,
                )
            )

        return sessions

    def get_meeting_sessions(self, meeting_number: MeetingNumber) -> list[IETFSession]:
        """Get all sessions for an IETF meeting.

        Note: This fetches sessions with minimal detail for listing purposes.
        For full session data, use get_group_sessions with a specific group.
        A failed read, or a session whose group cannot be read, raises
        DatatrackerError; an empty list means the Datatracker lists none.

        Args:
            meeting_number: IETF meeting number
        """
        sessions = []
        all_sessions = self._get_all(
            "/api/v1/meeting/session/", {"meeting__number": meeting_number}
        )

        for session_data in sorted(all_sessions, key=lambda row: int(row.get("id") or 0)):
            session_id = session_data.get("id") or str(session_data.get("pk", ""))

            group_uri = session_data.get("group")
            if not group_uri:
                raise DatatrackerError(
                    f"IETF {meeting_number}: session {session_id} names no group"
                )
            group_data = self._group_record(group_uri)

            sessions.append(
                IETFSession(
                    meeting_number=meeting_number,
                    group_acronym=group_data.get("acronym") or "",
                    session_id=str(session_id),
                    name=group_data.get("name") or session_data.get("name"),
                    start_time=None,  # Skip for listing
                    duration_seconds=None,
                    room=None,
                )
            )

        for session in sessions:
            if not session.group_acronym:
                raise DatatrackerError(
                    f"IETF {meeting_number}: session {session.session_id} has a group with no acronym"
                )
        return sessions

    def get_session_materials(
        self, meeting_number: MeetingNumber, group_acronym: str
    ) -> list[IETFMaterial]:
        """Get all materials (slides, agendas, etc.) for a group's sessions.

        A failed read of the list or of any document raises DatatrackerError.
        """
        materials = []

        try:
            # Get documents for the session
            data = self._get_all(
                "/api/v1/meeting/sessionpresentation/",
                {"session__meeting__number": meeting_number, "session__group__acronym": group_acronym},
            )

            for item in data:
                doc_uri = item.get("document")
                if not doc_uri:
                    continue

                doc_data = self._get(doc_uri)
                doc_name = doc_data.get("name", "")
                doc_title = doc_data.get("title", doc_name)

                # Determine material type from name
                if "slides" in doc_name:
                    mat_type = "slides"
                    mimetype = "application/pdf"
                elif "agenda" in doc_name:
                    mat_type = "agenda"
                    mimetype = "application/pdf"
                elif "minutes" in doc_name:
                    mat_type = "minutes"
                    mimetype = "application/pdf"
                elif "recording" in doc_name:
                    mat_type = "recording"
                    mimetype = "text/html"
                elif "chatlog" in doc_name:
                    mat_type = "chatlog"
                    mimetype = "text/plain"
                elif "bluesheets" in doc_name:
                    mat_type = "bluesheets"
                    mimetype = "application/pdf"
                else:
                    mat_type = "document"
                    mimetype = "application/pdf"

                # Build material URL. Session materials live under
                # /meeting/{num}/materials/{doc-name}, but a draft or RFC
                # discussed in session is not a meeting material -- it has its
                # own datatracker page, and the /materials/ form 404s.
                if DOC_NAME_RE.match(doc_name):
                    url = f"{BASE_URL}/doc/{doc_name}/"
                    mat_type, mimetype, landing_page = "document", "text/html", True
                else:
                    url = f"{BASE_URL}/meeting/{meeting_number}/materials/{doc_name}"
                    landing_page = False

                # For recordings, try to get the external URL
                external_url = doc_data.get("external_url")

                # The document record names the published file. Trust it over
                # the extension guess below, which cannot tell a deck published
                # as pdf from the pptx sitting beside it.
                uploaded_filename = None if landing_page else doc_data.get("uploaded_filename")
                # Cloudflare injects a per-request token into every HTML
                # response (window.__CF$cv$params={r:...,t:...}), so an IETF
                # HTML page returns different bytes on every fetch and no
                # content_hash can describe it. Treat it as the landing page it
                # effectively is.
                if uploaded_filename and uploaded_filename.lower().endswith((".htm", ".html")):
                    landing_page = True
                    uploaded_filename = None
                file_url = (
                    proceedings_url(meeting_number, mat_type, uploaded_filename)
                    if uploaded_filename
                    else None
                )

                materials.append(
                    IETFMaterial(
                        type=mat_type,
                        title=doc_title,
                        url=external_url or url,
                        filename=(
                            None
                            if landing_page
                            else (
                                Path(uploaded_filename).name
                                if uploaded_filename
                                else (
                                    f"{doc_name}.pdf"
                                    if mimetype == "application/pdf"
                                    else doc_name
                                )
                            )
                        ),
                        mimetype=mimetype,
                        order=item.get("order"),
                        landing_page=landing_page,
                        uploaded_filename=uploaded_filename,
                        file_url=file_url,
                    )
                )

        except DatatrackerError as e:
            # A partial list would drop materials silently.
            raise DatatrackerError(
                f"could not read the materials of {group_acronym} at IETF {meeting_number}: {e}"
            ) from e

        # Also add the session page, which carries the agenda. /meeting/N/agenda/<wg>/
        # looks plausible and 404s; the real page is /meeting/N/session/<wg>/.
        agenda_url = f"{BASE_URL}/meeting/{meeting_number}/session/{group_acronym}/"
        materials.append(
            IETFMaterial(
                type="agenda",
                title=f"{group_acronym.upper()} Agenda",
                url=agenda_url,
                mimetype="text/html",
                landing_page=True,
            )
        )

        # Add notes URL (collaborative notes)
        notes_url = f"https://notes.ietf.org/notes-ietf-{meeting_number}-{group_acronym}"
        materials.append(
            IETFMaterial(
                type="minutes",
                title=f"{group_acronym.upper()} Notes",
                url=notes_url,
                mimetype="text/markdown",
                landing_page=True,
            )
        )

        return materials

    # Chair lookups are cached for the life of the process, shared by every
    # client: the converter makes a new client per session, and a meeting has
    # several sessions per group. Only successful reads are cached.
    _chair_cache: dict[tuple, Any] = {}

    # True after a call whose dated lookup was dropped because the history is
    # ambiguous (tied snapshots hold different chairs).
    last_chair_ambiguous: bool = False

    @classmethod
    def clear_chair_cache(cls) -> None:
        """Forget cached group, history, role, person and email reads."""
        cls._chair_cache.clear()

    def _cached(self, key: tuple, fetch):
        cache = DataTrackerClient._chair_cache
        if key not in cache:
            cache[key] = fetch()
        return cache[key]

    @staticmethod
    def _aware_utc(value: datetime | date) -> datetime:
        """A cutoff as an aware UTC datetime.

        A bare date means the end of that day. A naive datetime is taken to be
        UTC, as the Datatracker's own timestamps are.
        """
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=UTC)

    @staticmethod
    def _api_time(text: str | None) -> datetime | None:
        """An API timestamp ("2025-05-14T06:23:01Z") as an aware UTC datetime.

        The Datatracker writes group and history times with a trailing Z; one
        without an offset is taken to be UTC.
        """
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    def _read_group(self, group_acronym: str) -> dict:
        """The live group record (numeric id and last-edit time).

        A group the Datatracker does not know, or a failed read, raises
        DatatrackerError.
        """
        def fetch():
            try:
                objects = self._get(
                    "/api/v1/group/group/", {"acronym": group_acronym}
                ).get("objects", [])
            except DatatrackerError as e:
                raise DatatrackerError(f"could not read group {group_acronym}: {e}") from e
            if not objects:
                raise DatatrackerError(f"no group with acronym {group_acronym}")
            return objects[0]

        return self._cached(("group", group_acronym), fetch)

    def _read_snapshots(self, group_id: int) -> list[dict]:
        """Every history snapshot of a group, paged until all are read.

        Filtered by group id, not acronym: the acronym on a snapshot is the one
        in force when it was taken, so an acronym filter misses a renamed
        group. The resource has no defined default order, so a truncated page
        cannot be trusted.
        """
        def fetch():
            return self._get_all("/api/v1/group/grouphistory/", {"group": group_id})

        return self._cached(("snapshots", group_id), fetch)

    def _read_chair_roles(self, resource: str, params: dict, key: tuple) -> list[dict]:
        """Chair role rows, ascending by role id."""
        def fetch():
            rows = self._get(
                resource,
                {"name__slug": "chair", "order_by": "id", "limit": 50, **params},
            ).get("objects", [])
            return sorted(rows, key=lambda r: int(r.get("id", 0)))

        return self._cached(key, fetch)

    @staticmethod
    def _chair_people(rows: list[dict]) -> frozenset:
        """The set of chair people in role rows, by person, never by email or name."""
        return frozenset(r.get("person") for r in rows if r.get("person"))

    def _ties_disagree(
        self, group_acronym: str, cutoff: datetime, source: str, when: datetime,
        tied_ids: list[int], reference: frozenset | None = None,
    ) -> bool:
        """Do snapshots sharing one `time` hold different chair people?

        Some groups had their chairs changed through older tools that wrote a
        snapshot without moving the group's own `time`. Several snapshots then
        carry the same `time` but each holds whoever held the role when it was
        written, so the `time` says nothing about when those people served and
        the tie-break would pick one arbitrarily. Compared with ``reference``
        (the live chair people) if given, else with each other. Reads happen
        through the cache, so a group pays for them once. A failed read raises
        and is a lookup failure, not "not ambiguous".
        """
        sets = [
            self._chair_people(self._read_chair_roles(
                "/api/v1/group/rolehistory/", {"group": sid}, ("snapshot", sid)
            ))
            for sid in tied_ids
        ]
        if reference is not None:
            sets.append(reference)
        if len(set(sets)) <= 1:
            return False
        self.last_chair_ambiguous = True
        logger.warning(
            "Chairs for %s at %s: %s is ambiguous; snapshots %s share time %s with "
            "different chairs; omitting chairs",
            group_acronym, cutoff.isoformat(), source,
            ", ".join(str(i) for i in sorted(tied_ids)), when.isoformat(),
        )
        return True

    def _chair_roles_at(
        self, group_acronym: str, session_start: datetime | date
    ) -> list[dict]:
        """Chair role records in force at a given moment, or None.

        The Datatracker writes a group-history snapshot as a copy of the state
        an edit is about to replace, and the snapshot's `time` is when that
        state BEGAN. So a snapshot is in force from its `time` until the next
        snapshot's `time`; the newest snapshot until the group's own `time`
        (the latest edit); and from `group.time` onward the live role table is
        the truth, which no snapshot holds. This is how the Datatracker's own
        "active at" helper resolves history.

        Returns the live roles when `group.time <= cutoff`, else the roles of
        the newest snapshot with `time <= cutoff` (ties broken by the highest
        integer id). Returns an empty list when the history was read but names
        no chair for that moment: the group was created later, or the moment
        predates the recorded history. Also an empty list, with a warning and
        ``last_chair_ambiguous`` set, when the source is ambiguous: snapshots
        that share the chosen snapshot's `time` (or, for live roles, the
        group's `time`) hold different chair people. A group, snapshot list or
        role read that fails raises DatatrackerError.
        """
        self.last_chair_ambiguous = False
        cutoff = self._aware_utc(session_start)
        group = self._read_group(group_acronym)
        group_time = self._api_time(group.get("time"))
        try:
            group_id = int(group["id"])
        except (KeyError, TypeError, ValueError):
            raise DatatrackerError(f"group record for {group_acronym} has no numeric id")
        if group_time is None:
            raise DatatrackerError(f"group {group_acronym} has no usable time")

        try:
            if group_time <= cutoff:
                roles = self._read_chair_roles(
                    "/api/v1/group/role/", {"group": group_id}, ("live", group_id)
                )
                tied = [
                    int(snap["id"]) for snap in self._read_snapshots(group_id)
                    if self._api_time(snap.get("time")) == group_time
                ]
                if tied and self._ties_disagree(
                    group_acronym, cutoff, "live roles", group_time, tied,
                    reference=self._chair_people(roles),
                ):
                    return []
                logger.info(
                    "Chairs for %s at %s: live roles, group last edited %s",
                    group_acronym, cutoff.isoformat(), group_time.isoformat(),
                )
                return roles

            dated = []
            for snapshot in self._read_snapshots(group_id):
                when = self._api_time(snapshot.get("time"))
                if when is None:
                    logger.warning(
                        "Ignoring %s snapshot %s with no usable time",
                        group_acronym, snapshot.get("id"),
                    )
                    continue
                dated.append((when, int(snapshot["id"]), snapshot))
            dated.sort(key=lambda item: (item[0], item[1]), reverse=True)
            in_force = next((item for item in dated if item[0] <= cutoff), None)
            if in_force is None:
                logger.info(
                    "No %s chairs at %s: no history snapshot at or before it "
                    "(group last edited %s)",
                    group_acronym, cutoff.isoformat(), group_time.isoformat(),
                )
                return []
            when, snapshot_id, _ = in_force
            roles = self._read_chair_roles(
                "/api/v1/group/rolehistory/", {"group": snapshot_id},
                ("snapshot", snapshot_id),
            )
            tied = [sid for ts, sid, _ in dated if ts == when]
            if len(tied) > 1 and self._ties_disagree(
                group_acronym, cutoff, f"snapshot {snapshot_id}", when, tied
            ):
                return []
        except DatatrackerError as e:
            raise DatatrackerError(
                f"could not read the chair history of {group_acronym}: {e}"
            ) from e

        logger.info(
            "Chairs for %s at %s: snapshot %s from %s",
            group_acronym, cutoff.isoformat(), snapshot_id, when.isoformat(),
        )
        return roles

    def get_group_chairs(
        self, group_acronym: str, session_date: datetime | date | None = None
    ) -> list[IETFPerson]:
        """Get the chairs of a working group, as of `session_date` if given.

        Pass the session's start *time* where it is known: chair handovers
        happen mid-meeting, so a date alone can pick up a change that happened
        hours after the session ended.

        Without a date this returns whoever chairs the group *now*, which is
        wrong for any historical session: a 2014 session recorded with 2026's
        chairs credits people who were not in the room. With a date, the empty
        list is a definitive answer (no history at or before the time, or an
        ambiguous history), and any read that fails, including a chair's
        person record or address, raises DatatrackerError: a chair dropped or
        left without an address would shape the file silently.
        """
        chairs = []
        seen_people: dict[str, Any] = {}
        self.last_chair_ambiguous = False

        if session_date:
            items = self._chair_roles_at(group_acronym, session_date)
        else:
            items = self._get(
                "/api/v1/group/role/",
                {
                    "group__acronym": group_acronym,
                    "name__slug": "chair",
                    "order_by": "id",
                    "limit": 10,
                },
            ).get("objects", [])
        if not items:
            return []  # history read, no chair for this session: no chairs is the truth

        for item in sorted(items, key=lambda r: int(r.get("id", 0))):
            person_uri = item.get("person")
            if not person_uri:
                continue

            # One person can hold the chair role in several rows (one per
            # address). The lowest role id is kept.
            if person_uri in seen_people:
                logger.info(
                    "Dropping duplicate chair role %s of %s (person %s, kept role %s)",
                    item.get("id"), group_acronym, person_uri, seen_people[person_uri],
                )
                continue
            seen_people[person_uri] = item.get("id")

            try:
                person_data = self._cached(
                    ("person", person_uri), lambda uri=person_uri: self._get(uri)
                )
                email = None
                email_uri = item.get("email")
                if email_uri:
                    email_data = self._cached(
                        ("email", email_uri), lambda uri=email_uri: self._get(uri)
                    )
                    email = email_data.get("address")
            except DatatrackerError as e:
                raise DatatrackerError(
                    f"could not read a chair of {group_acronym} ({person_uri}): {e}"
                ) from e

            chairs.append(
                IETFPerson(
                    name=person_data.get("name", "Unknown"),
                    email=email,
                    role="chair",
                )
            )

        return chairs

    def get_recording_url(self, meeting_number: MeetingNumber, group_acronym: str) -> str | None:
        """Get the Meetecho recording URL for a session."""
        # Meetecho recordings follow a predictable pattern
        # https://meetings.conf.meetecho.com/ietf{num}/?session={session-id}
        return f"https://meetings.conf.meetecho.com/ietf{meeting_number}/?group={group_acronym}"

    def get_youtube_playlist_url(self, meeting_number: MeetingNumber) -> str:
        """Get the YouTube playlist URL for an IETF meeting."""
        return f"https://www.youtube.com/playlist?list=PLC86T-6ZTP5g-mLpb6ER0j63i8yD6dDNq"

    def _parse_date(self, date_str: str | None) -> datetime | None:
        """Parse a date string from the API."""
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            return None

    def _parse_datetime(self, dt_str: str | None) -> datetime | None:
        """Parse a datetime string from the API."""
        if not dt_str:
            return None
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except Exception:
            return None

    def _parse_duration(self, duration_str: str) -> int | None:
        """Parse a duration string (HH:MM:SS) to seconds."""
        try:
            parts = duration_str.split(":")
            if len(parts) == 3:
                h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
                return h * 3600 + m * 60 + s
            elif len(parts) == 2:
                m, s = int(parts[0]), int(parts[1])
                return m * 60 + s
        except Exception:
            pass
        return None
