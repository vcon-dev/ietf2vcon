"""Main converter orchestrator for IETF sessions to vCon.

This module coordinates the various components to convert an IETF session
into a complete vCon document.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vcon import Vcon

from .datatracker import MET_STATUS, DataTrackerClient
from .errors import CarryOverError, MeetingReadError, SessionReadError
from .materials import MaterialsDownloader, organize_materials_by_type
from .models import IETFMeeting, IETFSession, MeetingNumber
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

    # Reuse the uuid of an existing vCon for the same session, when one is
    # already present in output_dir. Regenerating a session produces a new
    # instance version of the same conversation, not a different conversation,
    # so the identity has to survive. Turn this off only to mint a new vCon.
    preserve_uuid: bool = True

    # Carry an existing vCon's transcript across a regeneration that did not
    # produce one of its own, and its recording Dialog when the new build has
    # none. Re-running ASR yields different text, and a regeneration meant to
    # fix attachment structure should not silently rewrite -- or drop -- what
    # was said or the recording it was said in.
    preserve_analysis: bool = True

    # Authentication (for Zulip)
    zulip_email: str | None = None
    zulip_api_key: str | None = None


@dataclass
class ConversionResult:
    """Result of an IETF session conversion."""

    vcon: Vcon
    meeting_number: MeetingNumber
    group_acronym: str
    session_id: str
    video_url: str | None = None
    materials_count: int = 0
    has_transcript: bool = False
    chat_message_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    chair_ambiguous: bool = False
    # A published file's session id is no longer in the Datatracker's list;
    # nothing was converted and the file must be left alone.
    published_session_missing: bool = False
    # The failure came from a read every session of the meeting depends on, so
    # a batch run stops instead of trying the rest.
    meeting_wide_failure: bool = False


def finalize_vcon(vcon_dict: dict) -> dict:
    """Put a document in its final shape.

    The four content arrays and the sessions array are always present, as in
    the published files. ``encoding`` belongs to an inline ``body`` (core-05
    2.3.2), so an attachment, Dialog or analysis entry with no body has none.
    """
    for key in ("parties", "dialog", "attachments", "analysis", "sessions"):
        if not isinstance(vcon_dict.get(key), list):
            vcon_dict[key] = []
    for key in ("dialog", "attachments", "analysis"):
        for item in vcon_dict[key]:
            if isinstance(item, dict) and item.get("body") is None:
                item.pop("encoding", None)
    return vcon_dict


def vcon_file_text(vcon: Vcon) -> str:
    """The published file layout: two-space indent, raw UTF-8, one trailing newline."""
    return json.dumps(finalize_vcon(vcon.vcon_dict), indent=2, ensure_ascii=False) + "\n"


PARTIAL_SUFFIX = ".partial"


def atomic_write_text(path: Path, text: str) -> None:
    """Write a file so it is either entirely the old content or entirely the new.

    The text goes to a temporary file in the same directory (hidden, and not
    named ``*.vcon.json``, so nothing mistakes it for a vCon), is flushed and
    fsynced, and then replaces the target with ``os.replace``. A temporary
    file left by a failure is removed.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}{PARTIAL_SUFFIX}")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def remove_stale_partials(directory: Path) -> list[Path]:
    """Delete temporary files left in ``directory`` by a run that was killed."""
    removed = []
    for path in sorted(directory.glob(f".*.vcon.json.*{PARTIAL_SUFFIX}")):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        logger.warning("Removed a temporary file left by an interrupted run: %s", path.name)
        removed.append(path)
    return removed


