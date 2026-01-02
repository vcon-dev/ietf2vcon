"""IETF meeting materials downloader.

Downloads slides, agendas, minutes, and other materials from the IETF Datatracker.
"""

import hashlib
import logging
import mimetypes
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import IETFMaterial

logger = logging.getLogger(__name__)


class MaterialsDownloader:
    """Download and manage IETF meeting materials."""

    def __init__(self, download_dir: Path | None = None, timeout: float = 60.0):
        self.download_dir = download_dir or Path("./materials")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def download_material(self, material: IETFMaterial) -> Path | None:
        """Download a single material file.

        Args:
            material: The material to download

        Returns:
            Path to the downloaded file, or None if failed
        """
        try:
            logger.info(f"Downloading: {material.title} from {material.url}")

            response = self.client.get(material.url)
            response.raise_for_status()

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
        """Get the content of a material without saving to disk.

        Args:
            material: The material to fetch

        Returns:
            Raw bytes content, or None if failed
        """
        try:
            response = self.client.get(material.url)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.error(f"Failed to fetch {material.url}: {e}")
            return None

    def compute_hash(self, content: bytes, algorithm: str = "sha256") -> str:
        """Compute hash of content for integrity verification.

        Args:
            content: Raw bytes to hash
            algorithm: Hash algorithm (sha256, sha512, etc.)

        Returns:
            Hex-encoded hash string
        """
        hasher = hashlib.new(algorithm)
        hasher.update(content)
        return hasher.hexdigest()

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
