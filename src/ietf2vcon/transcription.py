"""Transcription services for IETF session audio/video.

Supports multiple transcription backends:
- OpenAI Whisper (local, optional dependency)
- MLX Whisper (Apple Silicon, via vcon-mac-wtf sidecar)
- WTF Server (multi-provider REST API)
- YouTube captions (pre-generated)
- Meetecho pre-generated transcripts
"""

import base64
import json
import logging
import math
import mimetypes
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    """A single segment of a transcript."""

    start: float
    end: float
    text: str
    id: int | None = None
    speaker: str | None = None
    confidence: float | None = None


@dataclass
class TranscriptionResult:
    """Result from a transcription service."""

    text: str
    segments: list[TranscriptSegment]
    language: str | None = None
    duration: float | None = None
    provider: str = "unknown"
    model: str | None = None


# ---------------------------------------------------------------------------
# Server-based transcription backends
# ---------------------------------------------------------------------------


class MlxWhisperTranscriber:
    """Transcribe audio via MLX Whisper (Apple Silicon).

    Connects to the vcon-mac-wtf Python sidecar running an OpenAI-compatible
    transcription API at ``/v1/audio/transcriptions``.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        model: str = "mlx-community/whisper-turbo",
        timeout: float = 600.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def is_available(self) -> bool:
        """Check if the MLX Whisper sidecar is reachable."""
        for path in ("/health", "/v1/models"):
            try:
                resp = httpx.get(f"{self.base_url}{path}", timeout=5.0)
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                continue
        return False

    def transcribe(self, audio_path: Path) -> TranscriptionResult | None:
        """Transcribe an audio file via MLX Whisper.

        Sends the audio as multipart form data and requests verbose_json
        with word + segment timestamp granularities.
        """
        try:
            content_type = _guess_content_type(audio_path)

            with open(audio_path, "rb") as f:
                audio_data = f.read()

            logger.info(
                "Starting MLX Whisper transcription: %s (%d bytes)",
                audio_path.name,
                len(audio_data),
            )

            files = {"file": (audio_path.name, audio_data, content_type)}
            data = {
                "model": self.model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": ["word", "segment"],
            }

            resp = httpx.post(
                f"{self.base_url}/v1/audio/transcriptions",
                files=files,
                data=data,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            result = resp.json()

            logger.info(
                "MLX Whisper transcription completed: duration=%.1fs, text_length=%d",
                result.get("duration", 0),
                len(result.get("text", "")),
            )

            return self._parse_verbose_json(result)

        except Exception as e:
            logger.error("MLX Whisper transcription failed: %s", e)
            return None

    def _parse_verbose_json(self, data: dict) -> TranscriptionResult:
        """Parse an OpenAI-style verbose_json response."""
        segments = []
        for i, seg in enumerate(data.get("segments", [])):
            confidence = 0.95
            if seg.get("avg_logprob") is not None:
                confidence = min(math.exp(seg["avg_logprob"]), 1.0)

            segments.append(
                TranscriptSegment(
                    id=i,
                    start=seg["start"],
                    end=seg["end"],
                    text=seg["text"].strip(),
                    confidence=confidence,
                )
            )

        return TranscriptionResult(
            text=data.get("text", "").strip(),
            segments=segments,
            language=data.get("language"),
            duration=data.get("duration"),
            provider="mlx-whisper",
            model=self.model,
        )


class WtfServerTranscriber:
    """Transcribe audio via the WTF Server REST API.

    Builds a minimal vCon with base64url-encoded audio, POSTs it to the
    server's ``/transcribe`` endpoint, and extracts the WTF analysis from
    the enriched vCon response.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:3000",
        provider: str | None = None,
        model: str | None = None,
        timeout: float = 600.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.provider = provider
        self.model = model
        self.timeout = timeout

    def is_available(self) -> bool:
        """Check if the WTF Server is reachable."""
        try:
            resp = httpx.get(f"{self.base_url}/health", timeout=5.0)
            if resp.status_code == 200:
                body = resp.json()
                return body.get("status") == "ok"
        except httpx.HTTPError:
            pass
        return False

    def transcribe(self, audio_path: Path) -> TranscriptionResult | None:
        """Transcribe an audio file via the WTF Server.

        Wraps the audio in a minimal vCon and POSTs to ``/transcribe``.
        The server returns an enriched vCon with WTF analysis attached.
        """
        try:
            content_type = _guess_content_type(audio_path)

            with open(audio_path, "rb") as f:
                audio_data = f.read()

            encoded = base64.urlsafe_b64encode(audio_data).decode("ascii")

            logger.info(
                "Starting WTF Server transcription: %s (%d bytes, provider=%s)",
                audio_path.name,
                len(audio_data),
                self.provider or "default",
            )

            # Build minimal vCon with inline audio
            vcon_payload = {
                "vcon": "0.0.1",
                "parties": [{"name": "speaker"}],
                "dialog": [
                    {
                        "type": "recording",
                        "start": "2024-01-01T00:00:00Z",
                        "parties": [0],
                        "mediatype": content_type,
                        "body": encoded,
                        "encoding": "base64url",
                    }
                ],
                "analysis": [],
                "attachments": [],
            }

            # Build query params for provider/model selection
            params = {}
            if self.provider:
                params["provider"] = self.provider
            if self.model:
                params["model"] = self.model

            resp = httpx.post(
                f"{self.base_url}/transcribe",
                json=vcon_payload,
                params=params,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            enriched = resp.json()

            provider_header = resp.headers.get("X-Provider", self.provider or "wtf-server")
            model_header = resp.headers.get("X-Model")

            return self._extract_transcription(enriched, provider_header, model_header)

        except Exception as e:
            logger.error("WTF Server transcription failed: %s", e)
            return None

    def _extract_transcription(
        self,
        vcon_data: dict,
        provider: str,
        model: str | None,
    ) -> TranscriptionResult | None:
        """Extract TranscriptionResult from an enriched vCon response.

        Looks for WTF transcription analysis in the response.
        """
        for analysis in vcon_data.get("analysis", []):
            if analysis.get("type") != "wtf_transcription":
                continue

            body = analysis.get("body", {})
            if isinstance(body, str):
                body = json.loads(body)

            transcript = body.get("transcript", {})
            raw_segments = body.get("segments", [])

            segments = []
            for i, seg in enumerate(raw_segments):
                segments.append(
                    TranscriptSegment(
                        id=seg.get("id", i),
                        start=seg["start"],
                        end=seg["end"],
                        text=seg["text"],
                        speaker=seg.get("speaker"),
                        confidence=seg.get("confidence"),
                    )
                )

            metadata = body.get("metadata", {})

            return TranscriptionResult(
                text=transcript.get("text", ""),
                segments=segments,
                language=transcript.get("language"),
                duration=transcript.get("duration"),
                provider=metadata.get("provider", provider),
                model=metadata.get("model", model),
            )

        logger.warning("No WTF transcription analysis found in server response")
        return None


# ---------------------------------------------------------------------------
# Local transcription backends
# ---------------------------------------------------------------------------


class WhisperTranscriber:
    """Transcribe audio using OpenAI Whisper (local, optional dependency)."""

    def __init__(self, model: str = "base"):
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
                    "whisper not installed. Install with: pip install ietf2vcon[whisper]"
                )
        return self._whisper

    def transcribe(self, audio_path: Path) -> TranscriptionResult | None:
        """Transcribe an audio file using local Whisper."""
        try:
            whisper = self._load_whisper()
            logger.info("Loading Whisper model: %s", self.model)
            model = whisper.load_model(self.model)

            logger.info("Transcribing: %s", audio_path)
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
            logger.error("Whisper transcription failed: %s", e)
            return None

    def transcribe_with_cli(self, audio_path: Path) -> TranscriptionResult | None:
        """Transcribe using whisper CLI (alternative to Python API)."""
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
                timeout=3600,
            )

            if result.returncode != 0:
                logger.error("Whisper CLI failed: %s", result.stderr)
                return None

            json_path = output_dir / f"{output_base}.json"
            if not json_path.exists():
                logger.error("Whisper output not found: %s", json_path)
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
            logger.error("Whisper CLI failed: %s", e)

        return None


