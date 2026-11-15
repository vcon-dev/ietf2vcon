"""Command-line interface for ietf2vcon.

Convert IETF meeting sessions to vCon format.
"""

import logging
import sys
import threading
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .converter import ConversionOptions, IETFSessionConverter, remove_stale_partials
from .errors import SessionReadError
from .rsync_mirror import sync_proceedings

console = Console()
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool):
    """Configure logging with rich output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def _apply_rate(max_rps: float | None) -> None:
    """Set the shared request throttle from --max-rps (else the environment/default stays)."""
    from .httpclient import THROTTLE

    if max_rps is not None:
        THROTTLE.set_rate(max_rps)


def check_mirror(rsync_mirror: Path | None, meeting) -> str | None:
    """Why a requested rsync mirror is unusable, or None.

    Asking for a mirror that is not there must not quietly fetch every
    material over HTTP. Interims are not in the mirror, so only a numbered
    meeting needs its proceedings directory.
    """
    if rsync_mirror is None:
        return None
    if not rsync_mirror.is_dir():
        return f"The rsync mirror {rsync_mirror} does not exist or is not a directory"
    if str(meeting).isdigit():
        proceedings = rsync_mirror / "proceedings" / str(meeting)
        if not proceedings.is_dir():
            return (
                f"The rsync mirror has no proceedings for IETF {meeting}: "
                f"{proceedings} is not a directory"
            )
    return None


def published_files(output_dir: Path, meeting) -> dict[str, list[str]]:
    """Group acronym -> session ids of the published files for a meeting in ``output_dir``."""
    import re

    slug = IETFSessionConverter._meeting_slug(meeting)
    pattern = re.compile(rf"^{re.escape(slug)}_([^_]+)_(\d+)\.vcon\.json$")
    found: dict[str, list[str]] = {}
    if output_dir.is_dir():
        for path in output_dir.iterdir():
            if m := pattern.match(path.name):
                found.setdefault(m.group(1), []).append(m.group(2))
    return {group: sorted(ids, key=int) for group, ids in found.items()}


MAX_RPS_OPTION = click.option(
    "--max-rps",
    type=float,
    default=None,
    help="Most requests per second to the Datatracker and ietf.org, shared by all "
         "of them (default 8; also IETF2VCON_MAX_RPS; 0 turns the throttle off)",
)


@click.group()
@click.version_option(version=__version__, prog_name="ietf2vcon")
def main():
    """Convert IETF meeting sessions to vCon format.

    This tool fetches IETF meeting recordings, materials, transcripts,
    and chat logs, combining them into a vCon (Virtual Conversation Container).

    Transcription backends (in auto priority order):
      YouTube captions > Meetecho > MLX Whisper > WTF Server > local Whisper

    Examples:

        # Convert IETF 121 vcon working group session (includes transcript)
        ietf2vcon convert --meeting 121 --group vcon

        # Use MLX Whisper (Apple Silicon) for transcription
        ietf2vcon convert --meeting 121 --group vcon \\
            --transcript-source mlx-whisper --mlx-whisper-url http://localhost:8000

        # Use WTF Server with a specific ASR provider
        ietf2vcon convert --meeting 121 --group vcon \\
            --transcript-source wtf-server --wtf-server-url http://localhost:3000 \\
            --wtf-server-provider nvidia

        # Include Zulip chat logs
        ietf2vcon convert --meeting 121 --group vcon \\
            --zulip-email user@example.com --zulip-api-key YOUR_KEY
    """
    pass


def _meeting_number(ctx, param, value):
    """Accept either a numbered meeting or an interim name.

    The Datatracker's `number` field holds both: 124 for a numbered meeting,
    interim-2026-vcon-02 for an interim. Digits are normalized back to int so
    the meeting number written into the vCon keeps the type it has always had.
    """
    if value is None:
        return None
    return int(value) if str(value).isdigit() else value


@main.command()
@click.option(
    "-m", "--meeting",
    type=str,
    callback=_meeting_number,
    required=True,
    help="IETF meeting number (e.g., 124) or interim name "
         "(e.g., interim-2026-vcon-02)",
)
@click.option(
    "-g", "--group",
    type=str,
    required=True,
    help="Working group acronym (e.g., vcon, httpbis)",
)
@click.option(
    "-s", "--session",
    type=int,
    default=0,
    help="Session index if multiple sessions (0 for first)",
)
@click.option(
    "-o", "--output",
    type=click.Path(path_type=Path),
    help="Output file path (default: ./output/<meeting>_<group>.vcon.json)",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=Path("./output"),
    help="Output directory for downloads and vCon files",
)
@click.option(
    "--video-source",
    type=click.Choice(["youtube", "meetecho", "both"]),
    default="youtube",
    help="Video source preference",
)
@click.option(
    "--download-video",
    is_flag=True,
    help="Download video and embed inline (creates large vCon)",
)
@click.option(
    "--no-video",
    is_flag=True,
    help="Skip video recording",
)
@click.option(
    "--no-materials",
    is_flag=True,
    help="Skip meeting materials (slides, agenda, etc.)",
)
@click.option(
    "--inline-materials",
    is_flag=True,
    help="Download and embed materials inline",
)
@click.option(
    "--no-transcript",
    is_flag=True,
    help="Skip transcript (YouTube captions are fetched by default)",
)
@click.option(
    "--transcript-source",
    type=click.Choice(["auto", "youtube", "meetecho", "mlx-whisper", "wtf-server", "whisper"]),
    default="auto",
    help="Transcript source: auto tries YouTube > Meetecho > MLX Whisper > WTF Server > local Whisper",
)
@click.option(
    "--export-srt",
    is_flag=True,
    help="Export transcript as SRT subtitle file",
)
@click.option(
    "--export-webvtt",
    is_flag=True,
    help="Export transcript as WebVTT subtitle file",
)
@click.option(
    "--whisper-model",
    type=click.Choice(["tiny", "base", "small", "medium", "large"]),
    default="base",
    help="Whisper model size (only used with --transcript-source=whisper)",
)
@click.option(
    "--mlx-whisper-url",
    type=str,
    envvar="MLX_WHISPER_URL",
    help="MLX Whisper sidecar URL (or set MLX_WHISPER_URL env var)",
)
@click.option(
    "--mlx-whisper-model",
    type=str,
    envvar="MLX_WHISPER_MODEL",
    default="mlx-community/whisper-turbo",
    help="MLX Whisper model name",
)
@click.option(
    "--wtf-server-url",
    type=str,
    envvar="WTF_SERVER_URL",
    help="WTF Server URL (or set WTF_SERVER_URL env var)",
)
@click.option(
    "--wtf-server-provider",
    type=click.Choice(
        ["nvidia", "openai", "deepgram", "groq", "local-whisper", "mlx-whisper"]
    ),
    envvar="WTF_SERVER_PROVIDER",
    help="WTF Server ASR provider (or set WTF_SERVER_PROVIDER env var)",
)
@click.option(
    "--wtf-server-model",
    type=str,
    envvar="WTF_SERVER_MODEL",
    help="WTF Server model override",
)
@click.option(
    "--no-chat",
    is_flag=True,
    help="Skip Zulip chat logs",
)
@click.option(
    "--zulip-email",
    type=str,
    envvar="ZULIP_EMAIL",
    help="Zulip email (or set ZULIP_EMAIL env var)",
)
@click.option(
    "--zulip-api-key",
    type=str,
    envvar="ZULIP_API_KEY",
    help="Zulip API key (or set ZULIP_API_KEY env var)",
)
@click.option(
    "--rsync-mirror",
    type=click.Path(path_type=Path),
    envvar="IETF_RSYNC_MIRROR",
    help="Local rsync mirror root (checked before HTTP for materials). "
         "Use 'ietf2vcon sync' to populate it.",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    help="Enable verbose output",
)
@MAX_RPS_OPTION
def convert(
    meeting: int,
    group: str,
    session: int,
    output: Path | None,
    output_dir: Path,
    video_source: str,
    download_video: bool,
    no_video: bool,
    no_materials: bool,
    inline_materials: bool,
    no_transcript: bool,
    transcript_source: str,
    export_srt: bool,
    export_webvtt: bool,
    whisper_model: str,
    mlx_whisper_url: str | None,
    mlx_whisper_model: str,
    wtf_server_url: str | None,
    wtf_server_provider: str | None,
    wtf_server_model: str | None,
    no_chat: bool,
    zulip_email: str | None,
    zulip_api_key: str | None,
    rsync_mirror: Path | None,
    verbose: bool,
    max_rps: float | None,
):
    """Convert an IETF session to vCon format.

    By default, fetches YouTube captions for the transcript in WTF format.
    Use --no-transcript to skip, or --transcript-source to select a backend.
    Use --export-srt or --export-webvtt to generate subtitle files.
    """
    setup_logging(verbose)
    _apply_rate(max_rps)

    problem = check_mirror(rsync_mirror, meeting)
    if problem:
        console.print(f"[red]Error:[/red] {problem}", markup=True)
        sys.exit(1)

    console.print(Panel(
        f"Converting IETF {meeting} - {group.upper()} session {session}",
        title="ietf2vcon",
    ))

    # Build options
    options = ConversionOptions(
        include_video=not no_video,
        video_source=video_source,
        download_video=download_video,
        include_materials=not no_materials,
        inline_materials=inline_materials,
        include_transcript=not no_transcript,
        transcription_source=transcript_source,
        export_srt=export_srt,
        export_webvtt=export_webvtt,
        whisper_model=whisper_model,
        mlx_whisper_url=mlx_whisper_url,
        mlx_whisper_model=mlx_whisper_model,
        wtf_server_url=wtf_server_url,
        wtf_server_provider=wtf_server_provider,
        wtf_server_model=wtf_server_model,
        include_chat=not no_chat,
        zulip_email=zulip_email,
        zulip_api_key=zulip_api_key,
        output_dir=output_dir,
        rsync_mirror_dir=rsync_mirror,
    )

    # Run conversion
    converter = IETFSessionConverter(options)

    try:
        converter.remove_stale_partials()
        with console.status("Converting session..."):
            result = converter.convert_session(meeting, group, session)

        # A session that could not be converted completely saves nothing.
        if result.errors:
            for error in result.errors:
                console.print(f"[red]Error:[/red] {escape(error)}")
            sys.exit(1)

        # Save output
        output_path = converter.save_vcon(result, output)

        # Display results
        _display_results(result, output_path)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        if verbose:
            console.print_exception()
        sys.exit(1)


@main.command()
@click.option(
    "-m", "--meeting",
    type=str,
    callback=_meeting_number,
    required=True,
    help="IETF meeting number, or interim name (interim-2026-vcon-02)",
)
@click.option(
    "-g", "--group",
    type=str,
    help="Filter by working group acronym",
)
def list_sessions(meeting: int, group: str | None):
    """List available sessions for an IETF meeting."""
    setup_logging(False)

    from .datatracker import DataTrackerClient

    console.print(f"Fetching sessions for IETF {meeting}...")

    client = DataTrackerClient()
    try:
        try:
            if group:
                sessions = client.get_group_sessions(meeting, group)
            else:
                sessions = client.get_meeting_sessions(meeting)
        except SessionReadError as e:
            console.print(f"[red]Error:[/red] {escape(str(e))}")
            sys.exit(1)

        if not sessions:
            console.print("[yellow]No sessions found[/yellow]")
            return

        table = Table(title=f"IETF {meeting} Sessions")
        table.add_column("Group", style="cyan")
        table.add_column("Session ID")
        table.add_column("Start Time")
        table.add_column("Duration")
        table.add_column("Room")

        for sess in sorted(sessions, key=lambda s: (s.group_acronym, s.session_id)):
            start = sess.start_time.strftime("%Y-%m-%d %H:%M") if sess.start_time else "-"
            duration = f"{sess.duration_seconds // 60}m" if sess.duration_seconds else "-"
            table.add_row(
                sess.group_acronym,
                sess.session_id,
                start,
                duration,
                sess.room or "-",
            )

        console.print(table)

    finally:
        client.close()


@main.command()
@click.option(
    "-m", "--meeting",
    type=str,
    callback=_meeting_number,
    required=True,
    help="IETF meeting number, or interim name (interim-2026-vcon-02)",
)
@click.option(
    "-g", "--group",
    type=str,
    required=True,
    help="Working group acronym",
)
def list_materials(meeting: int, group: str):
    """List available materials for a session."""
    setup_logging(False)

    from .datatracker import DataTrackerClient

    console.print(f"Fetching materials for IETF {meeting} {group}...")

    client = DataTrackerClient()
    try:
        materials = client.get_session_materials(meeting, group)

        if not materials:
            console.print("[yellow]No materials found[/yellow]")
            return

        table = Table(title=f"IETF {meeting} {group.upper()} Materials")
        table.add_column("Type", style="cyan")
        table.add_column("Title")
        table.add_column("URL")

        for mat in materials:
            table.add_row(
                mat.type,
                mat.title[:50] + "..." if len(mat.title) > 50 else mat.title,
                mat.url[:60] + "..." if len(mat.url) > 60 else mat.url,
            )

        console.print(table)

    finally:
        client.close()


@main.command("convert-all")
@click.option(
    "-m", "--meeting",
    type=str,
    callback=_meeting_number,
    required=True,
    help="IETF meeting number, or interim name (interim-2026-vcon-02)",
)
@click.option(
    "-o", "--output-dir",
    type=click.Path(path_type=Path),
    default=Path("./output"),
    help="Output directory for vCon files",
)
@click.option(
    "--no-transcript",
    is_flag=True,
    help="Skip transcript generation",
)
@click.option(
    "--transcript-source",
    type=click.Choice(["auto", "youtube", "meetecho", "mlx-whisper", "wtf-server", "whisper"]),
    default="auto",
    help="Transcript source for all sessions",
)
@click.option(
    "--mlx-whisper-url",
    type=str,
    envvar="MLX_WHISPER_URL",
    help="MLX Whisper sidecar URL",
)
@click.option(
    "--wtf-server-url",
    type=str,
    envvar="WTF_SERVER_URL",
    help="WTF Server URL",
)
@click.option(
    "--wtf-server-provider",
    type=click.Choice(
        ["nvidia", "openai", "deepgram", "groq", "local-whisper", "mlx-whisper"]
    ),
    envvar="WTF_SERVER_PROVIDER",
    help="WTF Server ASR provider",
)
@click.option(
    "--no-video",
    is_flag=True,
    help="Skip video references",
)
@click.option(
    "--parallel",
    type=int,
    default=1,
    help="Number of parallel conversions (default: 1)",
)
@click.option(
    "--groups",
    type=str,
    multiple=True,
    help="Only convert specific groups (can specify multiple times)",
)
@click.option(
    "--rsync-mirror",
    type=click.Path(path_type=Path),
    envvar="IETF_RSYNC_MIRROR",
    help="Local rsync mirror root (checked before HTTP for materials). "
         "Use 'ietf2vcon sync' to populate it.",
)
@click.option(
    "--existing-only",
    is_flag=True,
    help="Only regenerate sessions that already have a published file in the "
         "output directory; never create a new file. Groups the Datatracker lists "
         "but that have no file are skipped and listed in the summary.",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    help="Enable verbose output",
)
@MAX_RPS_OPTION
def convert_all(
    meeting: int,
    output_dir: Path,
    no_transcript: bool,
    transcript_source: str,
    mlx_whisper_url: str | None,
    wtf_server_url: str | None,
    wtf_server_provider: str | None,
    no_video: bool,
    parallel: int,
    groups: tuple[str, ...],
    rsync_mirror: Path | None,
    existing_only: bool,
    verbose: bool,
    max_rps: float | None,
):
    """Convert all sessions from an IETF meeting to vCon format.

    Examples:

        # Convert all of IETF 121
        ietf2vcon convert-all --meeting 121

        # Skip transcripts (faster)
        ietf2vcon convert-all --meeting 121 --no-transcript

        # Use MLX Whisper for transcription
        ietf2vcon convert-all --meeting 121 --transcript-source mlx-whisper \\
            --mlx-whisper-url http://localhost:8000

        # Use WTF Server
        ietf2vcon convert-all --meeting 121 --transcript-source wtf-server \\
            --wtf-server-url http://localhost:3000

        # Convert in parallel
        ietf2vcon convert-all --meeting 121 --parallel 4

        # Convert only specific groups
        ietf2vcon convert-all --meeting 121 --groups vcon --groups httpbis

        # Regenerate the files already in the output directory in place, and
        # create no new ones
        ietf2vcon convert-all --meeting 121 --existing-only --output-dir ./ietf121
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from rich.progress import Progress, SpinnerColumn, TextColumn

    from .datatracker import DataTrackerClient
    from .materials import MaterialsDownloader

    setup_logging(verbose)
    _apply_rate(max_rps)

    console.print(f"\n[bold]Converting IETF {meeting} to vCon[/bold]\n")

    problem = check_mirror(rsync_mirror, meeting)
    if problem:
        console.print(f"[red]Error:[/red] {escape(problem)}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    remove_stale_partials(output_dir)
    MaterialsDownloader.reset_counters()

    # The work list: the groups the Datatracker lists for the meeting together
    # with the groups that already have a published file. A group the
    # Datatracker no longer lists keeps its file untouched and is reported.
    on_disk = published_files(output_dir, meeting)
    listed: set[str] | None = None
    if groups:
        group_list = list(groups)
        console.print(f"Converting {len(group_list)} specified groups")
    else:
        console.print("Fetching session list...")
        client = DataTrackerClient()
        try:
            sessions = client.get_meeting_sessions(meeting)
        except SessionReadError as e:
            console.print(f"[red]Error:[/red] could not list the sessions of IETF {meeting}: {escape(str(e))}")
            sys.exit(1)
        finally:
            client.close()
        if not sessions:
            console.print(f"[red]Error:[/red] the Datatracker lists no sessions for IETF {meeting}")
            sys.exit(1)
        listed = {s.group_acronym for s in sessions}
        group_list = sorted(listed | set(on_disk))
        console.print(f"Found [cyan]{len(listed)}[/cyan] working groups\n")

    # Configure options
    options = ConversionOptions(
        include_video=not no_video,
        include_transcript=not no_transcript,
        transcription_source=transcript_source,
        mlx_whisper_url=mlx_whisper_url,
        wtf_server_url=wtf_server_url,
        wtf_server_provider=wtf_server_provider,
        include_chat=False,
        output_dir=output_dir,
        rsync_mirror_dir=rsync_mirror,
    )

    chair_ambiguous: list[str] = []
    missing_published: list[str] = []
    no_published_file: list[str] = []
    # A read every session depends on (meeting record, official schedule,
    # statuses) failed: stop trying, since each remaining session would fail
    # the same slow way.
    stop = threading.Event()
    stop_reason: list[str] = []
    not_attempted = [0]

    def convert_group(group: str) -> list[tuple[str, bool, str]]:
        """Convert a group's session(s).

        A group that already has files in the output directory is regenerated
        in place: one conversion per previous file, by the session id in its
        name, so filenames and uuids do not move. A group with none gets the
        session ``choose_session`` picks (or, with --existing-only, nothing).
        """
        outcomes = []
        converter = IETFSessionConverter(options)
        previous = converter.previous_session_ids(meeting, group)
        if listed is not None and group not in listed:
            # The Datatracker no longer lists the group: leave its files alone.
            for session_id in previous:
                label = f"{group}:{session_id}" if len(previous) > 1 else group
                logger.warning(
                    "Published session not found: %s has files but the Datatracker "
                    "no longer lists it at IETF %s; left untouched", group, meeting,
                )
                missing_published.append(label)
                outcomes.append((label, True, "group no longer listed; file untouched"))
            return outcomes
        if existing_only and not previous:
            no_published_file.append(group)
            return outcomes
        targets = previous or [None]
        for position, session_id in enumerate(targets):
            label = group if len(previous) <= 1 else f"{group}:{session_id}"
            if stop.is_set():
                not_attempted[0] += len(targets) - position
                break
            try:
                result = converter.convert_session(meeting, group, session_id=session_id)
                if result.meeting_wide_failure:
                    if not stop.is_set():
                        stop_reason.append(result.errors[0] if result.errors else "unknown")
                    stop.set()
                if result.chair_ambiguous:
                    chair_ambiguous.append(label)
                if result.published_session_missing:
                    missing_published.append(label)
                    outcomes.append((label, True, "published session not found; file untouched"))
                    continue
                if result.errors:
                    outcomes.append((label, False, result.errors[0]))
                    continue
                output_path = converter.save_vcon(result)
                outcomes.append((label, True, str(output_path)))
            except Exception as e:
                outcomes.append((label, False, str(e)))
        return outcomes

    # Convert sessions
    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Converting...", total=len(group_list))

        if parallel > 1:
            with ThreadPoolExecutor(max_workers=parallel) as executor:
                futures = {
                    executor.submit(convert_group, g): g for g in group_list
                }
                for future in as_completed(futures):
                    group = futures[future]
                    results.extend(future.result())
                    progress.update(task, advance=1, description=f"Converted {group}")
        else:
            for group in group_list:
                progress.update(task, description=f"Converting {group}...")
                results.extend(convert_group(group))
                progress.update(task, advance=1)

    # Display results
    console.print("\n")

    table = Table(title=f"IETF {meeting} Conversion Results")
    table.add_column("Group", style="cyan")
    table.add_column("Status")
    table.add_column("Output/Error")

    success_count = 0
    failures = []
    for group, success, message in sorted(results):
        if success:
            success_count += 1
            table.add_row(group, "[green]✓[/green]", escape(message[:60]))
        else:
            failures.append((group, message))
            table.add_row(group, "[red]✗[/red]", escape(message[:60]))

    console.print(table)
    console.print(f"\n[bold]Summary:[/bold] {success_count}/{len(results)} successful")
    if failures:
        console.print(f"[red]{len(failures)} session(s) failed; their published files are untouched:[/red]")
        for group, message in failures:
            console.print(f"  [red]{escape(group)}[/red]: {escape(message)}")
    if stop.is_set():
        console.print(
            f"[red]Run stopped at the first meeting-wide read failure:[/red] "
            f"{escape(stop_reason[0])}"
        )
        console.print(
            f"[red]{not_attempted[0]} session(s) not attempted[/red]; their files are untouched. "
            "Run the meeting again once the Datatracker answers."
        )
    if missing_published:
        console.print(
            f"[yellow]Published session not found for {len(missing_published)} "
            f"file(s), left untouched: {escape(', '.join(sorted(missing_published)))}[/yellow]"
        )
    if no_published_file:
        console.print(
            f"[yellow]No published file for {len(no_published_file)} group(s), skipped "
            f"(--existing-only): {escape(', '.join(sorted(no_published_file)))}[/yellow]"
        )
    if chair_ambiguous:
        console.print(
            f"[yellow]Chairs ambiguous, omitted for {len(chair_ambiguous)} "
            f"session(s): {escape(', '.join(sorted(chair_ambiguous)))}[/yellow]"
        )
    console.print(
        f"Materials: {MaterialsDownloader.mirror_served} from the mirror, "
        f"{MaterialsDownloader.http_fetched} over HTTP"
    )
    console.print(MaterialsDownloader.not_available_summary())
    logger.info(
        "Materials: %d from the mirror, %d over HTTP; %s",
        MaterialsDownloader.mirror_served, MaterialsDownloader.http_fetched,
        MaterialsDownloader.not_available_summary(),
    )

    if existing_only and not results:
        console.print(
            "[red]Error:[/red] --existing-only found no published file to regenerate; "
            f"is {escape(str(output_dir))} the right output directory?"
        )
        sys.exit(1)
    if failures:
        sys.exit(1)


@main.command()
@click.argument("vcon_file", type=click.Path(exists=True, path_type=Path))
def info(vcon_file: Path):
    """Display information about a vCon file."""
    import json

    with open(vcon_file) as f:
        data = json.load(f)

    table = Table(title=f"vCon: {vcon_file.name}")

    table.add_column("Field", style="cyan")
    table.add_column("Value")

    table.add_row("UUID", str(data.get("uuid", "-")))
    table.add_row("Subject", data.get("subject", "-"))
    table.add_row("Created", data.get("created_at", "-"))
    table.add_row("Parties", str(len(data.get("parties", []))))
    table.add_row("Dialogs", str(len(data.get("dialog", []))))
    table.add_row("Attachments", str(len(data.get("attachments", []))))
    table.add_row("Analysis", str(len(data.get("analysis", []))))

    console.print(table)

    # Show dialog types
    dialogs = data.get("dialog", [])
    if dialogs:
        console.print("\n[bold]Dialogs:[/bold]")
        for i, d in enumerate(dialogs):
            dtype = d.get("type", "unknown")
            url = d.get("url", "inline")[:50] if d.get("url") else "inline"
            console.print(f"  {i}: {dtype} - {url}")

    # Show attachment types
    attachments = data.get("attachments", [])
    if attachments:
        console.print("\n[bold]Attachments:[/bold]")
        for i, a in enumerate(attachments):
            atype = a.get("type", "unknown")
            console.print(f"  {i}: {atype}")


@main.command()
@click.option(
    "-m", "--meeting",
    type=int,
    required=True,
    help="IETF meeting number to sync (e.g., 125)",
)
@click.option(
    "--mirror-dir",
    type=click.Path(path_type=Path),
    envvar="IETF_RSYNC_MIRROR",
    default=Path("./downloads"),
    help="Local mirror root directory (default: ./downloads)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be synced without transferring files",
)
def sync(meeting: int, mirror_dir: Path, dry_run: bool):
    """Sync IETF meeting proceedings from rsync.ietf.org.

    Downloads slides, agendas, minutes, chatlogs, and bluesheets to a local
    mirror. Subsequent 'convert' runs with --rsync-mirror will use this cache
    instead of fetching files one-by-one over HTTP.

    Examples:

        # Sync IETF 125 proceedings locally
        ietf2vcon sync --meeting 125

        # Sync to a custom directory
        ietf2vcon sync --meeting 125 --mirror-dir /mnt/T9/ietf

        # Preview without downloading
        ietf2vcon sync --meeting 125 --dry-run
    """
    setup_logging(False)

    dest = mirror_dir / "proceedings" / str(meeting)
    console.print(f"Syncing IETF {meeting} proceedings → {dest}")
    if dry_run:
        console.print("[yellow]Dry run — no files will be written[/yellow]")

    ok = sync_proceedings(meeting, mirror_dir, dry_run=dry_run)
    if ok:
        console.print(f"[green]✓[/green] Sync complete: {dest}")
    else:
        console.print("[red]✗[/red] Sync failed — check rsync is installed and network is available")
        raise SystemExit(1)


def _display_results(result, output_path: Path):
    """Display conversion results."""
    table = Table(title="Conversion Results")
    table.add_column("Component", style="cyan")
    table.add_column("Status")
    table.add_column("Details")

    # Video
    if result.video_url:
        table.add_row("Video", "[green]✓[/green]", result.video_url[:50] + "...")
    else:
        table.add_row("Video", "[yellow]–[/yellow]", "Not included")

    # Materials
    if result.materials_count > 0:
        table.add_row("Materials", "[green]✓[/green]", f"{result.materials_count} items")
    else:
        table.add_row("Materials", "[yellow]–[/yellow]", "None found")

    # Transcript
    if result.has_transcript:
        table.add_row("Transcript", "[green]✓[/green]", "Included")
    else:
        table.add_row("Transcript", "[yellow]–[/yellow]", "Not included")

    # Chat
    if result.chat_message_count > 0:
        table.add_row("Chat", "[green]✓[/green]", f"{result.chat_message_count} messages")
    else:
        table.add_row("Chat", "[yellow]–[/yellow]", "Not included")

    console.print(table)

    # Warnings
    if result.warnings:
        console.print("\n[yellow]Warnings:[/yellow]")
        for w in result.warnings:
            console.print(f"  • {w}")

    # Errors
    if result.errors:
        console.print("\n[red]Errors:[/red]")
        for e in result.errors:
            console.print(f"  • {e}")

    # Output
    console.print(f"\n[bold]Output:[/bold] {output_path}")


if __name__ == "__main__":
    main()
