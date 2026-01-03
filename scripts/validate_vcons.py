#!/usr/bin/env python3
"""Validate randomly sampled vCon files for errors.

Usage:
    python scripts/validate_vcons.py output/
    python scripts/validate_vcons.py output/ --sample 10
    python scripts/validate_vcons.py output/ --all
    python scripts/validate_vcons.py output/ --verbose
"""

import argparse
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from rich.console import Console
from rich.table import Table

console = Console()


class VConValidator:
    """Validates vCon files for structural and content errors."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def reset(self):
        """Reset errors and warnings for a new validation."""
        self.errors = []
        self.warnings = []

    def error(self, msg: str):
        """Record an error."""
        self.errors.append(msg)

    def warn(self, msg: str):
        """Record a warning."""
        self.warnings.append(msg)

    def validate_file(self, path: Path) -> tuple[bool, list[str], list[str]]:
        """Validate a single vCon file. Returns (success, errors, warnings)."""
        self.reset()

        # Check file exists and is readable
        if not path.exists():
            self.error(f"File does not exist: {path}")
            return False, self.errors, self.warnings

        # Parse JSON
        try:
            with open(path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            self.error(f"Invalid JSON: {e}")
            return False, self.errors, self.warnings

        # Validate structure
        self._validate_root(data)
        self._validate_parties(data.get("parties", []))
        self._validate_dialogs(data.get("dialog", []))
        self._validate_attachments(data.get("attachments", []))
        self._validate_analysis(data.get("analysis", []))

        success = len(self.errors) == 0
        return success, self.errors.copy(), self.warnings.copy()

    def _validate_root(self, data: dict[str, Any]):
        """Validate root-level vCon structure."""
        # Required fields
        required = ["vcon", "uuid", "created_at"]
        for field in required:
            if field not in data:
                self.error(f"Missing required field: {field}")

        # vCon version
        if "vcon" in data:
            if data["vcon"] != "0.0.1":
                self.warn(f"Unexpected vCon version: {data['vcon']}")

        # UUID format
        if "uuid" in data:
            uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
            if not re.match(uuid_pattern, str(data["uuid"]), re.IGNORECASE):
                self.error(f"Invalid UUID format: {data['uuid']}")

        # created_at format (ISO 8601)
        if "created_at" in data:
            try:
                datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                self.error(f"Invalid created_at format: {data['created_at']}")

        # Subject should exist for IETF vCons
        if "subject" not in data or not data["subject"]:
            self.warn("Missing or empty subject")
        elif "IETF" not in data["subject"]:
            self.warn(f"Subject doesn't mention IETF: {data['subject']}")

    def _validate_parties(self, parties: list[dict]):
        """Validate parties array."""
        if not parties:
            self.warn("No parties in vCon")
            return

        for i, party in enumerate(parties):
            if not isinstance(party, dict):
                self.error(f"Party {i}: not a dict")
                continue

            # Should have at least name or tel or mailto
            has_identity = any(
                party.get(f) for f in ["name", "tel", "mailto", "uri"]
            )
            if not has_identity:
                self.warn(f"Party {i}: no identifying information")

            # Validate email format if present
            if "mailto" in party and party["mailto"]:
                email = party["mailto"]
                if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
                    self.error(f"Party {i}: invalid email format: {email}")

            # Role should be valid if present
            if "role" in party:
                valid_roles = ["chair", "presenter", "participant", "speaker", "host", "attendee"]
                if party["role"] not in valid_roles:
                    self.warn(f"Party {i}: unusual role: {party['role']}")

    def _validate_dialogs(self, dialogs: list[dict]):
        """Validate dialog array."""
        if not dialogs:
            self.warn("No dialogs in vCon")
            return

        for i, dialog in enumerate(dialogs):
            if not isinstance(dialog, dict):
                self.error(f"Dialog {i}: not a dict")
                continue

            # Must have type
            if "type" not in dialog:
                self.error(f"Dialog {i}: missing type")
            else:
                valid_types = ["recording", "video", "audio", "text", "transfer"]
                if dialog["type"] not in valid_types:
                    self.warn(f"Dialog {i}: unusual type: {dialog['type']}")

            # Must have either url or body
            has_content = dialog.get("url") or dialog.get("body")
            if not has_content:
                self.error(f"Dialog {i}: missing both url and body")

            # Validate URL if present
            if "url" in dialog and dialog["url"]:
                self._validate_url(dialog["url"], f"Dialog {i}")

            # Validate mimetype if present
            if "mimetype" in dialog and dialog["mimetype"]:
                if "/" not in dialog["mimetype"]:
                    self.error(f"Dialog {i}: invalid mimetype: {dialog['mimetype']}")

            # Start time format
            if "start" in dialog:
                try:
                    datetime.fromisoformat(dialog["start"].replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    self.error(f"Dialog {i}: invalid start time: {dialog['start']}")

    def _validate_attachments(self, attachments: list[dict]):
        """Validate attachments array."""
        if not attachments:
            self.warn("No attachments in vCon")
            return

        has_lawful_basis = False
        has_ingress_info = False

        for i, att in enumerate(attachments):
            if not isinstance(att, dict):
                self.error(f"Attachment {i}: not a dict")
                continue

            # Must have type
            if "type" not in att:
                self.error(f"Attachment {i}: missing type")
            else:
                if att["type"] == "lawful_basis":
                    has_lawful_basis = True
                    self._validate_lawful_basis(att, i)
                elif att["type"] == "ingress_info":
                    has_ingress_info = True

            # Must have either url or body
            has_content = att.get("url") or att.get("body") is not None
            if not has_content:
                self.error(f"Attachment {i}: missing both url and body")

            # Validate URL if present
            if "url" in att and att["url"]:
                self._validate_url(att["url"], f"Attachment {i}")

        # IETF vCons should have lawful basis (Note Well)
        if not has_lawful_basis:
            self.warn("No lawful_basis attachment (IETF Note Well)")

        if not has_ingress_info:
            self.warn("No ingress_info attachment")

    def _validate_lawful_basis(self, att: dict, index: int):
        """Validate lawful_basis attachment."""
        body = att.get("body")
        if not body:
            self.error(f"Attachment {index} (lawful_basis): missing body")
            return

        if not isinstance(body, dict):
            self.error(f"Attachment {index} (lawful_basis): body is not a dict")
            return

        # Should have lawful_basis field
        if "lawful_basis" not in body:
            self.error(f"Attachment {index} (lawful_basis): missing lawful_basis field")

        # Should reference Note Well
        if "terms_of_service_name" in body:
            if "Note Well" not in body["terms_of_service_name"]:
                self.warn(
                    f"Attachment {index}: terms_of_service_name doesn't mention Note Well"
                )

    def _validate_analysis(self, analysis: list[dict]):
        """Validate analysis array."""
        if not analysis:
            # Not an error, just informational
            if self.verbose:
                self.warn("No analysis (transcript) in vCon")
            return

        for i, item in enumerate(analysis):
            if not isinstance(item, dict):
                self.error(f"Analysis {i}: not a dict")
                continue

            # Must have type
            if "type" not in item:
                self.error(f"Analysis {i}: missing type")
            else:
                if item["type"] == "wtf_transcription":
                    self._validate_wtf_transcription(item, i)

            # Must have body
            if "body" not in item:
                self.error(f"Analysis {i}: missing body")

    def _validate_wtf_transcription(self, item: dict, index: int):
        """Validate WTF transcription format."""
        # Should have spec field
        if "spec" not in item:
            self.warn(f"Analysis {index} (wtf): missing spec field")
        elif "draft-howe-wtf" not in str(item.get("spec", "")):
            self.warn(f"Analysis {index} (wtf): unexpected spec: {item['spec']}")

        body = item.get("body")
        if not body:
            return

        if not isinstance(body, dict):
            self.error(f"Analysis {index} (wtf): body is not a dict")
            return

        # WTF required fields
        wtf_required = ["segments"]
        for field in wtf_required:
            if field not in body:
                self.error(f"Analysis {index} (wtf): missing {field}")

        # Validate segments
        segments = body.get("segments", [])
        if not segments:
            self.warn(f"Analysis {index} (wtf): no segments in transcript")
        else:
            # Sample validation of first few segments
            for j, seg in enumerate(segments[:5]):
                if not isinstance(seg, dict):
                    self.error(f"Analysis {index} (wtf): segment {j} is not a dict")
                    continue

                # Each segment should have start, end, text
                for field in ["start", "end", "text"]:
                    if field not in seg:
                        self.error(
                            f"Analysis {index} (wtf): segment {j} missing {field}"
                        )

                # Start should be < end
                if "start" in seg and "end" in seg:
                    if seg["start"] > seg["end"]:
                        self.error(
                            f"Analysis {index} (wtf): segment {j} start > end"
                        )

        # Check metadata
        if "metadata" in body:
            meta = body["metadata"]
            if not isinstance(meta, dict):
                self.warn(f"Analysis {index} (wtf): metadata is not a dict")

    def _validate_url(self, url: str, context: str):
        """Validate a URL."""
        try:
            parsed = urlparse(url)
            if not parsed.scheme:
                self.error(f"{context}: URL missing scheme: {url}")
            elif parsed.scheme not in ["http", "https", "mailto", "tel"]:
                self.warn(f"{context}: unusual URL scheme: {parsed.scheme}")
            if not parsed.netloc and parsed.scheme in ["http", "https"]:
                self.error(f"{context}: URL missing host: {url}")
        except Exception as e:
            self.error(f"{context}: invalid URL: {url} ({e})")


def main():
    parser = argparse.ArgumentParser(
        description="Validate randomly sampled vCon files for errors"
    )
    parser.add_argument(
        "directory",
        type=Path,
        help="Directory containing vCon files",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=10,
        help="Number of files to randomly sample (default: 10)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Validate all files instead of sampling",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output including warnings",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default="*.vcon.json",
        help="Glob pattern for vCon files (default: *.vcon.json)",
    )
    args = parser.parse_args()

    # Find vCon files
    vcon_files = list(args.directory.glob(args.pattern))

    if not vcon_files:
        console.print(f"[red]No vCon files found in {args.directory}[/red]")
        sys.exit(1)

    console.print(f"Found [cyan]{len(vcon_files)}[/cyan] vCon files\n")

    # Select files to validate
    if args.all:
        selected = vcon_files
    else:
        sample_size = min(args.sample, len(vcon_files))
        selected = random.sample(vcon_files, sample_size)

    console.print(f"Validating [cyan]{len(selected)}[/cyan] files...\n")

    # Validate each file
    validator = VConValidator(verbose=args.verbose)
    results = []

    for path in sorted(selected):
        success, errors, warnings = validator.validate_file(path)
        results.append((path.name, success, errors, warnings))

    # Display results
    table = Table(title="Validation Results")
    table.add_column("File", style="cyan", max_width=40)
    table.add_column("Status")
    table.add_column("Errors", style="red")
    table.add_column("Warnings", style="yellow")

    total_errors = 0
    total_warnings = 0
    failed_files = 0

    for name, success, errors, warnings in results:
        status = "[green]✓[/green]" if success else "[red]✗[/red]"
        error_count = str(len(errors)) if errors else "-"
        warning_count = str(len(warnings)) if warnings else "-"

        table.add_row(name, status, error_count, warning_count)

        total_errors += len(errors)
        total_warnings += len(warnings)
        if not success:
            failed_files += 1

    console.print(table)

    # Summary
    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  Files validated: {len(results)}")
    console.print(f"  Passed: [green]{len(results) - failed_files}[/green]")
    console.print(f"  Failed: [red]{failed_files}[/red]")
    console.print(f"  Total errors: [red]{total_errors}[/red]")
    console.print(f"  Total warnings: [yellow]{total_warnings}[/yellow]")

    # Show detailed errors if verbose or if there are failures
    if args.verbose or failed_files > 0:
        console.print("\n[bold]Details:[/bold]")
        for name, success, errors, warnings in results:
            if errors or (args.verbose and warnings):
                console.print(f"\n[cyan]{name}[/cyan]")
                for e in errors:
                    console.print(f"  [red]ERROR:[/red] {e}")
                if args.verbose:
                    for w in warnings:
                        console.print(f"  [yellow]WARN:[/yellow] {w}")

    # Exit with error if any files failed
    if failed_files > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