# ---------------------------------------------------------------------------
# Pre-generated transcript loaders
# ---------------------------------------------------------------------------


class YouTubeCaptionLoader:
    """Load transcripts from YouTube caption files (JSON3 format)."""

    def load_captions(self, caption_path: Path) -> TranscriptionResult | None:
        """Load a YouTube JSON3 caption file."""
        try:
            with open(caption_path) as f:
                data = json.load(f)

            segments = []
            full_text = []

            events = data.get("events", [])

            segment_id = 0
            for event in events:
                if "segs" not in event:
                    continue

                start_ms = event.get("tStartMs", 0)
                duration_ms = event.get("dDurationMs", 0)

                start_sec = start_ms / 1000.0
                end_sec = (start_ms + duration_ms) / 1000.0

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

            duration = segments[-1].end if segments else None

            return TranscriptionResult(
                text=" ".join(full_text),
                segments=segments,
                language="en",
                duration=duration,
                provider="youtube",
                model="auto-generated",
            )

        except Exception as e:
            logger.error("Failed to load YouTube captions: %s", e)
            return None


class MeetechoTranscriptLoader:
    """Load pre-generated transcripts from Meetecho recording player format."""

    def load_transcript(self, transcript_path: Path) -> TranscriptionResult | None:
        """Load a Meetecho transcript JSON file."""
        try:
            with open(transcript_path) as f:
                data = json.load(f)

            segments = []
            full_text = []

            for i, entry in enumerate(data.get("entries", data.get("transcript", []))):
                text = entry.get("text", entry.get("content", ""))
                start = entry.get("start", entry.get("timestamp", 0))
                end = entry.get("end", start + 5)

                if isinstance(start, str):
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
            logger.error("Failed to load Meetecho transcript: %s", e)
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


