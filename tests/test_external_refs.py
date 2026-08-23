"""The converter must not invent URLs, and must reference files the way core-02 says.

Three bugs these cover, each of which shipped into all 8,179 published vCons:

  * every session carried an agenda link to /meeting/N/agenda/<wg>/, which 404s
  * every draft discussed in session was linked as a meeting material, which 404s
  * every material was buried in a JSON body instead of being an external
    reference, and the one path that did emit a content_hash used bare hex
    SHA-256, which no conforming reader can verify
"""

import json

import pytest

from ietf2vcon.hashing import content_hash_token
from ietf2vcon.models import IETFMaterial
from ietf2vcon.vcon_builder import VConBuilder


SLIDES = IETFMaterial(
    type="slides",
    title="Zenzeleni Community Networks",
    url="https://datatracker.ietf.org/meeting/108/materials/slides-108-gaia-zenzeleni",
    filename="slides-108-gaia-zenzeleni.pdf",
    mimetype="application/pdf",
)
SESSION_PAGE = IETFMaterial(
    type="agenda",
    title="GAIA Agenda",
    url="https://datatracker.ietf.org/meeting/108/session/gaia/",
    mimetype="text/html",
    landing_page=True,
)


@pytest.fixture
def builder():
    return VConBuilder()


def attachments(builder):
    return builder.vcon.vcon_dict["attachments"]


def test_content_hash_is_a_spec_token():
    token = content_hash_token(b"slide deck bytes")
    algorithm, _, digest = token.partition("-")
    assert algorithm == "sha512"
    assert digest and "=" not in digest and "+" not in digest and "/" not in digest


def test_fetched_material_becomes_an_external_reference(builder):
    builder.add_material_attachment(SLIDES, content=b"slide deck bytes")
    att = attachments(builder)[-1]

    assert att["url"] == SLIDES.url
    assert att["content_hash"] == content_hash_token(b"slide deck bytes")
    assert att["mediatype"] == "application/pdf"
    assert att["filename"] == SLIDES.filename
    assert att["meta"]["title"] == SLIDES.title
    # The whole point: no opaque JSON blob standing in for a linked document.
    assert "body" not in att or att["body"] is None
    assert "meta" in builder.vcon.vcon_dict["extensions"]


def test_landing_page_stays_a_body_reference(builder):
    """A mutable HTML page must not get a content_hash it cannot honour."""
    builder.add_material_attachment(SESSION_PAGE, content=b"<html>today</html>")
    att = attachments(builder)[-1]

    assert att.get("url") is None
    assert att.get("content_hash") is None
    assert json.loads(att["body"])["url"] == SESSION_PAGE.url


def test_unfetchable_material_stays_a_body_reference(builder):
    """core-02 requires url and content_hash together, so no half-promotion."""
    builder.add_material_attachment(SLIDES, content=None)
    att = attachments(builder)[-1]

    assert att.get("url") is None
    assert json.loads(att["body"])["url"] == SLIDES.url


def test_inline_material_hashes_in_spec_form(builder):
    builder.add_material_attachment(SLIDES, content=b"pdf bytes", inline=True)
    att = attachments(builder)[-1]

    assert att["encoding"] == "base64url"
    assert att["content_hash"] == content_hash_token(b"pdf bytes")
    assert att["content_hash"].startswith("sha512-")


def test_regeneration_keeps_the_existing_uuid(tmp_path):
    """A regenerated session is the same conversation, so the uuid survives.

    Without this, regenerating the corpus orphans every row in the published
    dataset and every citation of a session.
    """
    from ietf2vcon.converter import ConversionOptions, IETFSessionConverter

    published = tmp_path / "ietf125_6lo_35225.vcon.json"
    published.write_text(json.dumps({"uuid": "019d3273-65c9-8df3-9dd8-dd37220d739c"}))

    converter = IETFSessionConverter(ConversionOptions(output_dir=tmp_path))
    vcon = VConBuilder().build()
    minted = vcon.vcon_dict["uuid"]

    converter._reuse_existing_uuid(vcon, 125, "6lo", "35225")

    assert vcon.vcon_dict["uuid"] == "019d3273-65c9-8df3-9dd8-dd37220d739c"
    assert vcon.vcon_dict["uuid"] != minted


def test_first_generation_mints_a_uuid(tmp_path):
    from ietf2vcon.converter import ConversionOptions, IETFSessionConverter

    converter = IETFSessionConverter(ConversionOptions(output_dir=tmp_path))
    vcon = VConBuilder().build()
    minted = vcon.vcon_dict["uuid"]

    converter._reuse_existing_uuid(vcon, 125, "6lo", "35225")

    assert vcon.vcon_dict["uuid"] == minted