def replacement_problems(previous: dict, new: dict, options: "ConversionOptions") -> list[str]:
    """Why ``new`` must not replace ``previous`` (empty when it may).

    A published file's uuid and creation time are public identifiers, and a
    regeneration that carries content over must not lose any of it.
    """
    problems = []
    if options.preserve_uuid:
        if previous.get("uuid") != new.get("uuid"):
            problems.append(
                f"uuid would change from {previous.get('uuid')} to {new.get('uuid')}"
            )
        if previous.get("created_at") and previous.get("created_at") != new.get("created_at"):
            problems.append(
                f"created_at would change from {previous.get('created_at')} "
                f"to {new.get('created_at')}"
            )
    if options.preserve_analysis:
        if previous.get("dialog") and not new.get("dialog"):
            problems.append("it has a recording Dialog and the new document has none")
        had = len(previous.get("analysis") or [])
        if len(new.get("analysis") or []) < had:
            problems.append(
                f"it has {had} analysis entries and the new document has "
                f"{len(new.get('analysis') or [])}"
            )
    return problems


class IETFSessionConverter:
    """Convert IETF sessions to vCon format."""

    def __init__(self, options: ConversionOptions | None = None):
        self.options = options or ConversionOptions()
        self.options.output_dir.mkdir(parents=True, exist_ok=True)

    def remove_stale_partials(self) -> list[Path]:
        """Clean up temporary files an interrupted run left in the output directory."""
        return remove_stale_partials(self.options.output_dir)

    @staticmethod
    def _by_id(sessions: list[IETFSession]) -> list[IETFSession]:
        """Sessions in ascending integer id, so positions never depend on the API."""
        def key(s: IETFSession):
            return (0, int(s.session_id), "") if s.session_id.isdigit() else (1, 0, s.session_id)

        return sorted(sessions, key=key)

    @classmethod
    def choose_session(cls, sessions: list[IETFSession]) -> IETFSession:
        """The session to convert for a group that has no previous file.

        A session that actually met comes first: its status is "sched", or
        unknown. Among those, the earliest official start wins (a session with
        no start after those with one), then the lowest integer id. If none
        met, the lowest id.
        """
        ordered = cls._by_id(sessions)
        met = [s for s in ordered if s.status in (None, MET_STATUS)]
        if not met:
            return ordered[0]
        return min(
            met,
            key=lambda s: (s.start_time is None, s.start_time or datetime.max.replace(tzinfo=UTC)),
        )

    def previous_session_ids(
        self, meeting_number: MeetingNumber, group_acronym: str
    ) -> list[str]:
        """Session ids of the files this meeting and group already have in output_dir."""
        pattern = re.compile(
            rf"^{re.escape(self._meeting_slug(meeting_number))}_"
            rf"{re.escape(group_acronym)}_(\d+)\.vcon\.json$"
        )
        found = [
            m.group(1) for path in self.options.output_dir.iterdir()
            if (m := pattern.match(path.name))
        ]
        return sorted(found, key=int)

    def convert_session(
        self,
        meeting_number: MeetingNumber,
        group_acronym: str,
        session_index: int | None = None,
        session_id: str | None = None,
    ) -> ConversionResult:
        """Convert an IETF session to vCon.

        Args:
            meeting_number: IETF meeting number (e.g., 124)
            group_acronym: Working group acronym (e.g., "vcon")
            session_index: Position among the group's sessions in id order
            session_id: Convert this session id (a previously published file's
                id). If the Datatracker no longer lists it, nothing is
                converted and ``published_session_missing`` is set.

        With neither, the session is chosen by ``choose_session``.

        Returns:
            ConversionResult with the vCon and metadata
        """
        errors = []
        warnings = []

        logger.info(
            "Converting IETF %s %s session %s",
            meeting_number, group_acronym,
            session_id if session_id is not None else (
                session_index if session_index is not None else "(chosen)"
            ),
        )

        # Initialize clients
        datatracker = DataTrackerClient()
        where = f"{group_acronym} at IETF {meeting_number}"
        chosen_id = str(session_id) if session_id is not None else ""

        try:
            # Get meeting info. Without the meeting record nothing can be
            # dated, so a missing or unreadable one fails the session.
            meeting = datatracker.get_meeting(meeting_number)
            if not meeting:
                raise MeetingReadError(f"the Datatracker has no meeting {meeting_number}")

            # Get sessions for the group.
            #
            # This used to fall back to a synthetic session -- id
            # "<group>-<meeting>", start_time = now -- whenever the lookup came
            # back empty. A transient API failure therefore produced a
            # plausible-looking vCon stamped with today's date and a fresh
            # uuid, reported as a success. Seven of IETF 125's records were
            # fabricated that way in a single batch run. Refuse instead.
            sessions = datatracker.get_group_sessions(meeting_number, group_acronym)
            if sessions is None:
                errors.append(
                    f"Could not look up sessions for {group_acronym} at IETF {meeting_number}"
                )
                return ConversionResult(
                    vcon=VConBuilder().build(), meeting_number=meeting_number,
                    group_acronym=group_acronym, session_id="",
                    errors=errors, warnings=warnings,
                )
            if not sessions:
                errors.append(
                    f"{group_acronym} has no sessions at IETF {meeting_number}"
                )
                return ConversionResult(
                    vcon=VConBuilder().build(), meeting_number=meeting_number,
                    group_acronym=group_acronym, session_id="",
                    errors=errors, warnings=warnings,
                )
            sessions = self._by_id(sessions)
            if session_id is not None:
                match = [s for s in sessions if s.session_id == str(session_id)]
                if not match:
                    filename = self._session_filename(meeting_number, group_acronym, str(session_id))
                    message = (
                        f"Published session not found: {filename} is for session "
                        f"{session_id}, which {group_acronym} no longer has at IETF "
                        f"{meeting_number}; the file is left untouched"
                    )
                    logger.warning(message)
                    warnings.append(message)
                    return ConversionResult(
                        vcon=VConBuilder().build(), meeting_number=meeting_number,
                        group_acronym=group_acronym, session_id=str(session_id),
                        errors=errors, warnings=warnings, published_session_missing=True,
                    )
                session = match[0]
            elif session_index is None:
                session = self.choose_session(sessions)
            elif session_index < len(sessions):
                session = sessions[session_index]
            else:
                session = sessions[0]
                warnings.append(
                    f"Session index {session_index} not found, using first session"
                )
            session_index = sessions.index(session)
            chosen_id = session.session_id
            where = f"{group_acronym} session {chosen_id} at IETF {meeting_number}"

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

            # Add chairs as parties. The session start time matters: without
            # it the Datatracker returns today's chairs, so a 2014 session
            # would credit whoever chairs the group now. A session with no
            # scheduled time is dated by its meeting's first day instead
            # (meeting.start_date, the Datatracker's `date` field), taking the
            # end of that day so a change made that morning counts. With
            # neither date known the roles cannot be dated, so no chairs are
            # asserted rather than today's being assumed.
            session_date = session.start_time
            if session_date is None and meeting.start_date:
                day = meeting.start_date
                session_date = datetime(
                    day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC
                )
                logger.info(
                    "Session %s of %s has no start time; looking up chairs as of "
                    "the end of the meeting's first day (%s)",
                    session.session_id, group_acronym, session_date.isoformat(),
                )
            chair_ambiguous = False
            if session_date:
                # A failed read raises (and fails the session); an empty list
                # is a definitive "no chairs".
                chairs = datatracker.get_group_chairs(group_acronym, session_date=session_date)
                chair_ambiguous = (
                    getattr(datatracker, "last_chair_ambiguous", False) is True
                )
                if chair_ambiguous:
                    warnings.append(
                        "Chair history is ambiguous (snapshots sharing one time hold "
                        "different chairs); chairs omitted."
                    )
            else:
                chairs = []
                logger.warning(
                    "Session %s of %s at IETF %s has no start time and the meeting "
                    "date is unknown; chairs left unset",
                    session.session_id, group_acronym, meeting_number,
                )
                warnings.append(
                    "Session start time unknown; chairs left unset rather than "
                    "asserting the current ones"
                )
            if chairs:
                builder.add_persons(chairs)
            else:
                # Role history starts 2011-12-09, and some sessions have no
                # scheduled time to date the roles against. Either way, no
                # chair party beats a "<WG> Chairs" placeholder standing in for
                # people we cannot name. The attendees party is still added, so
                # the vCon is complete without it.
                logger.info(
                    "No verifiable chairs for %s at %s; omitting the chair party",
                    group_acronym, session_date,
                )

            # Add attendees party
            builder.add_attendees_party()

            # Fetch materials first to extract recording URL
            materials = []
            recording_url = None
            recording_mediatype = "video/mp4"
            if self.options.include_materials or self.options.include_video:
                try:
                    materials = datatracker.get_session_materials(
                        meeting_number, group_acronym
                    )
                    # Prefer video: meetings from roughly IETF 95 on publish a
                    # YouTube recording, but IETF 90-94 have only the audio MP3
                    # on ietf.org, and several later meetings carry both.
                    audio_url = None
                    for mat in materials:
                        if mat.type != "recording" or not mat.url:
                            continue
                        if "youtube.com" in mat.url or "youtu.be" in mat.url:
                            recording_url = mat.url
                            break
                        if mat.url.lower().endswith(".mp3"):
                            # The Datatracker lists some audio twice, as http
                            # and https. The spec requires HTTPS for externally
                            # referenced files, so take that variant.
                            if audio_url is None or (
                                audio_url.startswith("http://")
                                and mat.url.startswith("https://")
                            ):
                                audio_url = mat.url
                    if recording_url is None and audio_url:
                        recording_url = audio_url
                        recording_mediatype = "audio/mpeg"
                except SessionReadError:
                    raise
                except Exception as e:
                    raise SessionReadError(f"could not read the materials list: {e}") from e

            # Process video
            video_url = None
            video_dialog_index = -1
            if self.options.include_video:
                video_url, video_dialog_index = self._process_video(
                    builder,
                    session,
                    errors,
                    warnings,
                    recording_url=recording_url,
                    recording_mediatype=recording_mediatype,
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

            self._carry_over_previous(
                vcon, meeting_number, group_acronym, session.session_id, warnings
            )
            finalize_vcon(vcon.vcon_dict)

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
                chair_ambiguous=chair_ambiguous,
            )

        except (SessionReadError, CarryOverError) as e:
            # Nothing is saved for a session that could not be read completely
            # or whose published file cannot safely be regenerated.
            message = f"{where}: {e}"
            logger.error(message)
            errors.append(message)
            return ConversionResult(
                vcon=VConBuilder().build(), meeting_number=meeting_number,
                group_acronym=group_acronym, session_id=chosen_id,
                errors=errors, warnings=warnings,
                meeting_wide_failure=isinstance(e, MeetingReadError),
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
        recording_mediatype: str = "video/mp4",
    ) -> tuple[str | None, int]:
        """Process the session recording (video, or audio for older meetings)."""
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
                    recording_url, session, mimetype=recording_mediatype
                )
                logger.info(
                    "Using %s recording from materials: %s",
                    recording_mediatype,
                    recording_url,
                )

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

            # A downloader is needed even when materials are not inlined: an
            # external reference carries a content_hash, and the hash needs the
            # bytes. It reads from the rsync mirror first when one is configured.
            downloader = MaterialsDownloader(
                download_dir=self.options.output_dir / "materials",
                mirror_dir=self.options.rsync_mirror_dir,
            )
            try:
                builder.add_materials(
                    non_recording_materials,
                    inline=self.options.inline_materials,
                    downloader=downloader,
                )
            finally:
                downloader.close()

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
                    f"{self._meeting_slug(session.meeting_number)}_{session.group_acronym}"
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
        if not video_url or not any(
            host in video_url for host in ("youtube.com", "youtu.be")
        ):
            return None

        youtube = YouTubeResolver(download_dir=self.options.output_dir / "videos")
        logger.info("Fetching YouTube captions...")
        caption_path = youtube.download_captions(
            video_url,
            output_filename=f"{self._meeting_slug(session.meeting_number)}_{session.group_acronym}",
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

                        # Inline content needs mediatype (core-05 4.6.6), and a
                        # list is JSON, not a JSON string, so "none" is wrong.
                        builder.vcon.add_attachment(
                            purpose="chat_log",
                            party=builder.attachment_party,
                            session=builder.session_index,
                            body=chat_messages_to_json(messages),
                            encoding="json",
                            mediatype="application/json",
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

    @staticmethod
    def _meeting_slug(meeting_number: MeetingNumber) -> str:
        """Filename stem for a meeting.

        "ietf" prefixes a bare number; an interim identifier already names the
        meeting, so prefixing it would read "ietfinterim-2026-vcon-02".
        """
        return (
            f"ietf{meeting_number}"
            if str(meeting_number).isdigit()
            else str(meeting_number)
        )

    def _session_filename(self, meeting_number: MeetingNumber, group_acronym: str, session_id: str) -> str:
        return f"{self._meeting_slug(meeting_number)}_{group_acronym}_{session_id}.vcon.json"

    def _carry_over_previous(
        self,
        vcon: Vcon,
        meeting_number: MeetingNumber,
        group_acronym: str,
        session_id: str,
        warnings: list[str],
    ) -> None:
        """Carry identity and transcripts across from a previous conversion.

        A regenerated session is the same conversation, so two things survive:

        * the **uuid**, because anything that cited it -- a database row, a
          published dataset, someone's bookmark -- must keep resolving;
        * the **transcript**, because re-running ASR produces different text
          and a regeneration meant to fix attachment structure has no business
          rewriting what was said. Dropping it would be worse still.
        """
        existing = self.options.output_dir / self._session_filename(
            meeting_number, group_acronym, session_id
        )
        if not existing.is_file():
            return
        # A published file is replaced only by a complete result. One that
        # cannot be read or migrated must not be silently replaced by a fresh
        # document with a new uuid, so each of these fails the session.
        try:
            raw_previous = json.loads(existing.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            raise CarryOverError(
                f"{existing.name} exists but cannot be read ({e}); it is left untouched"
            ) from e
        if not isinstance(raw_previous, dict):
            raise CarryOverError(
                f"{existing.name} exists but is not a vCon object; it is left untouched"
            )
        if self.options.preserve_uuid and not raw_previous.get("uuid"):
            raise CarryOverError(
                f"{existing.name} exists but has no uuid to keep; it is left untouched"
            )

        previous = raw_previous
        previous_created_at = raw_previous.get("created_at")
        if previous.get("vcon") != "0.5.0":
            # A file written before the port is 0.4.0. Migrate it with the
            # reference library so carried dialog and analysis have the
            # 0.5.0 shape (Sessions, recording-only dialog types).
            try:
                from vcon.migration import migrate_0_4_0_to_0_5_0

                previous = migrate_0_4_0_to_0_5_0(previous).vcon
            except Exception as e:  # noqa: BLE001
                raise CarryOverError(
                    f"{existing.name} cannot be migrated to 0.5.0 ({e}); it is left untouched"
                ) from e

        if self.options.preserve_uuid:
            previous_uuid = previous.get("uuid")
            if previous_uuid and previous_uuid != vcon.vcon_dict.get("uuid"):
                logger.info("Preserving uuid %s from %s", previous_uuid, existing.name)
                vcon.vcon_dict["uuid"] = previous_uuid
                # core-05 4.1.5: created_at "should not change once the vcon
                # object is created". The uuid says this is the same vCon, so
                # its creation time stays; 4.1.6 has updated_at record this
                # regeneration (set by VConBuilder.build()).
                if previous_created_at:
                    vcon.vcon_dict["created_at"] = previous_created_at

        if self.options.preserve_analysis:
            self._carry_over_dialog(vcon, previous, existing.name)
            self._carry_over_analysis(vcon, previous, existing.name, warnings)
        elif raw_previous.get("dialog") and not vcon.vcon_dict.get("dialog"):
            logger.warning(
                "CARRY-OVER lost the recording dialog of %s: this build produced "
                "none and carry-over is off", existing.name,
            )

        # Last check before anything can be saved: identity and carried
        # content must all have survived.
        problems = replacement_problems(raw_previous, vcon.vcon_dict, self.options)
        if problems:
            raise CarryOverError(
                f"the new document must not replace {existing.name}: "
                + "; ".join(problems)
            )

    def _carry_over_dialog(self, vcon: Vcon, previous: dict, name: str) -> None:
        """Reuse the previous dialog when this build produced none.

        Independent of analysis: a recording that was never transcribed must
        survive a regeneration that does not look the recording up again.
        """
        if vcon.vcon_dict.get("dialog") or not previous.get("dialog"):
            return
        dialog = previous["dialog"]
        vcon.vcon_dict["dialog"] = dialog
        # core-05: the Session lists its Dialogs.
        sessions = vcon.vcon_dict.get("sessions") or []
        if sessions:
            sessions[0]["dialog"] = list(range(len(dialog)))
        logger.info("CARRY-OVER dialog adopted: %d dialog from %s", len(dialog), name)
        self._tidy_carried_dialog(vcon, dialog, name)

    def _carry_over_analysis(
        self, vcon: Vcon, previous: dict, name: str, warnings: list[str]
    ) -> None:
        """Bring the previous transcript across.

        Analysis entries address dialog by index. _carry_over_dialog has
        already adopted the previous dialog if this conversion produced none,
        so the indices still resolve; any analysis pointing past the end of
        the dialog is left behind rather than carried across as a dangling
        reference.
        """
        previous_analysis = previous.get("analysis") or []
        if not previous_analysis or vcon.vcon_dict.get("analysis"):
            return

        dialog = vcon.vcon_dict.get("dialog") or []

        carried, dangling = [], 0
        for analysis in previous_analysis:
            index = analysis.get("dialog")
            if index is not None and index >= len(dialog):
                dangling += 1
                continue
            if (
                analysis.get("body") is not None
                and "url" not in analysis
                and "mediatype" not in analysis
                and (
                    analysis.get("encoding") == "json"
                    or isinstance(analysis["body"], (dict, list))
                )
            ):
                analysis["mediatype"] = "application/json"
            carried.append(analysis)

        if dangling:
            warnings.append(
                f"{dangling} analysis entries from {name} reference dialog this "
                "conversion did not produce, and were not carried over"
            )
        if not carried:
            return

        vcon.vcon_dict["analysis"] = carried
        # The carried content is described by the previous file's extension
        # declarations (wtf_transcription and friends), so those come too.
        # "role" and "meta" were never extension names, and "CC" is decided by
        # the parties of this build, not by the previous file.
        extensions = vcon.vcon_dict.setdefault("extensions", [])
        for extension in previous.get("extensions") or []:
            if extension not in extensions and extension not in ("role", "meta", "CC"):
                extensions.append(extension)
        has_role = any(
            isinstance(p, dict) and p.get("role") for p in vcon.vcon_dict.get("parties") or []
        )
        if has_role and "CC" not in extensions:
            extensions.append("CC")
        elif not has_role and "CC" in extensions:
            extensions.remove("CC")
        logger.info("Carried %d analysis entries over from %s", len(carried), name)

    @staticmethod
    def _retime_carried_recording(session: dict, index: int, dialog: dict, name: str) -> None:
        """Give an adopted recording the Session's official time.

        A fresh build sets a recording Dialog's ``start`` from the session, and
        its ``duration`` from the session too unless the recording is a
        YouTube video, whose own length is used. The Session carries the
        official values (or none when the session is not on the official
        schedule), so they are copied from it. Without an official time the
        carried values stay and a warning lists the recording as unverified.
        """
        new_start = session.get("start")
        if not new_start:
            if dialog.get("start"):
                logger.warning(
                    "CARRY-OVER recording dialog %d from %s: the session has no "
                    "official time, kept the carried start %s unverified",
                    index, name, dialog["start"],
                )
            return
        old_start = dialog.get("start")
        if old_start != new_start:
            try:
                changed = datetime.fromisoformat(old_start.replace("Z", "+00:00")) != (
                    datetime.fromisoformat(new_start)
                )
            except (AttributeError, ValueError):
                changed = True
            if changed:
                logger.info(
                    "CARRY-OVER recording dialog %d from %s: start %s -> %s",
                    index, name, old_start, new_start,
                )
            dialog["start"] = new_start
        url = str(dialog.get("url") or "")
        is_youtube = "youtube.com" in url or "youtu.be" in url
        if session.get("duration") is not None and not is_youtube:
            if dialog.get("duration") != session["duration"]:
                logger.info(
                    "CARRY-OVER recording dialog %d from %s: duration %s -> %s",
                    index, name, dialog.get("duration"), session["duration"],
                )
            dialog["duration"] = session["duration"]

    @staticmethod
    def _tidy_carried_dialog(vcon: Vcon, dialog: list, name: str) -> None:
        """Bring adopted dialog in line with this build.

        core-05 has no Dialog ``meta`` or ``metadata``, so both are dropped. The
        parties of a recording or chat Dialog are every party of the vCon, the
        rule VConBuilder follows. A previous file's list can index past the end
        of ``parties`` (an old maintenance script rewrote ``parties`` without
        touching the Dialogs) or leave parties out, so it is rebuilt from the
        parties of this build rather than copied.
        """
        parties = list(range(len(vcon.vcon_dict.get("parties") or [])))
        sessions = vcon.vcon_dict.get("sessions") or [{}]
        for i, d in enumerate(dialog):
            if not isinstance(d, dict):
                continue
            d.pop("meta", None)
            d.pop("metadata", None)
            if d.get("type") == "recording":
                IETFSessionConverter._retime_carried_recording(sessions[0], i, d, name)
            if d.get("type") not in ("recording", "text"):
                continue
            previous_parties = d.get("parties")
            if previous_parties == (parties or None):
                continue
            if d["type"] == "text" and isinstance(previous_parties, list) and len(previous_parties) == 1:
                # One party is how a sender is written. This converter's chat
                # Dialog lists every party, so a lone index is either a real
                # sender or what the 0.4.0 migration reduced it to; flattening
                # could erase a sender, so it is left alone.
                logger.warning(
                    "CARRY-OVER text dialog %d from %s has a single party %s; "
                    "left as is, not rebuilt to all parties", i, name, previous_parties,
                )
                continue
            logger.info(
                "CARRY-OVER parties rebuilt: dialog %d from %s had %s, now %s",
                i, name, previous_parties, parties,
            )
            if parties:
                d["parties"] = parties
            else:
                d.pop("parties", None)

    def save_vcon(
        self,
        result: ConversionResult,
        output_path: Path | None = None,
    ) -> Path:
        """Save a conversion result to a file, atomically.

        A published file is replaced only by a complete, verified result. A
        result with errors is refused, an existing file that cannot be read is
        left alone, and so is one whose uuid, creation time or carried content
        the new document would lose. The write itself goes through a temporary
        file and ``os.replace``, so a kill never leaves a truncated vCon.

        Args:
            result: Conversion result
            output_path: Optional output path

        Returns:
            Path to the saved file
        """
        if result.errors:
            raise CarryOverError(
                f"not saving {result.group_acronym} session {result.session_id}: "
                + result.errors[0]
            )
        if output_path is None:
            output_path = self.options.output_dir / self._session_filename(
                result.meeting_number, result.group_acronym, result.session_id
            )

        finalize_vcon(result.vcon.vcon_dict)
        if output_path.exists():
            try:
                previous = json.loads(output_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                raise CarryOverError(
                    f"{output_path.name} exists but cannot be read ({e}); it is left untouched"
                ) from e
            if not isinstance(previous, dict):
                raise CarryOverError(
                    f"{output_path.name} exists but is not a vCon object; it is left untouched"
                )
            problems = replacement_problems(previous, result.vcon.vcon_dict, self.options)
            if problems:
                raise CarryOverError(
                    f"the new document must not replace {output_path.name}: "
                    + "; ".join(problems)
                )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output_path, vcon_file_text(result.vcon))

        logger.info("Saved vCon to: %s", output_path)
        return output_path
