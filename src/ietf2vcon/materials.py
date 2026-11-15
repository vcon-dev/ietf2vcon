"""IETF meeting materials downloader.

Downloads slides, agendas, minutes, and other materials from the IETF Datatracker.
"""

import hashlib
import logging
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .errors import MaterialFetchError
from .hashing import content_hash_token
from .httpclient import get_with_retries
from .models import IETFMaterial
from .rsync_mirror import find_local_file

logger = logging.getLogger(__name__)

# A response that says the document is not there. Anything else that goes
# wrong (a timeout, a refused connection, 429, 5xx after the retries) is a
# failure to find out, not an answer, and fails the session.
NOT_AVAILABLE_STATUSES = {403, 404, 410}


class MaterialsDownloader:
    """Download and manage IETF meeting materials."""

    # Where materials came from in this process, for the end-of-run summary.
    mirror_served = 0
    http_fetched = 0
    # HTTP status -> how many materials the server said are not available. A
    # 403 can mean the client is being blocked, so these are counted, not just
    # logged.
    not_available: dict[int, int] = {}

    @classmethod
    def reset_counters(cls) -> None:
        cls.mirror_served = 0
        cls.http_fetched = 0
        cls.not_available = {}

    @classmethod
    def not_available_summary(cls) -> str:
        """"Materials not available: 59 (404: 59)", for the end-of-run summary."""
        total = sum(cls.not_available.values())
        if not total:
            return "Materials not available: 0"
        detail = ", ".join(f"{code}: {n}" for code, n in sorted(cls.not_available.items()))
        return f"Materials not available: {total} ({detail})"

    def __init__(
        self,
        download_dir: Path | None = None,
        timeout: float = 60.0,
        mirror_dir: Path | None = None,
    ):
        # Created when a file is first written, not here: fetch_material() does
        # not write to it, and an empty directory per run is clutter.
        self.download_dir = download_dir or Path("./materials")
        self.mirror_dir = mirror_dir
        self.client = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _mirror_path(self, material: IETFMaterial) -> Path | None:
        """This material in the local rsync mirror, if one is configured."""
        if not self.mirror_dir or not material.url:
            return None
        parts = material.url.split("/")
        try:
            meeting_number = int(parts[parts.index("meeting") + 1])
        except (ValueError, IndexError):
            return None

        # The document record names the published file, so go straight to it
        # rather than guessing which sibling the extension search lands on.
        if material.uploaded_filename:
            exact = (
                self.mirror_dir
                / "proceedings"
                / str(meeting_number)
                / material.type
                / material.uploaded_filename
            )
            if exact.is_file():
                return exact

        doc_name = material.url.rstrip("/").split("/")[-1].split("?")[0]
        return find_local_file(doc_name, meeting_number, self.mirror_dir)

    def download_material(self, material: IETFMaterial) -> Path | None:
        """Download a single material file.

        Checks the local rsync mirror first if one is configured, falling back
        to HTTP from the Datatracker.

        Args:
            material: The material to download

        Returns:
            Path to the downloaded file, or None if failed
        """
        # Check local rsync mirror first
        local_path = self._mirror_path(material)
        if local_path:
            logger.info("Using mirror: %s", local_path)
            # Copy to download_dir so callers get a consistent location
            self.download_dir.mkdir(parents=True, exist_ok=True)
            dest = self.download_dir / local_path.name
            if not dest.exists():
                import shutil
                shutil.copy2(local_path, dest)
            return dest

        try:
            logger.info(f"Downloading: {material.title} from {material.url}")

            response = get_with_retries(self.client, material.url)

            # Determine filename
            filename = material.filename
            if not filename:
                # Try to get from Content-Disposition header
                cd = response.headers.get("content-disposition", "")
                if "filename=" in cd:
                    filename = cd.split("filename=")[-1].strip('"')
                else:
                    # Generate from URL
                    filename = material.url.split("/")[-1]
                    if "?" in filename:
                        filename = filename.split("?")[0]

            # Ensure filename has extension
            if not Path(filename).suffix:
                content_type = response.headers.get("content-type", "")
                ext = mimetypes.guess_extension(content_type.split(";")[0])
                if ext:
                    filename = f"{filename}{ext}"

            # Save file
            self.download_dir.mkdir(parents=True, exist_ok=True)
            output_path = self.download_dir / filename
            output_path.write_bytes(response.content)

            logger.info(f"Downloaded: {output_path}")
            return output_path

        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP error downloading {material.url}: {e}")
        except Exception as e:
            logger.error(f"Failed to download {material.url}: {e}")

        return None

    def download_all_materials(
        self, materials: list[IETFMaterial]
    ) -> dict[str, Path]:
        """Download all materials for a session.

        Args:
            materials: List of materials to download

        Returns:
            Dictionary mapping material type to downloaded file path
        """
        downloaded = {}

        for material in materials:
            path = self.download_material(material)
            if path:
                # Use type + order as key to handle multiple slides
                key = material.type
                if material.order is not None:
                    key = f"{material.type}_{material.order}"
                downloaded[key] = path

        return downloaded

    def get_material_content(self, material: IETFMaterial) -> bytes | None:
        """Get the content of a material without saving to disk."""
        return self.fetch_material(material)[0]

    def fetch_material(
        self, material: IETFMaterial
    ) -> tuple[bytes | None, str | None, str | None]:
        """Fetch a material and report what it actually is.

        Returns (content, mediatype, filename). The Datatracker API does not
        say what a material's media type is, so the type was previously guessed
        from the document name -- minutes and agendas were labelled
        application/pdf and given a .pdf filename when most of them are plain
        text. Now that the bytes are fetched anyway (an external reference needs
        a content_hash), the observed type is used instead of a guess.
        """
        local = self._mirror_path(material)
        if local:
            logger.info("Using mirror: %s", local)
            MaterialsDownloader.mirror_served += 1
            mediatype, _ = mimetypes.guess_type(local.name)
            return local.read_bytes(), mediatype, local.name
        if material.type == "document":
            # The catch-all type (session polls, for one) has no proceedings
            # directory of its own, so the verbatim URL built for it points at
            # a directory that does not exist and always answers 404. The
            # material stays a reference in the attachment body.
            logger.info("Not fetching %s: no published file location", material.url)
            return None, None, None
        # Fetch the verbatim file where one is known. The Datatracker material
        # URL renders and converts, so hashing what it returns would describe a
        # view rather than the published document.
        target = material.file_url or material.url
        try:
            response = get_with_retries(self.client, target)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in NOT_AVAILABLE_STATUSES:
                # The server says the document is not there: an answer. The
                # material falls back to a body reference.
                logger.warning(f"Could not fetch {target}: HTTP {status}")
                MaterialsDownloader.not_available[status] = (
                    MaterialsDownloader.not_available.get(status, 0) + 1
                )
                return None, None, None
            raise MaterialFetchError(f"could not fetch {target}: HTTP {status}") from e
        except httpx.HTTPError as e:
            raise MaterialFetchError(
                f"could not fetch {target}: {type(e).__name__}: {e}"
            ) from e
        MaterialsDownloader.http_fetched += 1

        mediatype = (response.headers.get("content-type") or "").split(";")[0].strip() or None
        # www.ietf.org serves Markdown as application/octet-stream. That is the
        # server declining to guess, not a statement about the content, so the
        # filename wins wherever it implies something specific.
        if mediatype in (None, "application/octet-stream"):
            guessed, _ = mimetypes.guess_type(material.uploaded_filename or material.url)
            mediatype = guessed or mediatype
        # The name of the file actually read: the fetched URL's own last
        # segment when it names a file, else what the server calls it.
        filename = None
        segment = unquote(urlparse(target).path.rsplit("/", 1)[-1])
        if Path(segment).suffix:
            filename = segment
        else:
            disposition = response.headers.get("content-disposition", "")
            if "filename=" in disposition:
                filename = disposition.split("filename=")[-1].strip('"; ')
        return response.content, mediatype, filename

    def compute_hash(self, content: bytes, algorithm: str = "sha512") -> str:
        """Content hash in the form core-02 requires.

        Was a bare hex SHA-256, which no conforming reader can verify: the
        spec's ContentHash is `<algorithm>-<base64url digest, unpadded>` and
        SHA-512 is the algorithm that MUST be supported.
        """
        return content_hash_token(content, algorithm)

    def get_mimetype(self, filepath: Path) -> str:
        """Determine the MIME type of a file.

        Args:
            filepath: Path to the file

        Returns:
            MIME type string
        """
        mime_type, _ = mimetypes.guess_type(str(filepath))
        return mime_type or "application/octet-stream"


def organize_materials_by_type(
    materials: list[IETFMaterial],
) -> dict[str, list[IETFMaterial]]:
    """Organize materials by their type.

    Args:
        materials: List of materials

    Returns:
        Dictionary mapping type to list of materials
    """
    organized: dict[str, list[IETFMaterial]] = {}

    for material in materials:
        if material.type not in organized:
            organized[material.type] = []
        organized[material.type].append(material)

    # Sort slides by order
    if "slides" in organized:
        organized["slides"].sort(key=lambda m: m.order or 0)

    return organized
