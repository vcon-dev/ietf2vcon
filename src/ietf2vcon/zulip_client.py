"""Zulip chat client for IETF meeting chat logs.

The IETF uses Zulip at https://zulip.ietf.org for meeting chat.
API documentation: https://zulip.com/api/
"""

import logging
from datetime import datetime
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import ChatMessage

logger = logging.getLogger(__name__)

IETF_ZULIP_URL = "https://zulip.ietf.org"


class ZulipClient:
    """Client for the IETF Zulip chat server.

    Authentication is via Datatracker credentials.
    To use the API, you need to generate an API key at:
    https://zulip.ietf.org/#settings/account-and-privacy
    """

    def __init__(
        self,
        email: str | None = None,
        api_key: str | None = None,
        base_url: str = IETF_ZULIP_URL,
    ):
        """Initialize Zulip client.

        Args:
            email: Zulip account email (Datatracker email)
            api_key: Zulip API key
            base_url: Zulip server URL
        """
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_key = api_key
        self._client = None

    @property
    def client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._client is None:
            auth = None
            if self.email and self.api_key:
                auth = (self.email, self.api_key)

            self._client = httpx.Client(
                base_url=self.base_url,
                auth=auth,
                timeout=30.0,
            )
        return self._client

    def close(self):
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def _get(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        """Make an authenticated GET request."""
        response = self.client.get(f"/api/v1/{endpoint}", params=params)
        response.raise_for_status()
        return response.json()

    def get_streams(self) -> list[dict[str, Any]]:
        """Get list of all streams (channels)."""
        result = self._get("streams")
        return result.get("streams", [])

    def get_stream_id(self, stream_name: str) -> int | None:
        """Get the stream ID for a stream by name.

        Args:
            stream_name: Name of the stream (usually WG acronym)

        Returns:
            Stream ID or None if not found
        """
        result = self._get("get_stream_id", {"stream": stream_name})
        return result.get("stream_id")

    def get_messages(
        self,
        stream_name: str,
        topic: str | None = None,
        num_messages: int = 1000,
        anchor: str = "newest",
    ) -> list[ChatMessage]:
        """Get messages from a stream.

        Args:
            stream_name: Name of the stream
            topic: Optional topic to filter by
            num_messages: Maximum number of messages to fetch
            anchor: Where to start (newest, oldest, or message ID)

        Returns:
            List of ChatMessage objects
        """
        # Build narrow filter
        narrow = [{"operator": "stream", "operand": stream_name}]
        if topic:
            narrow.append({"operator": "topic", "operand": topic})

        import json
        params = {
            "anchor": anchor,
            "num_before": num_messages if anchor == "newest" else 0,
            "num_after": 0 if anchor == "newest" else num_messages,
            "narrow": json.dumps(narrow),
        }

        result = self._get("messages", params)
        messages = []

        for msg in result.get("messages", []):
            timestamp = datetime.fromtimestamp(msg.get("timestamp", 0))
            messages.append(
                ChatMessage(
                    timestamp=timestamp,
                    sender=msg.get("sender_full_name", "Unknown"),
                    sender_email=msg.get("sender_email"),
                    content=msg.get("content", ""),
                    topic=msg.get("subject"),
                    stream=stream_name,
                )
            )

        # Sort by timestamp (oldest first)
        messages.sort(key=lambda m: m.timestamp)
        return messages

    def get_session_messages(
        self,
        meeting_number: int,
        group_acronym: str,
        session_start: datetime | None = None,
        session_end: datetime | None = None,
    ) -> list[ChatMessage]:
        """Get chat messages for an IETF session.

        This searches the WG stream for messages during the session time.

        Args:
            meeting_number: IETF meeting number
            group_acronym: Working group acronym
            session_start: Session start time
            session_end: Session end time

        Returns:
            List of ChatMessage objects from the session
        """
        # IETF Zulip streams are typically named after the WG acronym
        stream_name = group_acronym.lower()

        # Try to get messages from the stream
        try:
            messages = self.get_messages(stream_name, num_messages=5000)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Stream '{stream_name}' not found")
                return []
            raise

        # Filter by session time if provided
        if session_start and session_end:
            messages = [
                m for m in messages
                if session_start <= m.timestamp <= session_end
            ]
        elif session_start:
            # Get messages after session start
            messages = [m for m in messages if m.timestamp >= session_start]

        return messages

    def search_messages(
        self,
        query: str,
        stream_name: str | None = None,
    ) -> list[ChatMessage]:
        """Search for messages matching a query.

        Args:
            query: Search query string
            stream_name: Optional stream to limit search

        Returns:
            List of matching ChatMessage objects
        """
        narrow = []
        if stream_name:
            narrow.append({"operator": "stream", "operand": stream_name})

        narrow.append({"operator": "search", "operand": query})

        import json
        params = {
            "anchor": "newest",
            "num_before": 100,
            "num_after": 0,
            "narrow": json.dumps(narrow),
        }

        result = self._get("messages", params)
        messages = []

        for msg in result.get("messages", []):
            timestamp = datetime.fromtimestamp(msg.get("timestamp", 0))
            messages.append(
                ChatMessage(
                    timestamp=timestamp,
                    sender=msg.get("sender_full_name", "Unknown"),
                    sender_email=msg.get("sender_email"),
                    content=msg.get("content", ""),
                    topic=msg.get("subject"),
                    stream=msg.get("display_recipient"),
                )
            )

        return messages


def chat_messages_to_text(messages: list[ChatMessage]) -> str:
    """Convert chat messages to plain text format.

    Args:
        messages: List of chat messages

    Returns:
        Plain text representation of the chat log
    """
    lines = []
    for msg in messages:
        timestamp = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{timestamp}] {msg.sender}: {msg.content}")

    return "\n".join(lines)


def chat_messages_to_json(messages: list[ChatMessage]) -> list[dict]:
    """Convert chat messages to JSON-serializable format.

    Args:
        messages: List of chat messages

    Returns:
        List of message dictionaries
    """
    return [
        {
            "timestamp": msg.timestamp.isoformat(),
            "sender": msg.sender,
            "sender_email": msg.sender_email,
            "content": msg.content,
            "topic": msg.topic,
            "stream": msg.stream,
        }
        for msg in messages
    ]
