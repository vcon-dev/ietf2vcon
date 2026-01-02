"""Command-line interface for ietf2vcon.

Convert IETF meeting sessions to vCon format.
"""

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from .converter import ConversionOptions, IETFSessionConverter

console = Console()


def setup_logging(verbose: bool):
    """Configure logging with rich output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Convert IETF meeting sessions to vCon format.

    This tool fetches IETF meeting recordings, materials, transcripts,
    and chat logs, combining them into a vCon (Virtual Conversation Container).

    By default, YouTube captions are fetched for the transcript.

    Examples:

        # Convert IETF 121 vcon working group session (includes transcript)
        ietf2vcon convert --meeting 121 --group vcon

        # Skip transcript
        ietf2vcon convert --meeting 121 --group vcon --no-transcript

        # Use Whisper for higher quality transcription
        ietf2vcon convert --meeting 121 --group vcon \\
            --transcript-source whisper --download-video

        # Include Zulip chat logs
        ietf2vcon convert --meeting 121 --group vcon \\
            --zulip-email user@example.com --zulip-api-key YOUR_KEY
    """
    pass


@main.command()
@click.option(
    "-m", "--meeting",
    type=int,
    required=True,
    help="IETF meeting number (e.g., 124)",
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
    type=click.Choice(["auto", "youtube", "whisper"]),
    default="auto",
    help="Transcript source: auto (YouTube captions first), youtube, or whisper",
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
    "-v", "--verbose",
    is_flag=True,
    help="Enable verbose output",
)
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
    no_chat: bool,
    zulip_email: str | None,
    zulip_api_key: str | None,
    verbose: bool,
):
    """Convert an IETF session to vCon format.

    By default, fetches YouTube captions for the transcript in WTF format.
    Use --no-transcript to skip, or --transcript-source=whisper for local transcription.
    Use --export-srt or --export-webvtt to generate subtitle files.
    """
    setup_logging(verbose)

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
        include_chat=not no_chat,
        zulip_email=zulip_email,
        zulip_api_key=zulip_api_key,
        output_dir=output_dir,
    )

    # Run conversion
    converter = IETFSessionConverter(options)

    try:
        with console.status("Converting session..."):
            result = converter.convert_session(meeting, group, session)

        # Save output
        output_path = converter.save_vcon(result, output)

        # Display results
        _display_results(result, output_path)

        if result.errors:
            sys.exit(1)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        if verbose:
            console.print_exception()
        sys.exit(1)


@main.command()
@click.option(
    "-m", "--meeting",
    type=int,
    required=True,
    help="IETF meeting number",
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
        if group:
            sessions = client.get_group_sessions(meeting, group)
        else:
            sessions = client.get_meeting_sessions(meeting)

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
    type=int,
    required=True,
    help="IETF meeting number",
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
