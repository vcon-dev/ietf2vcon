"""Main converter orchestrator for IETF sessions to vCon.

This module coordinates the various components to convert an IETF session
into a complete vCon document.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from vcon import Vcon

from .datatracker import DataTrackerClient
from .materials import MaterialsDownloader, organize_materials_by_type
from .models import IETFMeeting, IETFSession
from .transcription import (
    MeetechoTranscriptLoader,
    MlxWhisperTranscriber,
    TranscriptionResult,
    WhisperTranscriber,
    WtfServerTranscriber,
    YouTubeCaptionLoader,
    check_backend_availability,
    transcript_to_srt,
    transcript_to_webvtt,
)
from .vcon_builder import VConBuilder
from .youtube import VideoMetadata, YouTubeResolver
from .zulip_client import ZulipClient

logger = logging.getLogger(__name__)


@dataclass
class ConversionOptions:
    """Options for IETF session to vCon conversion."""

    # Video options
    include_video: bool = True
    video_source: str = "youtube"  # youtube, meetecho, or both
    download_video: bool = False
    video_format: str = "best[height<=1080]"

    # Materials options
    include_materials: bool = True
    inline_materials: bool = False  # Embed materials in vCon

    # Transcription options
    include_transcript: bool = True
    transcription_source: str = "auto"  # auto, youtube, whisper, mlx-whisper, wtf-server, meetecho
    whisper_model: str = "base"
    export_srt: bool = False
    export_webvtt: bool = False

    # MLX Whisper backend
    mlx_whisper_url: str | None = None
    mlx_whisper_model: str = "mlx-community/whisper-turbo"

    # WTF Server backend
    wtf_server_url: str | None = None
    wtf_server_provider: str | None = None
    wtf_server_model: str | None = None

    # Chat options
    include_chat: bool = True
    chat_as_dialog: bool = True

    # Output options
    output_dir: Path = field(default_factory=lambda: Path("./output"))

    # Local rsync mirror directory (checked before HTTP for materials)
    rsync_mirror_dir: Path | None = None

    # Authentication (for Zulip)
    zulip_email: str | None = None
    zulip_api_key: str | None = None


@dataclass
class ConversionResult:
    """Result of an IETF session conversion."""

    vcon: Vcon
    meeting_number: int
    group_acronym: str
    session_id: str
    video_url: str | None = None
    materials_count: int = 0
    has_transcript: bool = False
    chat_message_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class IETFSessionConverter:
    """Convert IETF sessions to vCon format."""

    def __init__(self, options: ConversionOptions | None = None):
        self.options = options or ConversionOptions()
        self.options.output_dir.mkdir(parents=True, exist_ok=True)

    def convert_session(
        self,
        meeting_number: int,
        group_acronym: str,
        session_index: int = 0,
    ) -> ConversionResult:
        """Convert an IETF session to vCon.

        Args:
            meeting_number: IETF meeting number (e.g., 124)
            group_acronym: Working group acronym (e.g., "vcon")
            session_index: Index if multiple sessions (0 for first/only)

        Returns:
            ConversionResult with the vCon and metadata
        """
        errors = []
        warnings = []

        logger.info(
            "Converting IETF %d %s session %d",
            meeting_number, group_acronym, session_index,
        )

        # Initialize clients
        datatracker = DataTrackerClient()

        try:
            # Get meeting info
            meeting = datatracker.get_meeting(meeting_number)
            if not meeting:
                meeting = IETFMeeting(number=meeting_number)
                warnings.append(f"Could not fetch meeting {meeting_number} metadata")

            # Get sessions for the group
            sessions = datatracker.get_group_sessions(meeting_number, group_acronym)
            if not sessions:
                session = IETFSession(
                    meeting_number=meeting_number,
                    group_acronym=group_acronym,
                    session_id=f"{group_acronym}-{meeting_number}",
                    start_time=datetime.utcnow(),
                )
                warnings.append("Could not find session data, using defaults")
            elif session_index < len(sessions):
                session = sessions[session_index]
            else:
                session = sessions[0]
                warnings.append(
                    f"Session index {session_index} not found, using first session"
                )

            # Initialize builder
            builder = VConBuilder()
            builder.set_meeting_metadata(meeting, session)
            builder.add_ingress_info(
                source="ietf2vcon",
                meeting_number=meeting_number,
                group_acronym=group_acronym,
                session_index=session_index,
            )

            # Add IETF Note Well as lawful basis for recording/processing
            builder.add_ietf_note_well()

            # Add chairs as parties
            chairs = datatracker.get_group_chairs(group_acronym)
            if chairs:
                builder.add_persons(chairs)
            else:
                builder.add_party(
                    name=f"{group_acronym.upper()} Chairs",
                    role="chair",
                )

            # Add attendees party
            builder.add_attendees_party()

            # Fetch materials first to extract recording URL
            materials = []
            recording_url = None
            if self.options.include_materials or self.options.include_video:
                try:
                    materials = datatracker.get_session_materials(
                        meeting_number, group_acronym
                    )
                    for mat in materials:
                        if mat.type == "recording" and mat.url:
                            if "youtube.com" in mat.url or "youtu.be" in mat.url:
                                recording_url = mat.url
                                break
                except Exception as e:
                    logger.warning("Failed to fetch materials: %s", e)

            # Process video
            video_url = None
            video_dialog_index = -1
            if self.options.include_video:
                video_url, video_dialog_index = self._process_video(
                    builder, session, errors, warnings, recording_url=recording_url
                )

            # Process materials (excluding recordings which are now dialogs)
            materials_count = 0
            if self.options.include_materials:
                materials_count = self._process_materials_list(
                    builder, materials, errors, warnings
                )

            # Process transcription
            has_transcript = False
            if self.options.include_transcript and video_url:
                has_transcript = self._process_transcript(
                    builder, session, video_dialog_index, video_url, errors, warnings
                )

            # Process chat
            chat_count = 0
            if self.options.include_chat:
                chat_count = self._process_chat(
                    builder, session, errors, warnings
                )

            # Build final vCon
            vcon = builder.build()

            return ConversionResult(
                vcon=vcon,
                meeting_number=meeting_number,
                group_acronym=group_acronym,
                session_id=session.session_id,
                video_url=video_url,
                materials_count=materials_count,
                has_transcript=has_transcript,
                chat_message_count=chat_count,
                errors=errors,
                warnings=warnings,
            )

        finally:
            datatracker.close()

    def _process_video(
        self,
        builder: VConBuilder,
        session: IETFSession,
        errors: list[str],
        warnings: list[str],
        recording_url: str | None = None,
    ) -> tuple[str | None, int]:
        """Process video recording."""
        video_url = None
        dialog_index = -1

        youtube = YouTubeResolver(download_dir=self.options.output_dir / "videos")

        try:
            if self.options.video_source in ("youtube", "both"):
                session_date = (
                    session.start_time.strftime("%Y-%m-%d")
                    if session.start_time
                    else None
                )
                video = youtube.search_session_video(
                    session.meeting_number,
                    session.group_acronym,
                    session_date,
                )

                if video:
                    video_url = video.url
                    if self.options.download_video:
                        video_path = youtube.download_video(
                            video.url,
                            format_spec=self.options.video_format,
                        )
                        if video_path:
                            dialog_index = builder.add_video_dialog_inline(
                                video_path, session
                            )
                        else:
                            warnings.append(
                                "Failed to download video, using URL reference"
                            )
                            dialog_index = builder.add_video_dialog(video, session)
                    else:
                        dialog_index = builder.add_video_dialog(video, session)

            # If no YouTube video, try using recording URL from materials
            if dialog_index < 0 and recording_url:
                video_url = recording_url
                dialog_index = builder.add_video_dialog_from_url(
                    recording_url, session, mimetype="video/mp4"
                )
                logger.info("Using recording URL from materials: %s", recording_url)

            # Fallback to Meetecho
            if self.options.video_source in ("meetecho", "both") and dialog_index < 0:
                meetecho_url = youtube.get_meetecho_recording_url(
                    session.meeting_number,
                    session.group_acronym,
                )
                video_url = meetecho_url
                dialog_index = builder.add_video_dialog_from_url(
                    meetecho_url, session
                )

            if dialog_index < 0:
                warnings.append("Could not find video recording for session")

        except Exception as e:
            errors.append(f"Video processing failed: {e}")
            logger.error("Video processing error: %s", e)

        return video_url, dialog_index

    def _ensure_audio(
        self,
        session: IETFSession,
        video_url: str | None,
    ) -> Path | None:
        """Ensure audio is available locally for server-based transcription.

        Auto-downloads audio (mp3) from YouTube if not cached. This avoids
        requiring ``--download-video`` for audio-only transcription.
        """
        audio_dir = self.options.output_dir / "audio"
        audio_path = (
            audio_dir / f"{session.group_acronym}_{session.meeting_number}.mp3"
        )

        if audio_path.exists():
            return audio_path

        if not video_url or "youtube.com" not in video_url:
            return None

        audio_dir.mkdir(parents=True, exist_ok=True)

        youtube = YouTubeResolver(download_dir=audio_dir)
        try:
            logger.info("Downloading audio for transcription: %s", video_url)
            downloaded = youtube.download_audio(
                video_url,
                output_filename=audio_path.name,
            )
            if downloaded and downloaded.exists():
                return downloaded
        except Exception as e:
            logger.warning("Audio download failed: %s", e)

        return None

    def _process_materials_list(
        self,
        builder: VConBuilder,
        materials: list,
        errors: list[str],
        warnings: list[str],
    ) -> int:
        """Process meeting materials from a pre-fetched list."""
        count = 0

        try:
            if not materials:
                warnings.append("No materials found for session")
                return 0

            non_recording_materials = [m for m in materials if m.type != "recording"]

            if self.options.inline_materials:
                downloader = MaterialsDownloader(
                    download_dir=self.options.output_dir / "materials",
                    mirror_dir=self.options.rsync_mirror_dir,
                )
                try:
                    builder.add_materials(
                        non_recording_materials, inline=True, downloader=downloader
                    )
                finally:
                    downloader.close()
            else:
                builder.add_materials(non_recording_materials, inline=False)

            count = len(non_recording_materials)
            logger.info("Added %d materials", count)

        except Exception as e:
            errors.append(f"Materials processing failed: {e}")
            logger.error("Materials processing error: %s", e)

        return count

    def _process_transcript(
        self,
        builder: VConBuilder,
        session: IETFSession,
        video_dialog_index: int,
        video_url: str | None,
        errors: list[str],
        warnings: list[str],
    ) -> bool:
        """Process transcription.

        Tries sources in priority order based on ``transcription_source``:
        1. YouTube auto-generated captions (fastest, no download)
        2. Meetecho pre-generated transcripts
        3. MLX Whisper direct (Apple Silicon, if configured)
        4. WTF Server (multi-provider, if configured)
        5. Local openai-whisper (optional dependency, slowest)
        """
        try:
            transcript = None
            source = self.options.transcription_source

            # --- YouTube captions ---
            if source in ("auto", "youtube") and not transcript:
                transcript = self._try_youtube_captions(session, video_url)

            # --- Meetecho transcripts ---
            if source in ("auto", "meetecho") and not transcript:
                transcript = self._try_meetecho_transcript(session)

            # --- MLX Whisper ---
            if source in ("auto", "mlx-whisper") and not transcript:
                transcript = self._try_mlx_whisper(session, video_url)

            # --- WTF Server ---
            if source in ("auto", "wtf-server") and not transcript:
                transcript = self._try_wtf_server(session, video_url)

            # --- Local Whisper ---
            if source in ("auto", "whisper") and not transcript:
                transcript = self._try_local_whisper(session, video_url)

            if transcript:
                builder.add_transcript(transcript, video_dialog_index)
                logger.info(
                    "Added transcript (%d segments, provider: %s)",
                    len(transcript.segments),
                    transcript.provider,
                )

                # Export to SRT/WebVTT if requested
                base_filename = (
                    f"ietf{session.meeting_number}_{session.group_acronym}"
                )

                if self.options.export_srt:
                    srt_path = self.options.output_dir / f"{base_filename}.srt"
                    srt_path.write_text(
                        transcript_to_srt(transcript), encoding="utf-8"
                    )
                    logger.info("Exported SRT: %s", srt_path)

                if self.options.export_webvtt:
                    vtt_path = self.options.output_dir / f"{base_filename}.vtt"
                    vtt_path.write_text(
                        transcript_to_webvtt(transcript), encoding="utf-8"
                    )
                    logger.info("Exported WebVTT: %s", vtt_path)

                return True
            else:
                warnings.append("No transcript available from any source")

        except Exception as e:
            errors.append(f"Transcription failed: {e}")
            logger.error("Transcription error: %s", e)

        return False

    def _try_youtube_captions(
        self, session: IETFSession, video_url: str | None
    ) -> TranscriptionResult | None:
        """Try loading YouTube auto-generated captions."""
        if not video_url or "youtube.com" not in video_url:
            return None

        youtube = YouTubeResolver(download_dir=self.options.output_dir / "videos")
        logger.info("Fetching YouTube captions...")
        caption_path = youtube.download_captions(
            video_url,
            output_filename=f"ietf{session.meeting_number}_{session.group_acronym}",
        )

        if not caption_path:
            return None

        loader = YouTubeCaptionLoader()
        transcript = loader.load_captions(caption_path)
        if transcript:
            logger.info(
                "Loaded YouTube captions: %d segments", len(transcript.segments)
            )
        return transcript

    def _try_meetecho_transcript(
        self, session: IETFSession
    ) -> TranscriptionResult | None:
        """Try loading a pre-generated Meetecho transcript."""
        loader = MeetechoTranscriptLoader()
        transcript_path = (
            self.options.output_dir
            / "transcripts"
            / f"IETF{session.meeting_number}-{session.group_acronym.upper()}.json"
        )
        if not transcript_path.exists():
            return None

        transcript = loader.load_transcript(transcript_path)
        if transcript:
            logger.info("Loaded Meetecho transcript")
        return transcript

    def _try_mlx_whisper(
        self, session: IETFSession, video_url: str | None
    ) -> TranscriptionResult | None:
        """Try transcribing via MLX Whisper sidecar."""
        if not self.options.mlx_whisper_url:
            return None

        transcriber = MlxWhisperTranscriber(
            base_url=self.options.mlx_whisper_url,
            model=self.options.mlx_whisper_model,
        )

        if not transcriber.is_available():
            logger.info("MLX Whisper not available at %s", self.options.mlx_whisper_url)
            return None

        audio_path = self._ensure_audio(session, video_url)
        if not audio_path:
            logger.info("No audio available for MLX Whisper")
            return None

        return transcriber.transcribe(audio_path)

    def _try_wtf_server(
        self, session: IETFSession, video_url: str | None
    ) -> TranscriptionResult | None:
        """Try transcribing via WTF Server."""
        if not self.options.wtf_server_url:
            return None

        transcriber = WtfServerTranscriber(
            base_url=self.options.wtf_server_url,
            provider=self.options.wtf_server_provider,
            model=self.options.wtf_server_model,
        )

        if not transcriber.is_available():
            logger.info("WTF Server not available at %s", self.options.wtf_server_url)
            return None

        audio_path = self._ensure_audio(session, video_url)
        if not audio_path:
            logger.info("No audio available for WTF Server")
            return None

        return transcriber.transcribe(audio_path)

    def _try_local_whisper(
        self, session: IETFSession, video_url: str | None
    ) -> TranscriptionResult | None:
        """Try transcribing via local openai-whisper."""
        try:
            import whisper  # noqa: F401
        except ImportError:
            if self.options.transcription_source == "whisper":
                logger.error(
                    "openai-whisper not installed. "
                    "Install with: pip install ietf2vcon[whisper]"
                )
            return None

        audio_path = self._ensure_audio(session, video_url)

        # Also check legacy path from --download-video
        if not audio_path:
            legacy_path = (
                self.options.output_dir
                / "videos"
                / f"{session.group_acronym}_{session.meeting_number}.mp3"
            )
            if legacy_path.exists():
                audio_path = legacy_path

        if not audio_path or not audio_path.exists():
            return None

        logger.info(
            "Running local Whisper transcription (model: %s)...",
            self.options.whisper_model,
        )
        whisper_t = WhisperTranscriber(model=self.options.whisper_model)
        transcript = whisper_t.transcribe(audio_path)
        if transcript:
            logger.info(
                "Whisper transcription complete: %d segments",
                len(transcript.segments),
            )
        return transcript

    def _process_chat(
        self,
        builder: VConBuilder,
        session: IETFSession,
        errors: list[str],
        warnings: list[str],
    ) -> int:
        """Process Zulip chat messages."""
        count = 0

        if not self.options.zulip_email or not self.options.zulip_api_key:
            warnings.append(
                "Zulip credentials not provided. "
                "Set --zulip-email and --zulip-api-key to include chat logs."
            )
            return 0

        try:
            zulip = ZulipClient(
                email=self.options.zulip_email,
                api_key=self.options.zulip_api_key,
            )

            try:
                session_start = session.start_time
                session_end = None
                if session_start and session.duration_seconds:
                    session_end = session_start + timedelta(
                        seconds=session.duration_seconds
                    )

                messages = zulip.get_session_messages(
                    session.meeting_number,
                    session.group_acronym,
                    session_start,
                    session_end,
                )

                if messages:
                    if self.options.chat_as_dialog:
                        builder.add_chat_dialog(messages, session, as_text=True)
                    else:
                        from .zulip_client import chat_messages_to_json

                        builder.vcon.add_attachment(
                            purpose="chat_log",
                            body=chat_messages_to_json(messages),
                            encoding="none",
                        )

                    count = len(messages)
                    logger.info("Added %d chat messages", count)
                else:
                    warnings.append("No chat messages found for session")

            finally:
                zulip.close()

        except Exception as e:
            errors.append(f"Chat processing failed: {e}")
            logger.error("Chat processing error: %s", e)

        return count

    def save_vcon(
        self,
        result: ConversionResult,
        output_path: Path | None = None,
    ) -> Path:
        """Save a conversion result to a file.

        Args:
            result: Conversion result
            output_path: Optional output path

        Returns:
            Path to the saved file
        """
        if output_path is None:
            filename = (
                f"ietf{result.meeting_number}_{result.group_acronym}"
                f"_{result.session_id}.vcon.json"
            )
            output_path = self.options.output_dir / filename

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.vcon.to_json())

        logger.info("Saved vCon to: %s", output_path)
        return output_path
