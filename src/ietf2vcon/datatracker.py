"""IETF Datatracker API client.

Provides access to IETF meeting metadata, sessions, materials, and more.
API documentation: https://datatracker.ietf.org/api/
"""

import logging
import re
from pathlib import Path
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import IETFMaterial, IETFMeeting, IETFPerson, IETFSession

logger = logging.getLogger(__name__)

BASE_URL = "https://datatracker.ietf.org"

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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def _get(self, url: str, params: dict | None = None) -> dict[str, Any]:
        """Make a GET request with retry logic."""
        response = self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def _get_paginated(self, url: str, params: dict | None = None) -> list[dict[str, Any]]:
        """Get all results from a paginated API endpoint."""
        results = []
        params = params or {}
        params["limit"] = 100

        while url:
            data = self._get(url, params)
            results.extend(data.get("objects", []))

            # Get next page URL
            meta = data.get("meta", {})
            next_url = meta.get("next")
            if next_url:
                url = next_url
                params = None  # params are in the URL now
            else:
                break

        return results

    def get_meeting(self, meeting_number: int) -> IETFMeeting | None:
        """Get metadata for an IETF meeting by number."""
        try:
            data = self._get(f"/api/v1/meeting/meeting/", {"number": meeting_number})
            objects = data.get("objects", [])
            if not objects:
                return None

            meeting = objects[0]
            return IETFMeeting(
                number=meeting_number,
                city=meeting.get("city"),
                country=meeting.get("country"),
                start_date=self._parse_date(meeting.get("date")),
                time_zone=meeting.get("time_zone"),
            )
        except Exception as e:
            logger.error(f"Failed to get meeting {meeting_number}: {e}")
            return None

    def get_group_sessions(
        self, meeting_number: int, group_acronym: str
    ) -> list[IETFSession] | None:
        """Get sessions for a specific working group at a meeting.

        This method queries the API directly for the specific group,
        avoiding the need to fetch all sessions.

        Returns None if the lookup failed, as distinct from an empty list,
        which means the group genuinely did not meet. Callers must not treat
        a failed lookup as "no sessions" -- see the caller in converter.py.
        """
        sessions = []
        try:
            # Query sessions directly filtered by group and meeting
            data = self._get_paginated(
                "/api/v1/meeting/session/",
                {
                    "meeting__number": meeting_number,
                    "group__acronym": group_acronym,
                },
            )

            for session_data in data:
                session_id = session_data.get("id") or str(session_data.get("pk", ""))

                # Get group name
                group_uri = session_data.get("group")
                group_name = None
                if group_uri:
                    try:
                        group_data = self._get(group_uri)
                        group_name = group_data.get("name")
                    except Exception:
                        pass

                # Get scheduled time from assignment
                start_time = None
                duration = None
                room = None

                # Try to get schedule assignment for this session
                try:
                    assignments = self._get(
                        "/api/v1/meeting/schedtimesessassignment/",
                        {
                            "session": session_data.get("id"),
                            "schedule__meeting__number": meeting_number,
                            "limit": 1,
                        },
                    )
                    if assignments.get("objects"):
                        assignment = assignments["objects"][0]
                        timeslot_uri = assignment.get("timeslot")
                        if timeslot_uri:
                            timeslot = self._get(timeslot_uri)
                            start_time = self._parse_datetime(timeslot.get("time"))
                            duration = timeslot.get("duration")
                            location_uri = timeslot.get("location")
                            if location_uri:
                                try:
                                    location = self._get(location_uri)
                                    room = location.get("name")
                                except Exception:
                                    pass
                except Exception as e:
                    logger.debug(f"Could not get schedule for session: {e}")

                sessions.append(
                    IETFSession(
                        meeting_number=meeting_number,
                        group_acronym=group_acronym,
                        session_id=str(session_id) or f"{group_acronym}-{meeting_number}",
                        name=group_name or session_data.get("name"),
                        start_time=start_time,
                        duration_seconds=self._parse_duration(duration) if duration else None,
                        room=room,
                    )
                )

        except Exception as e:
            logger.error(f"Failed to get sessions for {group_acronym} at {meeting_number}: {e}")
            return None

        return sessions

    def get_meeting_sessions(self, meeting_number: int) -> list[IETFSession]:
        """Get all sessions for an IETF meeting.

        Note: This fetches sessions with minimal detail for listing purposes.
        For full session data, use get_group_sessions with a specific group.

        Args:
            meeting_number: IETF meeting number
        """
        sessions = []
        try:
            # Get all sessions using pagination
            all_sessions = self._get_paginated(
                "/api/v1/meeting/session/",
                {
                    "meeting__number": meeting_number,
                },
            )

            for session_data in all_sessions:
                session_id = session_data.get("id") or str(session_data.get("pk", ""))

                # Get group info from the URI
                group_uri = session_data.get("group")
                group_acronym = "unknown"
                group_name = None

                if group_uri:
                    # Extract acronym from URI path: /api/v1/group/group/xxx/
                    # We'll fetch it to be accurate
                    try:
                        group_data = self._get(group_uri)
                        group_acronym = group_data.get("acronym", "unknown")
                        group_name = group_data.get("name")
                    except Exception:
                        pass

                sessions.append(
                    IETFSession(
                        meeting_number=meeting_number,
                        group_acronym=group_acronym,
                        session_id=str(session_id),
                        name=group_name or session_data.get("name"),
                        start_time=None,  # Skip for listing
                        duration_seconds=None,
                        room=None,
                    )
                )

        except Exception as e:
            logger.error(f"Failed to get sessions for meeting {meeting_number}: {e}")

        return sessions

    def get_session_materials(
        self, meeting_number: int, group_acronym: str
    ) -> list[IETFMaterial]:
        """Get all materials (slides, agendas, etc.) for a session."""
        materials = []

        try:
            # Get documents for the session
            data = self._get_paginated(
                f"/api/v1/meeting/sessionpresentation/",
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

        except Exception as e:
            logger.error(f"Failed to get materials for {group_acronym} at {meeting_number}: {e}")

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

    def _chair_roles_at(
        self, group_acronym: str, session_start: datetime | date
    ) -> list[dict] | None:
        """Chair role records as they stood on a given date, or None.

        The Datatracker keeps a snapshot of each group in `grouphistory` every
        time the group record changes, with `rolehistory` rows hanging off each
        snapshot. So the chairs of a 2014 session are the chairs recorded on the
        newest snapshot that is not newer than the session.

        Returns an empty list when the session predates the group's role
        history (nothing before 2011-12-09 is resolvable), and None when the
        history could not be read at all -- the caller falls back to current
        roles only in the second case.
        """
        try:
            snapshots = self._get(
                "/api/v1/group/grouphistory/",
                {
                    "acronym": group_acronym,
                    "limit": 1000,
                },
            ).get("objects", [])
        except Exception as e:
            logger.warning("Could not read group history for %s: %s", group_acronym, e)
            return None
        if not snapshots:
            return None

        # Compare full timestamps, not just dates. Chair handovers happen at
        # the plenary, mid-meeting: the IAB chair changed from Tommy Pauly to
        # Dhruv Dhody on 2026-03-16, the same day as IETF 125's IAB session.
        # A date-only comparison lets that afternoon's snapshot claim a session
        # that ran before it.
        cutoff = session_start.isoformat()
        precision = len(cutoff) if isinstance(session_start, datetime) else 10
        earlier = [
            s for s in snapshots
            if s.get("time", "").replace("Z", "+00:00")[:precision] <= cutoff[:precision]
        ]
        if not earlier:
            # Role history begins 2011-12-09 for every group, when the feature
            # was switched on, so nothing before IETF 83 is resolvable. Report
            # that rather than substituting a later snapshot: naming a 2026
            # chair on a 2006 session is a fabrication, and an absent chair is
            # the honest record.
            logger.info(
                "No %s role history at or before %s; leaving chairs unset",
                group_acronym, cutoff,
            )
            return []
        snapshot = max(earlier, key=lambda s: s["time"])

        snapshot_id = snapshot["resource_uri"].rstrip("/").split("/")[-1]
        try:
            roles = self._get(
                "/api/v1/group/rolehistory/",
                {
                    "group": snapshot_id,
                    "name__slug": "chair",
                    "limit": 50,
                },
            ).get("objects", [])
        except Exception as e:
            logger.warning("Could not read role history for %s: %s", group_acronym, e)
            return None

        if roles:
            logger.info(
                "Chairs for %s from the %s snapshot (session %s)",
                group_acronym, snapshot["time"][:10], cutoff,
            )
        return roles or None

    def get_group_chairs(
        self, group_acronym: str, session_date: datetime | date | None = None
    ) -> list[IETFPerson]:
        """Get the chairs of a working group, as of `session_date` if given.

        Pass the session's start *time* where it is known: chair handovers
        happen mid-meeting, so a date alone can pick up a change that happened
        hours after the session ended.

        Without a date this returns whoever chairs the group *now*, which is
        wrong for any historical session: a 2014 session recorded with 2026's
        chairs credits people who were not in the room.
        """
        chairs = []
        seen_names = set()

        try:
            items = self._chair_roles_at(group_acronym, session_date) if session_date else None
            if items == []:
                return []  # session predates role history; no chairs is the truth
            if items is None:
                if session_date:
                    logger.warning(
                        "Could not read role history for %s; falling back to current chairs",
                        group_acronym,
                    )
                items = self._get(
                    "/api/v1/group/role/",
                    {
                        "group__acronym": group_acronym,
                        "name__slug": "chair",
                        "limit": 10,
                    },
                ).get("objects", [])

            for item in items:
                person_uri = item.get("person")
                if not person_uri:
                    continue

                try:
                    person_data = self._get(person_uri)
                    name = person_data.get("name", "Unknown")

                    # Avoid duplicates
                    if name in seen_names:
                        continue
                    seen_names.add(name)

                    email_uri = item.get("email")
                    email = None
                    if email_uri:
                        try:
                            email_data = self._get(email_uri)
                            email = email_data.get("address")
                        except Exception:
                            pass

                    chairs.append(
                        IETFPerson(
                            name=name,
                            email=email,
                            role="chair",
                        )
                    )
                except Exception as e:
                    logger.debug(f"Could not fetch person data: {e}")

        except Exception as e:
            logger.error(f"Failed to get chairs for {group_acronym}: {e}")

        return chairs

    def get_recording_url(self, meeting_number: int, group_acronym: str) -> str | None:
        """Get the Meetecho recording URL for a session."""
        # Meetecho recordings follow a predictable pattern
        # https://meetings.conf.meetecho.com/ietf{num}/?session={session-id}
        return f"https://meetings.conf.meetecho.com/ietf{meeting_number}/?group={group_acronym}"

    def get_youtube_playlist_url(self, meeting_number: int) -> str:
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
