"""Transcription services for IETF session audio/video.

Supports multiple transcription backends:
- OpenAI Whisper (local)
- Meetecho pre-generated transcripts
"""

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import TranscriptSegment, VConAnalysis

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    """Result from a transcription service."""

    text: str
    segments: list[TranscriptSegment]
    language: str | None = None
    duration: float | None = None
    provider: str = "unknown"
    model: str | None = None


class WhisperTranscriber:
    """Transcribe audio using OpenAI Whisper."""

    def __init__(self, model: str = "base"):
        """Initialize Whisper transcriber.

        Args:
            model: Whisper model size (tiny, base, small, medium, large)
        """
        self.model = model
        self._whisper = None

    def _load_whisper(self):
        """Lazy load whisper module."""
        if self._whisper is None:
            try:
                import whisper
                self._whisper = whisper
            except ImportError:
                raise ImportError(
                    "whisper not installed. Install with: pip install openai-whisper"
                )
        return self._whisper

    def transcribe(self, audio_path: Path) -> TranscriptionResult | None:
        """Transcribe an audio file using Whisper.

        Args:
            audio_path: Path to audio file

        Returns:
            TranscriptionResult or None if failed
        """
        try:
            whisper = self._load_whisper()
            logger.info(f"Loading Whisper model: {self.model}")
            model = whisper.load_model(self.model)

            logger.info(f"Transcribing: {audio_path}")
            result = model.transcribe(str(audio_path))

            segments = []
            for i, seg in enumerate(result.get("segments", [])):
                segments.append(
                    TranscriptSegment(
                        id=i,
                        start=seg["start"],
                        end=seg["end"],
                        text=seg["text"].strip(),
                        confidence=seg.get("avg_logprob"),
                    )
                )

            return TranscriptionResult(
                text=result["text"],
                segments=segments,
                language=result.get("language"),
                provider="whisper",
                model=self.model,
            )

        except Exception as e:
            logger.error(f"Whisper transcription failed: {e}")
            return None

    def transcribe_with_cli(self, audio_path: Path) -> TranscriptionResult | None:
        """Transcribe using whisper CLI (alternative to Python API).

        Args:
            audio_path: Path to audio file

        Returns:
            TranscriptionResult or None if failed
        """
        try:
            output_dir = audio_path.parent
            output_base = audio_path.stem

            result = subprocess.run(
                [
                    "whisper",
                    str(audio_path),
                    "--model", self.model,
                    "--output_dir", str(output_dir),
                    "--output_format", "json",
                ],
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour timeout
            )

            if result.returncode != 0:
                logger.error(f"Whisper CLI failed: {result.stderr}")
                return None

            # Load the JSON output
            json_path = output_dir / f"{output_base}.json"
            if not json_path.exists():
                logger.error(f"Whisper output not found: {json_path}")
                return None

            with open(json_path) as f:
                data = json.load(f)

            segments = []
            for i, seg in enumerate(data.get("segments", [])):
                segments.append(
                    TranscriptSegment(
                        id=i,
                        start=seg["start"],
                        end=seg["end"],
                        text=seg["text"].strip(),
                    )
                )

            return TranscriptionResult(
                text=data.get("text", ""),
                segments=segments,
                language=data.get("language"),
                provider="whisper-cli",
                model=self.model,
            )

        except subprocess.TimeoutExpired:
            logger.error("Whisper CLI timed out")
        except Exception as e:
            logger.error(f"Whisper CLI failed: {e}")

        return None


class YouTubeCaptionLoader:
    """Load transcripts from YouTube caption files (JSON3 format)."""

    def load_captions(self, caption_path: Path) -> TranscriptionResult | None:
        """Load a YouTube JSON3 caption file.

        Args:
            caption_path: Path to the JSON3 caption file

        Returns:
            TranscriptionResult or None if failed
        """
        try:
            with open(caption_path) as f:
                data = json.load(f)

            segments = []
            full_text = []

            # JSON3 format has "events" array with caption segments
            events = data.get("events", [])

            segment_id = 0
            for event in events:
                # Skip non-caption events (like style events)
                if "segs" not in event:
                    continue

                # Get timing info (in milliseconds)
                start_ms = event.get("tStartMs", 0)
                duration_ms = event.get("dDurationMs", 0)

                start_sec = start_ms / 1000.0
                end_sec = (start_ms + duration_ms) / 1000.0

                # Combine all segments in this event
                text_parts = []
                for seg in event.get("segs", []):
                    text = seg.get("utf8", "")
                    if text and text.strip():
                        text_parts.append(text)

                if text_parts:
                    combined_text = "".join(text_parts).strip()
                    if combined_text:
                        segments.append(
                            TranscriptSegment(
                                id=segment_id,
                                start=start_sec,
                                end=end_sec,
                                text=combined_text,
                            )
                        )
                        full_text.append(combined_text)
                        segment_id += 1

            if not segments:
                logger.warning("No caption segments found in file")
                return None

            # Calculate duration from last segment
            duration = segments[-1].end if segments else None

            return TranscriptionResult(
                text=" ".join(full_text),
                segments=segments,
                language="en",  # YouTube captions are typically auto-detected
                duration=duration,
                provider="youtube",
                model="auto-generated",
            )

        except Exception as e:
            logger.error(f"Failed to load YouTube captions: {e}")
            return None


