#!/usr/bin/env python3
"""Convert multiple IETF meetings to vCon format.

Usage:
    python scripts/convert_multi_meetings.py 110 124
    python scripts/convert_multi_meetings.py 110 124 --parallel 4
    python scripts/convert_multi_meetings.py 110 124 --no-transcript
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()


def convert_meeting(
    meeting_number: int,
    output_dir: Path,
    parallel: int,
    include_transcript: bool,
) -> tuple[int, int, int, list[str]]:
    """Convert a single meeting. Returns (success, failed, skipped, errors)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ietf2vcon.converter import ConversionOptions, IETFSessionConverter
    from ietf2vcon.datatracker import DataTrackerClient

    # Get all groups for this meeting
    client = DataTrackerClient()
    try:
        sessions = client.get_meeting_sessions(meeting_number)
        if not sessions:
            return 0, 0, 0, [f"No sessions found for IETF {meeting_number}"]
        groups = sorted(set(s.group_acronym for s in sessions))
    except Exception as e:
        return 0, 0, 0, [f"Failed to fetch sessions: {e}"]
    finally:
        client.close()

    # Configure options
    meeting_output_dir = output_dir / f"ietf{meeting_number}"
    meeting_output_dir.mkdir(parents=True, exist_ok=True)

    options = ConversionOptions(
        include_video=True,
        include_transcript=include_transcript,
        include_chat=False,
        output_dir=meeting_output_dir,
    )

    def convert_group(group: str) -> tuple[str, bool, str]:
        """Convert a single group's session."""
        try:
            converter = IETFSessionConverter(options)
            result = converter.convert_session(meeting_number, group)
            if result.errors:
                return (group, False, result.errors[0])
            output_path = converter.save_vcon(result)
            return (group, True, str(output_path))
        except Exception as e:
            return (group, False, str(e))

    # Convert all groups
    results = []
    success = 0
    failed = 0
    errors = []

    if parallel > 1:
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {executor.submit(convert_group, g): g for g in groups}
            for future in as_completed(futures):
                group = futures[future]
                result = future.result()
                results.append(result)
                if result[1]:
                    success += 1
                else:
                    failed += 1
                    errors.append(f"{group}: {result[2]}")
    else:
        for group in groups:
            result = convert_group(group)
            results.append(result)
            if result[1]:
                success += 1
            else:
                failed += 1
                errors.append(f"{group}: {result[2]}")

    return success, failed, 0, errors


def main():
    parser = argparse.ArgumentParser(
        description="Convert multiple IETF meetings to vCon format"
    )
    parser.add_argument(
        "start_meeting",
        type=int,
        help="First IETF meeting number",
    )
    parser.add_argument(
        "end_meeting",
        type=int,
        help="Last IETF meeting number",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./output"),
        help="Base output directory (default: ./output)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=4,
        help="Parallel conversions per meeting (default: 4)",
    )
    parser.add_argument(
        "--no-transcript",
        action="store_true",
        help="Skip transcript generation",
    )
    parser.add_argument(
        "--resume-from",
        type=int,
        help="Resume from this meeting number (skip earlier ones)",
    )
    args = parser.parse_args()

    meetings = list(range(args.start_meeting, args.end_meeting + 1))
    if args.resume_from:
        meetings = [m for m in meetings if m >= args.resume_from]

    console.print(f"\n[bold]Converting IETF meetings {args.start_meeting}-{args.end_meeting}[/bold]")
    console.print(f"Meetings to process: {len(meetings)}")
    console.print(f"Parallel workers per meeting: {args.parallel}")
    console.print(f"Transcripts: {'No' if args.no_transcript else 'Yes'}")
    console.print(f"Output directory: {args.output_dir}\n")

    # Process each meeting
    all_results = []
    total_success = 0
    total_failed = 0
    start_time = time.time()

    for i, meeting in enumerate(meetings):
        console.print(f"\n[cyan]{'='*60}[/cyan]")
        console.print(
            f"[bold]IETF {meeting}[/bold] ({i+1}/{len(meetings)}) - "
            f"Started at {datetime.now().strftime('%H:%M:%S')}"
        )
        console.print(f"[cyan]{'='*60}[/cyan]")

        meeting_start = time.time()
        success, failed, skipped, errors = convert_meeting(
            meeting,
            args.output_dir,
            args.parallel,
            not args.no_transcript,
        )
        meeting_time = time.time() - meeting_start

        total_success += success
        total_failed += failed

        all_results.append({
            "meeting": meeting,
            "success": success,
            "failed": failed,
            "time": meeting_time,
            "errors": errors,
        })

        console.print(f"\n[bold]IETF {meeting} complete:[/bold]")
        console.print(f"  Success: [green]{success}[/green]")
        console.print(f"  Failed: [red]{failed}[/red]")
        console.print(f"  Time: {meeting_time:.1f}s")

        if errors and len(errors) <= 5:
            for e in errors:
                console.print(f"  [red]Error:[/red] {e[:80]}")
        elif errors:
            console.print(f"  [red]{len(errors)} errors (showing first 5)[/red]")
            for e in errors[:5]:
                console.print(f"    {e[:80]}")

    # Final summary
    total_time = time.time() - start_time

    console.print(f"\n\n[bold]{'='*60}[/bold]")
    console.print("[bold]FINAL SUMMARY[/bold]")
    console.print(f"[bold]{'='*60}[/bold]\n")

    table = Table(title="Meeting Results")
    table.add_column("Meeting", style="cyan")
    table.add_column("Success", style="green")
    table.add_column("Failed", style="red")
    table.add_column("Time")

    for r in all_results:
        table.add_row(
            f"IETF {r['meeting']}",
            str(r["success"]),
            str(r["failed"]),
            f"{r['time']:.0f}s",
        )

    console.print(table)

    console.print(f"\n[bold]Totals:[/bold]")
    console.print(f"  Meetings processed: {len(meetings)}")
    console.print(f"  Total sessions: {total_success + total_failed}")
    console.print(f"  Successful: [green]{total_success}[/green]")
    console.print(f"  Failed: [red]{total_failed}[/red]")
    console.print(f"  Total time: {total_time/60:.1f} minutes")

    # Check output sizes
    console.print(f"\n[bold]Output directory sizes:[/bold]")
    for r in all_results:
        meeting_dir = args.output_dir / f"ietf{r['meeting']}"
        if meeting_dir.exists():
            files = list(meeting_dir.glob("*.vcon.json"))
            total_size = sum(f.stat().st_size for f in files)
            console.print(f"  IETF {r['meeting']}: {len(files)} files, {total_size/1024/1024:.1f} MB")

    if total_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
