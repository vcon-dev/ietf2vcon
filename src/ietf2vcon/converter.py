"""Main converter orchestrator for IETF sessions to vCon.

This module coordinates the various components to convert an IETF session
into a complete vCon document.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .datatracker import DataTrackerClient
from .materials import MaterialsDownloader, organize_materials_by_type
from .models import IETFMeeting, IETFSession, VCon
from .transcription import (
    MeetechoTranscriptLoader,
    TranscriptionResult,
    WhisperTranscriber,
    YouTubeCaptionLoader,
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
    include_transcript: bool = True  # Enabled by default
    transcription_source: str = "auto"  # auto, youtube, whisper, meetecho
    whisper_model: str = "base"
    export_srt: bool = False  # Export transcript as SRT file
    export_webvtt: bool = False  # Export transcript as WebVTT file

    # Chat options
    include_chat: bool = True
    chat_as_dialog: bool = True  # True for dialog, False for attachment

    # Output options
    output_dir: Path = field(default_factory=lambda: Path("./output"))

    # Authentication (for Zulip)
    zulip_email: str | None = None
    zulip_api_key: str | None = None


@dataclass
class ConversionResult:
    """Result of an IETF session conversion."""

    vcon: VCon
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

        logger.info(f"Converting IETF {meeting_number} {group_acronym} session {session_index}")

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
                # Create a minimal session if not found
                session = IETFSession(
                    meeting_number=meeting_number,
                    group_acronym=group_acronym,
                    session_id=f"{group_acronym}-{meeting_number}",
                    start_time=datetime.utcnow(),
                )
                warnings.append(f"Could not find session data, using defaults")
            elif session_index < len(sessions):
                session = sessions[session_index]
            else:
                session = sessions[0]
                warnings.append(f"Session index {session_index} not found, using first session")

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
                # Add generic chair party
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
                    materials = datatracker.get_session_materials(meeting_number, group_acronym)
                    # Extract recording URL from materials
                    for mat in materials:
                        if mat.type == "recording" and mat.url:
                            # Check if it's a YouTube URL
                            if "youtube.com" in mat.url or "youtu.be" in mat.url:
                                recording_url = mat.url
                                break
                except Exception as e:
                    logger.warning(f"Failed to fetch materials: {e}")

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

            # Process transcription (always try if we have a video)
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
                # Search for video on YouTube
                session_date = session.start_time.strftime("%Y-%m-%d") if session.start_time else None
                video = youtube.search_session_video(
                    session.meeting_number,
                    session.group_acronym,
                    session_date,
                )

                if video:
                    video_url = video.url
                    if self.options.download_video:
                        # Download and embed inline
                        video_path = youtube.download_video(
                            video.url,
                            format_spec=self.options.video_format,
                        )
                        if video_path:
                            dialog_index = builder.add_video_dialog_inline(
                                video_path, session
                            )
                        else:
                            warnings.append("Failed to download video, using URL reference")
                            dialog_index = builder.add_video_dialog(video, session)
                    else:
                        dialog_index = builder.add_video_dialog(video, session)

            # If no YouTube video, try using recording URL from materials
            if dialog_index < 0 and recording_url:
                video_url = recording_url
                dialog_index = builder.add_video_dialog_from_url(
                    recording_url, session,
                    mimetype="video/mp4",
                )
                logger.info(f"Using recording URL from materials: {recording_url}")

            # Fallback to Meetecho
            if self.options.video_source in ("meetecho", "both") and dialog_index < 0:
                # Use Meetecho URL
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
            logger.error(f"Video processing error: {e}")

        return video_url, dialog_index

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

            # Filter out recordings (handled as video dialog)
            non_recording_materials = [m for m in materials if m.type != "recording"]

            if self.options.inline_materials:
                downloader = MaterialsDownloader(
                    download_dir=self.options.output_dir / "materials"
                )
                try:
                    builder.add_materials(non_recording_materials, inline=True, downloader=downloader)
                finally:
                    downloader.close()
            else:
                builder.add_materials(non_recording_materials, inline=False)

            count = len(non_recording_materials)
            logger.info(f"Added {count} materials")

        except Exception as e:
            errors.append(f"Materials processing failed: {e}")
            logger.error(f"Materials processing error: {e}")

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

        Tries multiple sources in order:
        1. YouTube auto-generated captions (fastest, no download)
        2. Meetecho pre-generated transcripts
        3. Whisper transcription (requires audio download)
        """
        try:
            transcript = None
            youtube = YouTubeResolver(download_dir=self.options.output_dir / "videos")

            # 1. Try YouTube captions first (fastest option)
            if video_url and "youtube.com" in video_url:
                logger.info("Fetching YouTube captions...")
                caption_path = youtube.download_captions(
                    video_url,
                    output_filename=f"ietf{session.meeting_number}_{session.group_acronym}",
                )

                if caption_path:
                    loader = YouTubeCaptionLoader()
                    transcript = loader.load_captions(caption_path)
                    if transcript:
                        logger.info(f"Loaded YouTube captions: {len(transcript.segments)} segments")

            # 2. Try Meetecho transcript if no YouTube captions
            if not transcript and self.options.transcription_source in ("meetecho", "auto"):
                loader = MeetechoTranscriptLoader()
                transcript_path = (
                    self.options.output_dir
                    / "transcripts"
                    / f"IETF{session.meeting_number}-{session.group_acronym.upper()}.json"
                )
                if transcript_path.exists():
                    transcript = loader.load_transcript(transcript_path)
                    if transcript:
                        logger.info("Loaded Meetecho transcript")

            # 3. Try Whisper transcription as last resort
            if not transcript and self.options.transcription_source in ("whisper", "auto"):
                # Check if we already have audio
                audio_path = (
                    self.options.output_dir
                    / "videos"
                    / f"{session.group_acronym}_{session.meeting_number}.mp3"
                )

                # Download audio if not present and we have a YouTube URL
                if not audio_path.exists() and video_url and "youtube.com" in video_url:
                    if self.options.download_video:
                        logger.info("Downloading audio for Whisper transcription...")
                        audio_path = youtube.download_audio(
                            video_url,
                            output_filename=f"{session.group_acronym}_{session.meeting_number}.mp3",
                        )

                if audio_path and audio_path.exists():
                    logger.info(f"Running Whisper transcription (model: {self.options.whisper_model})...")
                    whisper = WhisperTranscriber(model=self.options.whisper_model)
                    transcript = whisper.transcribe(audio_path)
                    if transcript:
                        logger.info(f"Whisper transcription complete: {len(transcript.segments)} segments")

            if transcript:
                builder.add_transcript(transcript, video_dialog_index)
                logger.info(f"Added transcript ({len(transcript.segments)} segments, provider: {transcript.provider})")

                # Export to SRT/WebVTT if requested
                base_filename = f"ietf{session.meeting_number}_{session.group_acronym}"

                if self.options.export_srt:
                    srt_path = self.options.output_dir / f"{base_filename}.srt"
                    srt_content = transcript_to_srt(transcript)
                    srt_path.write_text(srt_content, encoding="utf-8")
                    logger.info(f"Exported SRT: {srt_path}")

                if self.options.export_webvtt:
                    vtt_path = self.options.output_dir / f"{base_filename}.vtt"
                    vtt_content = transcript_to_webvtt(transcript)
                    vtt_path.write_text(vtt_content, encoding="utf-8")
                    logger.info(f"Exported WebVTT: {vtt_path}")

                return True
            else:
                warnings.append("No transcript available (YouTube captions not found)")

        except Exception as e:
            errors.append(f"Transcription failed: {e}")
            logger.error(f"Transcription error: {e}")

        return False

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
                # Calculate session time window
                session_start = session.start_time
                session_end = None
                if session_start and session.duration_seconds:
                    session_end = session_start + timedelta(seconds=session.duration_seconds)

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
                        # Add as attachment
                        from .zulip_client import chat_messages_to_json
                        builder.vcon.attachments.append(
                            builder.vcon.attachments.__class__(
                                type="chat_log",
                                body=chat_messages_to_json(messages),
                                encoding="none",
                                meta={
                                    "source": "zulip",
                                    "stream": session.group_acronym,
                                    "message_count": len(messages),
                                },
                            )
                        )

                    count = len(messages)
                    logger.info(f"Added {count} chat messages")
                else:
                    warnings.append("No chat messages found for session")

            finally:
                zulip.close()

        except Exception as e:
            errors.append(f"Chat processing failed: {e}")
            logger.error(f"Chat processing error: {e}")

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
                f"ietf{result.meeting_number}_{result.group_acronym}_{result.session_id}.vcon.json"
            )
            output_path = self.options.output_dir / filename

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.vcon.to_json())

        logger.info(f"Saved vCon to: {output_path}")
        return output_path
