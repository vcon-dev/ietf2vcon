"""Behaviour that matters when a published corpus is regenerated in place."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from vcon import Vcon

from ietf2vcon.converter import (
    ConversionOptions,
    ConversionResult,
    IETFSessionConverter,
    vcon_file_text,
)
from ietf2vcon.materials import MaterialsDownloader
from ietf2vcon.models import IETFMaterial
from ietf2vcon.vcon_builder import VConBuilder

# Set to a published vCon file that holds non-ASCII text to run the byte-for-byte
# round trip against real data; the test is skipped without it.
PUBLISHED_SAMPLE = Path(os.environ.get("IETF2VCON_PUBLISHED_SAMPLE", "/nonexistent"))


def result_for(vcon):
    return ConversionResult(
        vcon=vcon, meeting_number=126, group_acronym="x", session_id="1"
    )


def written(tmp_path, data):
    vcon = Vcon.build_new()
    vcon.vcon_dict = data
    converter = IETFSessionConverter(ConversionOptions(output_dir=tmp_path))
    path = converter.save_vcon(result_for(vcon), tmp_path / "out.vcon.json")
    return path.read_bytes()


class TestFileLayout:
    """T2: the published layout is indent=2, raw UTF-8, one trailing newline."""

    DATA = {
        "vcon": "0.5.0",
        "uuid": "018f2c3e-7b1a-8c4d-9e2f-0a1b2c3d4e5f",
        "created_at": "2026-08-11T18:50:35.898401+00:00",
        "subject": "caf\u00e9 na\u00efve",
        "parties": [{"name": "Chair"}],
        "z_last": {"b": 1, "a": [1, 2]},
    }

    def test_written_file_is_the_published_layout(self, tmp_path):
        raw = written(tmp_path, self.DATA).decode("utf-8")
        assert raw == json.dumps(self.DATA, indent=2, ensure_ascii=False) + "\n"
        assert "caf\u00e9" in raw and "\\u00e9" not in raw
        assert raw.endswith("}\n") and not raw.endswith("\n\n")

    def test_key_order_is_kept(self, tmp_path):
        loaded = json.loads(written(tmp_path, self.DATA))
        assert list(loaded) == list(self.DATA)

    def test_every_writer_uses_it(self, tmp_path):
        builder = VConBuilder()
        vcon = builder.build()
        assert vcon_file_text(vcon) == json.dumps(
            vcon.vcon_dict, indent=2, ensure_ascii=False
        ) + "\n"

    @pytest.mark.skipif(not PUBLISHED_SAMPLE.is_file(), reason="trial data not present")
    def test_published_file_round_trips_byte_for_byte(self, tmp_path):
        original = PUBLISHED_SAMPLE.read_bytes()
        assert any(b > 127 for b in original)  # carries non-ASCII text
        assert written(tmp_path, json.loads(original)) == original


class TestCreatedAt:
    """T3: core-05 4.1.5, created_at should not change once the vCon is created."""

    PREVIOUS_CREATED = "2026-08-11T18:50:35.898401+00:00"

    def _previous(self, tmp_path, **extra):
        (tmp_path / "ietf125_6lo_35225.vcon.json").write_text(json.dumps({
            "vcon": "0.5.0", "uuid": "019d3273-65c9-8df3-9dd8-dd37220d739c",
            "created_at": self.PREVIOUS_CREATED, "updated_at": self.PREVIOUS_CREATED,
            **extra,
        }))

    def _carry(self, tmp_path, **options):
        converter = IETFSessionConverter(
            ConversionOptions(output_dir=tmp_path, **options)
        )
        vcon = VConBuilder().build()
        converter._carry_over_previous(vcon, 125, "6lo", "35225", [])
        return vcon.vcon_dict

    def test_carried_uuid_brings_created_at_and_updated_at_is_now(self, tmp_path):
        self._previous(tmp_path)
        before = datetime.now(timezone.utc)
        data = self._carry(tmp_path)

        assert data["uuid"] == "019d3273-65c9-8df3-9dd8-dd37220d739c"
        assert data["created_at"] == self.PREVIOUS_CREATED
        assert datetime.fromisoformat(data["updated_at"]) >= before

    def test_created_at_of_a_0_4_0_file_is_carried_too(self, tmp_path):
        self._previous(tmp_path)
        path = tmp_path / "ietf125_6lo_35225.vcon.json"
        previous = json.loads(path.read_text())
        previous["vcon"] = "0.4.0"
        path.write_text(json.dumps(previous))

        assert self._carry(tmp_path)["created_at"] == self.PREVIOUS_CREATED

    def test_new_file_gets_the_conversion_time(self, tmp_path):
        before = datetime.now(timezone.utc)
        data = self._carry(tmp_path)  # nothing previous in tmp_path

        assert datetime.fromisoformat(data["created_at"]) >= before
        assert "updated_at" in data

    def test_without_the_uuid_the_creation_time_is_new_as_well(self, tmp_path):
        self._previous(tmp_path)
        data = self._carry(tmp_path, preserve_uuid=False)

        assert data["uuid"] != "019d3273-65c9-8df3-9dd8-dd37220d739c"
        assert data["created_at"] != self.PREVIOUS_CREATED


class TestMaterialsDirectory:
    """T4: no empty materials/ directory when nothing is written there."""

    def test_constructing_a_downloader_creates_nothing(self, tmp_path):
        target = tmp_path / "materials"
        downloader = MaterialsDownloader(download_dir=target)
        downloader.close()
        assert not target.exists()

    def test_converter_path_with_no_download_leaves_no_directory(self, tmp_path):
        converter = IETFSessionConverter(ConversionOptions(output_dir=tmp_path))
        builder = VConBuilder()
        converter._process_materials_list(builder, [], [], [])
        landing = IETFMaterial(
            type="agenda", title="t", url="https://example.com/a", landing_page=True,
        )
        converter._process_materials_list(builder, [landing], [], [])
        assert not (tmp_path / "materials").exists()

    def test_mirror_copy_creates_it_on_demand(self, tmp_path):
        mirror = tmp_path / "mirror" / "proceedings" / "126" / "slides"
        mirror.mkdir(parents=True)
        (mirror / "slides-126-x-a-00.pdf").write_bytes(b"%PDF")
        target = tmp_path / "materials"
        downloader = MaterialsDownloader(
            download_dir=target, mirror_dir=tmp_path / "mirror"
        )
        material = IETFMaterial(
            type="slides", title="t", filename="slides-126-x-a-00.pdf",
            url="https://datatracker.ietf.org/meeting/126/materials/slides-126-x-a",
            uploaded_filename="slides-126-x-a-00.pdf",
        )
        path = downloader.download_material(material)
        downloader.close()
        assert path == target / "slides-126-x-a-00.pdf" and path.read_bytes() == b"%PDF"


class TestPollsAndMissingDocuments:
    """T5: polls are `document` materials with no file location, not ERRORs."""

    POLLS = IETFMaterial(
        type="document",
        title="Polls",
        url="https://datatracker.ietf.org/meeting/126/materials/polls-126-acme-202607231630",
        file_url="https://www.ietf.org/proceedings/126/document/polls-126-acme-202607231630-00.json",
        filename="polls-126-acme-202607231630.pdf",
        mimetype="application/pdf",
        uploaded_filename="polls-126-acme-202607231630-00.json",
    )

    def test_document_type_is_not_requested(self, tmp_path):
        downloader = MaterialsDownloader(download_dir=tmp_path / "m")

        def refuse(*args, **kwargs):
            raise AssertionError("no request expected")

        downloader.client.get = refuse
        assert downloader.fetch_material(self.POLLS) == (None, None, None)
        downloader.close()

    def test_polls_stay_a_body_reference_like_the_published_files(self, tmp_path):
        builder = VConBuilder()
        builder.add_materials([self.POLLS], downloader=MaterialsDownloader(tmp_path / "m"))
        att = builder.vcon.vcon_dict["attachments"][-1]
        assert att["purpose"] == "document"
        assert att["encoding"] == "json" and "url" not in att
        assert att["body"]["url"] == self.POLLS.url

    def test_a_404_is_a_warning_not_an_error(self, tmp_path, caplog):
        downloader = MaterialsDownloader(download_dir=tmp_path / "m")
        slides = IETFMaterial(
            type="slides", title="t", url="https://datatracker.ietf.org/x",
            file_url="https://www.ietf.org/proceedings/126/slides/gone.pdf",
        )

        def not_found(url, **kwargs):
            request = httpx.Request("GET", url)
            return httpx.Response(404, request=request)

        downloader.client.get = not_found
        with caplog.at_level("INFO", logger="ietf2vcon.materials"):
            assert downloader.fetch_material(slides) == (None, None, None)
        downloader.close()

        assert not [r for r in caplog.records if r.levelname == "ERROR"]
        assert [r for r in caplog.records if r.levelname == "WARNING"]
