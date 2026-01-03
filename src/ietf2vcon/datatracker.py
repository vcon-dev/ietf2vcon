"""IETF Datatracker API client.

Provides access to IETF meeting metadata, sessions, materials, and more.
API documentation: https://datatracker.ietf.org/api/
"""

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import IETFMaterial, IETFMeeting, IETFPerson, IETFSession

logger = logging.getLogger(__name__)

BASE_URL = "https://datatracker.ietf.org"
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
    ) -> list[IETFSession]:
        """Get sessions for a specific working group at a meeting.

        This method queries the API directly for the specific group,
        avoiding the need to fetch all sessions.
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

                # Build material URL
                # Materials are at /meeting/{num}/materials/{doc-name}
                url = f"{BASE_URL}/meeting/{meeting_number}/materials/{doc_name}"

                # For recordings, try to get the external URL
                external_url = doc_data.get("external_url")

                materials.append(
                    IETFMaterial(
                        type=mat_type,
                        title=doc_title,
                        url=external_url or url,
                        filename=f"{doc_name}.pdf" if mimetype == "application/pdf" else doc_name,
                        mimetype=mimetype,
                        order=item.get("order"),
                    )
                )

        except Exception as e:
            logger.error(f"Failed to get materials for {group_acronym} at {meeting_number}: {e}")

        # Also add agenda URL
        agenda_url = f"{BASE_URL}/meeting/{meeting_number}/agenda/{group_acronym}/"
        materials.append(
            IETFMaterial(
                type="agenda",
                title=f"{group_acronym.upper()} Agenda",
                url=agenda_url,
                mimetype="text/html",
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
            )
        )

        return materials

    def get_group_chairs(self, group_acronym: str) -> list[IETFPerson]:
        """Get current chairs for a working group."""
        chairs = []
        seen_names = set()

        try:
            # Get current role holders (not history)
            data = self._get(
                "/api/v1/group/role/",
                {
                    "group__acronym": group_acronym,
                    "name__slug": "chair",
                    "limit": 10,
                },
            )

            for item in data.get("objects", []):
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