# ---------------------------------------------------------------------------
# Subtitle export helpers
# ---------------------------------------------------------------------------


def transcript_to_srt(result: TranscriptionResult) -> str:
    """Convert a TranscriptionResult to SRT subtitle format."""
    lines = []

    for i, seg in enumerate(result.segments, 1):
        start_time = _seconds_to_srt_time(seg.start)
        end_time = _seconds_to_srt_time(seg.end)

        lines.append(str(i))
        lines.append(f"{start_time} --> {end_time}")
        lines.append(seg.text)
        lines.append("")

    return "\n".join(lines)


def transcript_to_webvtt(result: TranscriptionResult) -> str:
    """Convert a TranscriptionResult to WebVTT subtitle format."""
    lines = ["WEBVTT", ""]

    for i, seg in enumerate(result.segments, 1):
        start_time = _seconds_to_webvtt_time(seg.start)
        end_time = _seconds_to_webvtt_time(seg.end)

        lines.append(f"{i}")
        lines.append(f"{start_time} --> {end_time}")
        lines.append(seg.text)
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backend availability
# ---------------------------------------------------------------------------


def check_backend_availability(
    mlx_whisper_url: str | None = None,
    wtf_server_url: str | None = None,
) -> dict[str, bool]:
    """Probe configured transcription backends and return availability."""
    availability: dict[str, bool] = {}

    if mlx_whisper_url:
        t = MlxWhisperTranscriber(base_url=mlx_whisper_url)
        availability["mlx-whisper"] = t.is_available()

    if wtf_server_url:
        t = WtfServerTranscriber(base_url=wtf_server_url)
        availability["wtf-server"] = t.is_available()

    # Local whisper: check if the package is importable
    try:
        import whisper  # noqa: F401
        availability["whisper"] = True
    except ImportError:
        availability["whisper"] = False

    return availability


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _guess_content_type(audio_path: Path) -> str:
    """Guess the MIME type for an audio file."""
    mime, _ = mimetypes.guess_type(str(audio_path))
    return mime or "audio/wav"


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
