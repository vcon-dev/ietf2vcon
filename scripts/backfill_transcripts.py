#!/usr/bin/env python3
"""Backfill transcripts for existing IETF vCon files using MLX Whisper.

Scans existing vCon files, finds those with YouTube video but no transcript,
downloads audio, transcribes via MLX Whisper sidecar, and patches the vCon.

Usage:
    # Start the MLX Whisper sidecar first:
    cd /path/to/vcon-mac-wtf && make run

    # Then run this script:
    python scripts/backfill_transcripts.py 124
    python scripts/backfill_transcripts.py 124 --mlx-whisper-url http://localhost:8000
    python scripts/backfill_transcripts.py 124 --dry-run
    python scripts/backfill_transcripts.py 124 --groups vcon httpbis
"""

import argparse
import json
import logging
import math
import re
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

import httpx

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

console = Console()
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from a URL."""
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"youtube\.com/embed/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def scan_vcons(vcon_dir: Path) -> list[dict]:
    """Scan vCon files and return info about each."""
    results = []
    for f in sorted(vcon_dir.glob("*.vcon.json")):
        with open(f) as fh:
            data = json.load(fh)

        # Find video URL
        video_url = None
        for d in data.get("dialog", []):
            if d.get("type") == "video" and d.get("url"):
                url = d["url"]
                if "youtube.com" in url or "youtu.be" in url:
                    video_url = url
                    break

        # Check for existing transcript
        has_transcript = False
        for a in data.get("attachments", []) + data.get("analysis", []):
            atype = a.get("type") or a.get("purpose") or ""
            if "wtf_transcription" in atype or "transcript" in atype.lower():
                has_transcript = True
                break

        group = data.get("subject", "")
        # Extract group acronym from filename
        parts = f.stem.replace(".vcon", "").split("_")
        group_acronym = parts[1] if len(parts) > 1 else "unknown"

        results.append({
            "file": f,
            "group": group_acronym,
            "subject": data.get("subject", ""),
            "video_url": video_url,
            "has_transcript": has_transcript,
            "dialog_count": len(data.get("dialog", [])),
        })

    return results


def _find_yt_dlp() -> str:
    """Find yt-dlp executable, checking venv first."""
    import shutil

    # Check if running inside a venv
    venv_bin = Path(sys.executable).parent / "yt-dlp"
    if venv_bin.exists():
        return str(venv_bin)

    # Fall back to PATH
    found = shutil.which("yt-dlp")
    if found:
        return found

    raise FileNotFoundError(
        "yt-dlp not found. Install with: pip install yt-dlp"
    )


def download_audio(video_url: str, output_path: Path) -> Path | None:
    """Download audio from YouTube using yt-dlp."""
    import subprocess

    video_id = extract_video_id(video_url)
    if not video_id:
        logger.error("Cannot extract video ID from: %s", video_url)
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove extension since yt-dlp adds it
    output_template = str(output_path)
    if output_template.endswith(".mp3"):
        output_template = output_template[:-4]

    try:
        yt_dlp = _find_yt_dlp()

        # Ensure homebrew bin is on PATH for ffmpeg, node, etc.
        import os
        env = os.environ.copy()
        extra_paths = ["/opt/homebrew/bin", "/usr/local/bin"]
        current = env.get("PATH", "")
        for p in extra_paths:
            if p not in current:
                env["PATH"] = p + ":" + current
                current = env["PATH"]

        result = subprocess.run(
            [
                yt_dlp,
                "--js-runtimes", "node:/opt/homebrew/bin",
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "5",  # Lower quality OK for speech
                "--postprocessor-args", "ffmpeg:-ac 1 -ar 16000",  # Mono 16kHz for Whisper
                "-o", output_template + ".%(ext)s",
                "--print", "after_move:filepath",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )

        # yt-dlp may return non-zero with warnings but still succeed
        filepath = result.stdout.strip().split("\n")[-1] if result.stdout.strip() else ""
        downloaded = Path(filepath) if filepath else None

        if downloaded and downloaded.exists():
            return downloaded

        # Fallback: look for the file with expected name patterns
        for ext in (".mp3", ".m4a", ".webm", ".opus"):
            candidate = Path(output_template + ext)
            if candidate.exists():
                return candidate

        # Also check exact output_path
        if output_path.exists():
            return output_path

        if result.returncode != 0:
            logger.error("yt-dlp failed (exit %d): %s", result.returncode, result.stderr[:300])
            return None

    except subprocess.TimeoutExpired:
        logger.error("yt-dlp timed out for %s", video_url)
    except FileNotFoundError:
        logger.error("yt-dlp not found. Install with: pip install yt-dlp")
    except Exception as e:
        logger.error("Audio download failed: %s", e)

    return None


CHUNK_DURATION_SECS = 600  # 10-minute chunks


def _get_audio_duration(audio_path: Path) -> float | None:
    """Get audio duration in seconds using ffprobe."""
    import subprocess, os

    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True, text=True, timeout=10, env=env,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _split_audio(audio_path: Path, chunk_dir: Path, chunk_secs: int = CHUNK_DURATION_SECS) -> list[tuple[Path, float]]:
    """Split audio into chunks, returning list of (chunk_path, start_offset)."""
    import subprocess, os

    duration = _get_audio_duration(audio_path)
    if duration is None or duration <= chunk_secs:
        return [(audio_path, 0.0)]

    chunk_dir.mkdir(parents=True, exist_ok=True)
    stem = audio_path.stem

    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")

    chunks = []
    start = 0.0
    idx = 0
    while start < duration:
        chunk_path = chunk_dir / f"{stem}_chunk{idx:03d}.mp3"
        if not chunk_path.exists():
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", str(audio_path),
                    "-ss", str(start),
                    "-t", str(chunk_secs),
                    "-ac", "1", "-ar", "16000",
                    str(chunk_path),
                ],
                capture_output=True, timeout=60, env=env,
            )
        if chunk_path.exists():
            chunks.append((chunk_path, start))
        start += chunk_secs
        idx += 1

    return chunks if chunks else [(audio_path, 0.0)]


def _transcribe_chunk(
    audio_path: Path,
    mlx_url: str,
    model: str,
    timeout: float = 600.0,
    max_retries: int = 3,
) -> dict | None:
    """Transcribe a single audio chunk via MLX Whisper.

    Retries on transient server errors (5xx) with exponential backoff.
    """
    import mimetypes

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    content_type = mimetypes.guess_type(str(audio_path))[0] or "audio/mpeg"

    for attempt in range(1, max_retries + 1):
        files = {"file": (audio_path.name, audio_data, content_type)}
        data = {
            "model": model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
        }

        resp = httpx.post(
            f"{mlx_url}/v1/audio/transcriptions",
            files=files,
            data=data,
            timeout=timeout,
        )

        if resp.status_code < 500:
            resp.raise_for_status()
            return resp.json()

        # Server error — retry with backoff
        wait = 2 ** attempt  # 2, 4, 8 seconds
        if attempt < max_retries:
            logger.warning(
                "  Chunk %s: server error %d, retrying in %ds (attempt %d/%d)",
                audio_path.name, resp.status_code, wait, attempt, max_retries,
            )
            time.sleep(wait)
        else:
            logger.error(
                "  Chunk %s: server error %d after %d attempts, giving up",
                audio_path.name, resp.status_code, max_retries,
            )
            return None

    return None


def transcribe_audio(
    audio_path: Path,
    mlx_url: str,
    model: str = "mlx-community/whisper-turbo",
    timeout: float = 600.0,
) -> dict | None:
    """Transcribe audio via MLX Whisper, chunking long files.

    Returns a WTF transcription attachment dict.
    """
    try:
        duration = _get_audio_duration(audio_path)
        logger.info("Audio duration: %.0fs (%.1f min)", duration or 0, (duration or 0) / 60)

        # Decide whether to chunk
        if duration and duration > CHUNK_DURATION_SECS:
            chunk_dir = audio_path.parent / "chunks"
            chunks = _split_audio(audio_path, chunk_dir)
            logger.info("Split into %d chunks of %ds each", len(chunks), CHUNK_DURATION_SECS)
        else:
            chunks = [(audio_path, 0.0)]

        # Transcribe each chunk
        all_segments = []
        all_text_parts = []
        total_duration = 0.0
        language = "en"

        failed_chunks = 0
        for chunk_path, offset in chunks:
            logger.info("  Transcribing chunk: %s (offset=%.0fs)", chunk_path.name, offset)
            try:
                result = _transcribe_chunk(chunk_path, mlx_url, model, timeout=timeout)
            except Exception as e:
                logger.warning("  Chunk %s failed: %s", chunk_path.name, e)
                result = None
            if result is None:
                failed_chunks += 1
                continue

            language = result.get("language", language)
            chunk_duration = result.get("duration", 0.0)
            total_duration = max(total_duration, offset + chunk_duration)
            all_text_parts.append(result.get("text", "").strip())

            for seg in result.get("segments", []):
                confidence = 0.95
                if seg.get("avg_logprob") is not None:
                    confidence = min(math.exp(seg["avg_logprob"]), 1.0)

                entry = {
                    "id": len(all_segments),
                    "start": round(seg["start"] + offset, 3),
                    "end": round(seg["end"] + offset, 3),
                    "text": seg["text"].strip(),
                    "confidence": round(confidence, 4),
                }
                all_segments.append(entry)

        if failed_chunks:
            logger.warning("  %d/%d chunks failed", failed_chunks, len(chunks))

        if not all_segments:
            logger.error("No segments produced from transcription")
            return None

        text = " ".join(all_text_parts)
        confidences = [s["confidence"] for s in all_segments]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.95

        now = datetime.now(UTC).isoformat()

        wtf_body = {
            "transcript": {
                "text": text,
                "language": language,
                "duration": total_duration,
                "confidence": round(avg_confidence, 4),
            },
            "segments": all_segments,
            "metadata": {
                "created_at": now,
                "processed_at": now,
                "provider": "mlx-whisper",
                "model": model,
            },
        }

        return {
            "type": "wtf_transcription",
            "encoding": "json",
            "body": wtf_body,
        }

    except Exception as e:
        logger.error("Transcription failed: %s", e)
        return None


def check_mlx_available(url: str) -> bool:
    """Check if MLX Whisper sidecar is reachable."""
    for path in ("/health", "/v1/models"):
        try:
            resp = httpx.get(f"{url}{path}", timeout=5.0)
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            continue
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Backfill transcripts for existing IETF vCon files"
    )
    parser.add_argument("meeting", type=int, help="IETF meeting number")
    parser.add_argument(
        "--vcon-dir",
        type=Path,
        help="vCon directory (default: ./output/ietf<meeting>)",
    )
    parser.add_argument(
        "--audio-dir",
        type=Path,
        help="Audio cache directory (default: ./output/audio)",
    )
    parser.add_argument(
        "--mlx-whisper-url",
        type=str,
        default="http://localhost:8000",
        help="MLX Whisper sidecar URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--mlx-whisper-model",
        type=str,
        default="mlx-community/whisper-turbo",
        help="MLX Whisper model",
    )
    parser.add_argument(
        "--groups",
        type=str,
        nargs="+",
        help="Only process these groups",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip audio download (use cached audio only)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    vcon_dir = args.vcon_dir or Path(f"./output/ietf{args.meeting}")
    audio_dir = args.audio_dir or Path("./output/audio")

    if not vcon_dir.exists():
        console.print(f"[red]vCon directory not found: {vcon_dir}[/red]")
        sys.exit(1)

    console.print(f"\n[bold]Backfill Transcripts - IETF {args.meeting}[/bold]\n")

    # Scan vCons
    console.print("Scanning vCon files...")
    all_vcons = scan_vcons(vcon_dir)
    console.print(f"Found [cyan]{len(all_vcons)}[/cyan] vCon files")

    # Filter to those needing transcripts
    needs_transcript = [
        v for v in all_vcons
        if v["video_url"] and not v["has_transcript"]
    ]

    # Filter by groups if specified
    if args.groups:
        needs_transcript = [
            v for v in needs_transcript
            if v["group"] in args.groups
        ]

    already_done = sum(1 for v in all_vcons if v["has_transcript"])
    no_video = sum(1 for v in all_vcons if not v["video_url"])

    console.print(f"  With transcript: [green]{already_done}[/green]")
    console.print(f"  No video URL: [yellow]{no_video}[/yellow]")
    console.print(f"  Needs transcript: [cyan]{len(needs_transcript)}[/cyan]\n")

    if not needs_transcript:
        console.print("[green]All sessions already have transcripts![/green]")
        return

    if args.dry_run:
        table = Table(title="Sessions needing transcripts (dry run)")
        table.add_column("Group", style="cyan")
        table.add_column("Video URL")
        table.add_column("File")

        for v in needs_transcript:
            table.add_row(
                v["group"],
                v["video_url"][:50] + "..." if v["video_url"] else "-",
                v["file"].name,
            )

        console.print(table)
        console.print(f"\nTotal: {len(needs_transcript)} sessions to process")
        return

    # Check MLX Whisper availability
    console.print(f"Checking MLX Whisper at {args.mlx_whisper_url}...")
    if not check_mlx_available(args.mlx_whisper_url):
        console.print(
            f"[red]MLX Whisper sidecar not available at {args.mlx_whisper_url}[/red]\n"
            "Start it with:\n"
            "  cd /path/to/vcon-mac-wtf && make run"
        )
        sys.exit(1)
    console.print("[green]MLX Whisper sidecar is running[/green]\n")

    # Process each vCon
    success_count = 0
    error_count = 0
    skip_count = 0
    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Transcribing...", total=len(needs_transcript))

        for v in needs_transcript:
            group = v["group"]
            progress.update(task, description=f"Processing {group}...")

            # Step 1: Ensure audio is available
            audio_path = audio_dir / f"ietf{args.meeting}_{group}.mp3"

            if not audio_path.exists() and not args.skip_download:
                audio_path.parent.mkdir(parents=True, exist_ok=True)
                downloaded = download_audio(v["video_url"], audio_path)
                if downloaded:
                    audio_path = downloaded
                else:
                    logger.warning("Skipping %s: audio download failed", group)
                    results.append((group, False, "Audio download failed"))
                    error_count += 1
                    progress.update(task, advance=1)
                    continue

            if not audio_path.exists():
                logger.warning("Skipping %s: no audio available", group)
                results.append((group, False, "No audio"))
                skip_count += 1
                progress.update(task, advance=1)
                continue

            # Step 2: Transcribe
            start_time = time.time()
            wtf_attachment = transcribe_audio(
                audio_path,
                args.mlx_whisper_url,
                model=args.mlx_whisper_model,
            )
            elapsed = time.time() - start_time

            if not wtf_attachment:
                logger.warning("Skipping %s: transcription failed", group)
                results.append((group, False, "Transcription failed"))
                error_count += 1
                progress.update(task, advance=1)
                continue

            seg_count = len(wtf_attachment["body"].get("segments", []))

            # Step 3: Patch the vCon file
            with open(v["file"]) as fh:
                vcon_data = json.load(fh)

            vcon_data["attachments"].append(wtf_attachment)
            vcon_data["updated_at"] = datetime.now(UTC).isoformat()

            with open(v["file"], "w") as fh:
                json.dump(vcon_data, fh, indent=2, ensure_ascii=False)

            logger.info(
                "%s: %d segments in %.1fs",
                group, seg_count, elapsed,
            )
            results.append((group, True, f"{seg_count} segments ({elapsed:.0f}s)"))
            success_count += 1
            progress.update(task, advance=1)

    # Display results
    console.print("\n")

    table = Table(title=f"IETF {args.meeting} Transcript Backfill Results")
    table.add_column("Group", style="cyan")
    table.add_column("Status")
    table.add_column("Details")

    for group, success, message in sorted(results):
        if success:
            table.add_row(group, "[green]\u2713[/green]", message)
        else:
            table.add_row(group, "[red]\u2717[/red]", message)

    console.print(table)
    console.print(
        f"\n[bold]Summary:[/bold] "
        f"[green]{success_count} success[/green], "
        f"[red]{error_count} errors[/red], "
        f"[yellow]{skip_count} skipped[/yellow]"
    )

    if error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