class MeetechoTranscriptLoader:
    """Load pre-generated transcripts from Meetecho recording player format."""

    def load_transcript(self, transcript_path: Path) -> TranscriptionResult | None:
        """Load a Meetecho transcript JSON file.

        Args:
            transcript_path: Path to transcript JSON

        Returns:
            TranscriptionResult or None if failed
        """
        try:
            with open(transcript_path) as f:
                data = json.load(f)

            # Meetecho format has segments with timestamps
            segments = []
            full_text = []

            for i, entry in enumerate(data.get("entries", data.get("transcript", []))):
                text = entry.get("text", entry.get("content", ""))
                start = entry.get("start", entry.get("timestamp", 0))
                end = entry.get("end", start + 5)  # Default 5 second segments

                if isinstance(start, str):
                    # Parse timestamp string (e.g., "00:01:23")
                    start = self._parse_timestamp(start)
                if isinstance(end, str):
                    end = self._parse_timestamp(end)

                segments.append(
                    TranscriptSegment(
                        id=i,
                        start=float(start),
                        end=float(end),
                        text=text,
                        speaker=entry.get("speaker"),
                    )
                )
                full_text.append(text)

            return TranscriptionResult(
                text=" ".join(full_text),
                segments=segments,
                provider="meetecho",
            )

        except Exception as e:
            logger.error(f"Failed to load Meetecho transcript: {e}")
            return None

    def _parse_timestamp(self, ts: str) -> float:
        """Parse a timestamp string to seconds."""
        try:
            parts = ts.split(":")
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + float(s)
            elif len(parts) == 2:
                m, s = parts
                return int(m) * 60 + float(s)
            else:
                return float(ts)
        except Exception:
            return 0.0


def transcription_to_vcon_analysis(
    result: TranscriptionResult,
    dialog_index: int = 0,
) -> VConAnalysis:
    """Convert a TranscriptionResult to vCon WTF (World Transcription Format) analysis.

    The WTF extension provides a standardized format for transcriptions with:
    - Full transcript text with language detection
    - Time-aligned segments with optional speaker attribution
    - Confidence scores at segment and transcript level
    - Metadata about the transcription provider/model

    Args:
        result: Transcription result
        dialog_index: Index of the dialog this transcript belongs to

    Returns:
        VConAnalysis object in WTF format
    """
    # Calculate average confidence if segments have confidence scores
    confidences = [seg.confidence for seg in result.segments if seg.confidence is not None]
    avg_confidence = sum(confidences) / len(confidences) if confidences else None

    # Build WTF (World Transcription Format) body
    # Following draft-howe-wtf-transcription
    wtf_body = {
        "transcript": {
            "text": result.text,
            "language": result.language or "en",
            "duration": result.duration,
            "confidence": avg_confidence,
        },
        "segments": [
            {
                "id": seg.id if seg.id is not None else i,
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text,
                # Only include speaker if present (party index in vCon)
                **({"speaker": seg.speaker} if seg.speaker is not None else {}),
                # Only include confidence if present
                **({"confidence": round(seg.confidence, 4)} if seg.confidence is not None else {}),
            }
            for i, seg in enumerate(result.segments)
        ],
        "metadata": {
            "created_at": datetime.utcnow().isoformat() + "Z",
            "provider": result.provider,
            "model": result.model,
            "segment_count": len(result.segments),
        },
    }

    return VConAnalysis(
        type="wtf_transcription",  # WTF extension type
        dialog=dialog_index,
        vendor=result.provider,
        spec="draft-howe-wtf-transcription-00",  # Explicit draft reference for traceability
        body=wtf_body,
        encoding="none",
    )


def transcript_to_srt(result: TranscriptionResult) -> str:
    """Convert a TranscriptionResult to SRT subtitle format.

    Args:
        result: Transcription result

    Returns:
        SRT formatted string
    """
    lines = []

    for i, seg in enumerate(result.segments, 1):
        # SRT uses comma for milliseconds
        start_time = _seconds_to_srt_time(seg.start)
        end_time = _seconds_to_srt_time(seg.end)

        lines.append(str(i))
        lines.append(f"{start_time} --> {end_time}")
        lines.append(seg.text)
        lines.append("")  # Empty line between entries

    return "\n".join(lines)


def transcript_to_webvtt(result: TranscriptionResult) -> str:
    """Convert a TranscriptionResult to WebVTT subtitle format.

    Args:
        result: Transcription result

    Returns:
        WebVTT formatted string
    """
    lines = ["WEBVTT", ""]

    for i, seg in enumerate(result.segments, 1):
        # WebVTT uses period for milliseconds
        start_time = _seconds_to_webvtt_time(seg.start)
        end_time = _seconds_to_webvtt_time(seg.end)

        lines.append(f"{i}")
        lines.append(f"{start_time} --> {end_time}")
        lines.append(seg.text)
        lines.append("")

    return "\n".join(lines)


def _seconds_to_srt_time(seconds: float) -> str:
    """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _seconds_to_webvtt_time(seconds: float) -> str:
    """Convert seconds to WebVTT timestamp format (HH:MM:SS.mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
