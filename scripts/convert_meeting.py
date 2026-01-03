#!/usr/bin/env python3
"""Batch convert all sessions from an IETF meeting to vCon format.

Usage:
    python scripts/convert_meeting.py 121
    python scripts/convert_meeting.py 121 --no-transcript
    python scripts/convert_meeting.py 121 --parallel 4
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ietf2vcon.converter import ConversionOptions, IETFSessionConverter
from ietf2vcon.datatracker import DataTrackerClient

console = Console()


def get_all_groups(meeting_number: int) -> list[str]:
    """Get all unique working group acronyms for a meeting."""
    client = DataTrackerClient()
    try:
        sessions = client.get_meeting_sessions(meeting_number)
        groups = sorted(set(s.group_acronym for s in sessions))
        return groups
    finally:
        client.close()


def convert_group(
    meeting_number: int,
    group: str,
    options: ConversionOptions,
) -> tuple[str, bool, str]:
    """Convert a single group's session. Returns (group, success, message)."""
    try:
        converter = IETFSessionConverter(options)
        result = converter.convert_session(meeting_number, group)

        if result.errors:
            return (group, False, result.errors[0])

        output_path = converter.save_vcon(result)
        return (group, True, str(output_path))
    except Exception as e:
        return (group, False, str(e))


def main():
    parser = argparse.ArgumentParser(
        description="Convert all sessions from an IETF meeting to vCon format"
    )
    parser.add_argument("meeting", type=int, help="IETF meeting number")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./output"),
        help="Output directory",
    )
    parser.add_argument(
        "--no-transcript",
        action="store_true",
        help="Skip transcript generation",
    )
    parser.add_argument(
        "--no-video",
        action="store_true",
        help="Skip video",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Number of parallel conversions (default: 1)",
    )
    parser.add_argument(
        "--groups",
        type=str,
        nargs="+",
        help="Only convert these groups (space-separated)",
    )
    args = parser.parse_args()

    console.print(f"\n[bold]Converting IETF {args.meeting} to vCon[/bold]\n")

    # Get groups to convert
    if args.groups:
        groups = args.groups
        console.print(f"Converting {len(groups)} specified groups")
    else:
        console.print("Fetching session list...")
        groups = get_all_groups(args.meeting)
        console.print(f"Found [cyan]{len(groups)}[/cyan] working groups\n")

    # Configure options
    options = ConversionOptions(
        include_video=not args.no_video,
        include_transcript=not args.no_transcript,
        include_chat=False,  # Skip chat by default for batch
        output_dir=args.output_dir,
    )

    # Convert sessions
    results = []

    if args.parallel > 1:
        # Parallel conversion
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Converting...", total=len(groups))

            with ThreadPoolExecutor(max_workers=args.parallel) as executor:
                futures = {
                    executor.submit(convert_group, args.meeting, g, options): g
                    for g in groups
                }

                for future in as_completed(futures):
                    group = futures[future]
                    result = future.result()
                    results.append(result)
                    progress.update(task, advance=1, description=f"Converted {group}")
    else:
        # Sequential conversion
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Converting...", total=len(groups))

            for group in groups:
                progress.update(task, description=f"Converting {group}...")
                result = convert_group(args.meeting, group, options)
                results.append(result)
                progress.update(task, advance=1)

    # Display results
    console.print("\n")

    table = Table(title=f"IETF {args.meeting} Conversion Results")
    table.add_column("Group", style="cyan")
    table.add_column("Status")
    table.add_column("Output/Error")

    success_count = 0
    for group, success, message in sorted(results):
        if success:
            success_count += 1
            table.add_row(group, "[green]✓[/green]", message[:60])
        else:
            table.add_row(group, "[red]✗[/red]", message[:60])

    console.print(table)
    console.print(f"\n[bold]Summary:[/bold] {success_count}/{len(results)} successful")

    if success_count < len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
