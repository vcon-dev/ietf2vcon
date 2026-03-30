"""IETF rsync mirror support.

The IETF publishes meeting proceedings on rsync.ietf.org::proceedings/.
This module syncs them locally and provides lookup so materials.py can
serve files from disk instead of hitting the Datatracker HTTP API.

Mirror layout (mirrors rsync.ietf.org::proceedings/{meeting}/):
    downloads/proceedings/{meeting}/slides/slides-{meeting}-{group}-*.pdf
    downloads/proceedings/{meeting}/agenda/agenda-{meeting}-{group}-*.*
    downloads/proceedings/{meeting}/minutes/minutes-{meeting}-{group}-*.*
    downloads/proceedings/{meeting}/chatlog/...
    downloads/proceedings/{meeting}/bluesheets/...
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

IETF_RSYNC = "rsync.ietf.org::proceedings"

# Subdirs that contain per-session materials (in priority order for lookup)
MATERIAL_SUBDIRS = ["slides", "agenda", "minutes", "chatlog", "bluesheets", "procmaterials"]


def sync_proceedings(meeting_number: int, local_dir: Path, dry_run: bool = False) -> bool:
    """Rsync IETF meeting proceedings to a local directory.

    Args:
        meeting_number: IETF meeting number (e.g., 125)
        local_dir: Root directory for the local mirror
        dry_run: If True, pass --dry-run to rsync (no files written)

    Returns:
        True if rsync succeeded, False otherwise
    """
    source = f"{IETF_RSYNC}/{meeting_number}/"
    dest = local_dir / "proceedings" / str(meeting_number)
    dest.mkdir(parents=True, exist_ok=True)

    cmd = ["rsync", "-av", "--delete"]
    if dry_run:
        cmd.append("--dry-run")
    cmd += [source, str(dest) + "/"]

    logger.info("Syncing IETF %d proceedings: %s → %s", meeting_number, source, dest)

    try:
        result = subprocess.run(cmd, capture_output=False, text=True)
        if result.returncode == 0:
            logger.info("Sync complete for IETF %d", meeting_number)
            return True
        else:
            logger.error("rsync failed (exit %d) for IETF %d", result.returncode, meeting_number)
            return False
    except FileNotFoundError:
        logger.error("rsync not found. Install rsync and try again.")
        return False
    except Exception as e:
        logger.error("rsync error: %s", e)
        return False


def find_local_file(doc_name: str, meeting_number: int, local_dir: Path) -> Path | None:
    """Find a document in the local proceedings mirror.

    The rsync server stores files as:
        proceedings/{meeting}/{type}/{doc_name}.{ext}

    The Datatracker URL basename (doc_name) matches the rsync filename
    without extension, e.g. ``slides-125-6lo-chairs-introduction-00``.

    Args:
        doc_name: Document name from Datatracker URL (no extension)
        meeting_number: IETF meeting number
        local_dir: Root directory of the local mirror

    Returns:
        Path to the local file if found, None otherwise
    """
    meeting_dir = local_dir / "proceedings" / str(meeting_number)
    if not meeting_dir.exists():
        return None

    # Determine which subdir to check based on the doc_name prefix
    if doc_name.startswith("slides-"):
        search_dirs = ["slides"]
    elif doc_name.startswith("agenda-"):
        search_dirs = ["agenda"]
    elif doc_name.startswith("minutes-"):
        search_dirs = ["minutes"]
    elif doc_name.startswith("chatlog-"):
        search_dirs = ["chatlog"]
    elif doc_name.startswith("bluesheets-"):
        search_dirs = ["bluesheets"]
    else:
        search_dirs = MATERIAL_SUBDIRS

    for subdir in search_dirs:
        subdir_path = meeting_dir / subdir
        if not subdir_path.exists():
            continue
        # Try exact match with common extensions
        for ext in [".pdf", ".txt", ".md", ".html", ".htm", ".pptx", ".docx"]:
            candidate = subdir_path / f"{doc_name}{ext}"
            if candidate.exists():
                logger.debug("Mirror hit: %s", candidate)
                return candidate
        # Try glob (handles cases where revision suffix differs)
        matches = list(subdir_path.glob(f"{doc_name}.*"))
        if matches:
            logger.debug("Mirror hit (glob): %s", matches[0])
            return matches[0]

    return None


def mirror_available(meeting_number: int, local_dir: Path) -> bool:
    """Return True if a local mirror exists for this meeting."""
    return (local_dir / "proceedings" / str(meeting_number)).exists()
