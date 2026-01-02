"""YouTube video resolver and downloader for IETF recordings.

IETF meeting recordings are published to the official IETF YouTube channel:
https://www.youtube.com/@ietf
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

IETF_YOUTUBE_CHANNEL = "https://www.youtube.com/@ietf"
IETF_YOUTUBE_PLAYLISTS = "https://www.youtube.com/user/ietf/playlists"


@dataclass
class VideoMetadata:
    """Metadata for a YouTube video."""

    video_id: str
    title: str
    url: str
    duration_seconds: int | None = None
    upload_date: str | None = None
    description: str | None = None
    thumbnail_url: str | None = None


class YouTubeResolver:
    """Resolve and fetch IETF meeting videos from YouTube."""

    def __init__(self, download_dir: Path | None = None):
        self.download_dir = download_dir or Path("./downloads")
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def search_session_video(
        self,
        meeting_number: int,
        group_acronym: str,
        session_date: str | None = None,
    ) -> VideoMetadata | None:
        """Search for a session video on the IETF YouTube channel.

        Args:
            meeting_number: IETF meeting number (e.g., 124)
            group_acronym: Working group acronym (e.g., "vcon")
            session_date: Optional date string to help match (YYYY-MM-DD)

        Returns:
            VideoMetadata if found, None otherwise
        """
        # Build search query
        search_terms = [f"IETF {meeting_number}", group_acronym.upper()]
        if session_date:
            search_terms.append(session_date)

        search_query = " ".join(search_terms)
        logger.info(f"Searching YouTube for: {search_query}")

        try:
            # Use yt-dlp to search
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--flat-playlist",
                    "--print", "%(id)s|%(title)s|%(duration)s|%(upload_date)s",
                    f"ytsearch5:{search_query} site:youtube.com/ietf",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                logger.warning(f"yt-dlp search failed: {result.stderr}")
                return None

            # Parse results and find best match
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue

                parts = line.split("|")
                if len(parts) < 2:
                    continue

                video_id = parts[0]
                title = parts[1]
                duration = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
                upload_date = parts[3] if len(parts) > 3 else None

                # Check if title matches our session
                if self._title_matches_session(title, meeting_number, group_acronym):
                    return VideoMetadata(
                        video_id=video_id,
                        title=title,
                        url=f"https://www.youtube.com/watch?v={video_id}",
                        duration_seconds=duration,
                        upload_date=upload_date,
                    )

        except subprocess.TimeoutExpired:
            logger.error("YouTube search timed out")
        except FileNotFoundError:
            logger.error("yt-dlp not found. Install with: pip install yt-dlp")
        except Exception as e:
            logger.error(f"YouTube search failed: {e}")

        return None

    def get_video_metadata(self, video_url: str) -> VideoMetadata | None:
        """Get metadata for a specific YouTube video.

        Args:
            video_url: YouTube video URL or video ID

        Returns:
            VideoMetadata if successful, None otherwise
        """
        try:
            # Extract video ID if full URL
            video_id = self._extract_video_id(video_url)
            if not video_id:
                video_id = video_url

            result = subprocess.run(
                [
                    "yt-dlp",
                    "--print", "%(id)s",
                    "--print", "%(title)s",
                    "--print", "%(duration)s",
                    "--print", "%(upload_date)s",
                    "--print", "%(description)s",
                    "--print", "%(thumbnail)s",
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.warning(f"Failed to get video metadata: {result.stderr}")
                return None

            lines = result.stdout.strip().split("\n")
            if len(lines) < 2:
                return None

            return VideoMetadata(
                video_id=lines[0],
                title=lines[1],
                url=f"https://www.youtube.com/watch?v={lines[0]}",
                duration_seconds=int(lines[2]) if len(lines) > 2 and lines[2].isdigit() else None,
                upload_date=lines[3] if len(lines) > 3 else None,
                description=lines[4] if len(lines) > 4 else None,
                thumbnail_url=lines[5] if len(lines) > 5 else None,
            )

        except Exception as e:
            logger.error(f"Failed to get video metadata: {e}")
            return None

    def download_video(
        self,
        video_url: str,
        output_filename: str | None = None,
        format_spec: str = "best[height<=1080]",
    ) -> Path | None:
        """Download a video from YouTube.

        Args:
            video_url: YouTube video URL
            output_filename: Optional custom filename
            format_spec: yt-dlp format specification

        Returns:
            Path to downloaded file, or None if failed
        """
        try:
            video_id = self._extract_video_id(video_url) or video_url

            output_template = str(self.download_dir / (output_filename or "%(title)s.%(ext)s"))

            result = subprocess.run(
                [
                    "yt-dlp",
                    "-f", format_spec,
                    "-o", output_template,
                    "--print", "after_move:filepath",
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour timeout for large videos
            )

            if result.returncode != 0:
                logger.error(f"Video download failed: {result.stderr}")
                return None

            output_path = result.stdout.strip().split("\n")[-1]
            return Path(output_path) if output_path else None

        except subprocess.TimeoutExpired:
            logger.error("Video download timed out")
        except Exception as e:
            logger.error(f"Video download failed: {e}")

        return None

    def download_audio(
        self,
        video_url: str,
        output_filename: str | None = None,
    ) -> Path | None:
        """Download only the audio track from a YouTube video.

        Args:
            video_url: YouTube video URL
            output_filename: Optional custom filename

        Returns:
            Path to downloaded audio file, or None if failed
        """
        try:
            video_id = self._extract_video_id(video_url) or video_url

            output_template = str(
                self.download_dir / (output_filename or "%(title)s.%(ext)s")
            )

            result = subprocess.run(
                [
                    "yt-dlp",
                    "-x",  # Extract audio
                    "--audio-format", "mp3",
                    "--audio-quality", "0",  # Best quality
                    "-o", output_template,
                    "--print", "after_move:filepath",
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                capture_output=True,
                text=True,
                timeout=1800,  # 30 min timeout
            )

            if result.returncode != 0:
                logger.error(f"Audio download failed: {result.stderr}")
                return None

            output_path = result.stdout.strip().split("\n")[-1]
            return Path(output_path) if output_path else None

        except Exception as e:
            logger.error(f"Audio download failed: {e}")
            return None

    def get_meetecho_recording_url(
        self, meeting_number: int, group_acronym: str
    ) -> str:
        """Generate the Meetecho recording URL for a session.

        Args:
            meeting_number: IETF meeting number
            group_acronym: Working group acronym

        Returns:
            Meetecho recording URL
        """
        return f"https://meetings.conf.meetecho.com/ietf{meeting_number}/?group={group_acronym}"

    def download_captions(
        self,
        video_url: str,
        output_filename: str | None = None,
        lang: str = "en",
    ) -> Path | None:
        """Download captions/subtitles from a YouTube video.

        Args:
            video_url: YouTube video URL
            output_filename: Optional custom filename (without extension)
            lang: Language code for captions (default: en)

        Returns:
            Path to downloaded caption file (JSON format), or None if failed
        """
        try:
            video_id = self._extract_video_id(video_url) or video_url
            output_base = output_filename or video_id

            # Create captions directory
            captions_dir = self.download_dir / "captions"
            captions_dir.mkdir(parents=True, exist_ok=True)

            output_template = str(captions_dir / output_base)

            # Try to get auto-generated captions first, then manual
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--write-auto-sub",
                    "--write-sub",
                    "--sub-lang", lang,
                    "--sub-format", "json3",
                    "--skip-download",
                    "-o", output_template,
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                logger.warning(f"Caption download failed: {result.stderr}")
                return None

            # Find the downloaded caption file
            possible_files = [
                captions_dir / f"{output_base}.{lang}.json3",
                captions_dir / f"{output_base}.{lang}.json",
            ]

            for caption_file in possible_files:
                if caption_file.exists():
                    logger.info(f"Downloaded captions: {caption_file}")
                    return caption_file

            # Check for any json3 files that were created
            for f in captions_dir.glob(f"{output_base}*.json3"):
                logger.info(f"Downloaded captions: {f}")
                return f

            logger.warning("No caption file found after download")
            return None

        except subprocess.TimeoutExpired:
            logger.error("Caption download timed out")
        except Exception as e:
            logger.error(f"Caption download failed: {e}")

        return None

    def get_available_captions(self, video_url: str) -> list[dict]:
        """List available captions for a YouTube video.

        Args:
            video_url: YouTube video URL

        Returns:
            List of available caption tracks with language info
        """
        try:
            video_id = self._extract_video_id(video_url) or video_url

            result = subprocess.run(
                [
                    "yt-dlp",
                    "--list-subs",
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                return []

            # Parse the output to find available subtitles
            captions = []
            lines = result.stdout.split("\n")
            in_subs_section = False

            for line in lines:
                if "Available subtitles" in line or "Available automatic captions" in line:
                    in_subs_section = True
                    continue

                if in_subs_section and line.strip():
                    # Lines look like: "en       vtt, json3, ..."
                    parts = line.split()
                    if parts:
                        lang = parts[0]
                        if len(lang) == 2 or len(lang) == 5:  # e.g., "en" or "en-US"
                            captions.append({
                                "lang": lang,
                                "auto": "automatic" in result.stdout.lower(),
                            })

            return captions

        except Exception as e:
            logger.error(f"Failed to list captions: {e}")
            return []

    def _title_matches_session(
        self, title: str, meeting_number: int, group_acronym: str
    ) -> bool:
        """Check if a video title matches the session we're looking for."""
        title_lower = title.lower()
        meeting_patterns = [
            f"ietf {meeting_number}",
            f"ietf{meeting_number}",
            f"ietf-{meeting_number}",
        ]
        group_lower = group_acronym.lower()

        has_meeting = any(p in title_lower for p in meeting_patterns)
        has_group = group_lower in title_lower

        return has_meeting and has_group

    def _extract_video_id(self, url: str) -> str | None:
        """Extract video ID from a YouTube URL."""
        patterns = [
            r"(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})",
            r"^([a-zA-Z0-9_-]{11})$",
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)

        return None
