"""Regeneration must not silently rewrite or drop what was said.

Re-running ASR over a session produces different text than what was published,
and without a provenance record nobody can tell which version they are holding.
A regeneration meant to fix attachment structure therefore carries the existing
transcript across rather than reproducing it.
"""

import json

import pytest

from ietf2vcon.converter import ConversionOptions, IETFSessionConverter
from ietf2vcon.vcon_builder import VConBuilder


TRANSCRIPT = {
    "type": "wtf_transcription",
    "dialog": 0,
    "vendor": "whisper",
    "url": "https://github.com/vcon-dev/ietf-meeting-vcons/releases/download/t/x.wtf.json",
    "content_hash": "sha512-PQfvoVLhPReRR",
    "mediatype": "application/json",
}
RECORDING = {
    "type": "recording",
    "start": "2026-03-16T03:30:00+00:00",
    "url": "https://youtu.be/2aUH7XIe8Cg",
    "mediatype": "video/mp4",
}


@pytest.fixture
def published(tmp_path):
    """A previously published record, as it sits in ietf-meeting-vcons."""
    path = tmp_path / "ietf125_6lo_35225.vcon.json"
    path.write_text(json.dumps({
        "uuid": "019d3273-65c9-8df3-9dd8-dd37220d739c",
        "dialog": [RECORDING],
        "analysis": [TRANSCRIPT],
        "extensions": ["lawful_basis", "wtf_transcription", "role", "meta"],
    }))
    return path


@pytest.fixture
def converter(tmp_path):
    return IETFSessionConverter(ConversionOptions(output_dir=tmp_path))


def carry(converter, vcon, warnings=None):
    converter._carry_over_previous(vcon, 125, "6lo", "35225", warnings if warnings is not None else [])


def test_transcript_and_its_dialog_come_across(converter, published):
    """A structure-only regeneration produces neither, so both are carried."""
    vcon = VConBuilder().build()
    carry(converter, vcon)

    assert vcon.vcon_dict["analysis"] == [TRANSCRIPT]
    assert vcon.vcon_dict["dialog"] == [RECORDING]
    # dialog index 0 still addresses the recording it was written against
    assert vcon.vcon_dict["analysis"][0]["dialog"] == 0


def test_extensions_that_describe_the_carried_content_come_too(converter, published):
    vcon = VConBuilder().build()
    carry(converter, vcon)

    assert "wtf_transcription" in vcon.vcon_dict["extensions"]


def test_a_fresh_transcript_is_not_overwritten(converter, published):
    """Conversion with transcription enabled wins; carry-over is a fallback."""
    vcon = VConBuilder().build()
    fresh = {"type": "wtf_transcription", "dialog": 0, "vendor": "youtube", "body": "fresh"}
    vcon.vcon_dict["analysis"] = [fresh]
    vcon.vcon_dict["dialog"] = [RECORDING]
    carry(converter, vcon)

    assert vcon.vcon_dict["analysis"] == [fresh]


def test_dangling_analysis_is_left_behind_and_reported(converter, tmp_path):
    """An analysis pointing past the end of the dialog must not be carried."""
    (tmp_path / "ietf125_6lo_35225.vcon.json").write_text(json.dumps({
        "uuid": "u", "dialog": [], "analysis": [dict(TRANSCRIPT, dialog=3)],
    }))
    vcon = VConBuilder().build()
    vcon.vcon_dict["dialog"] = [RECORDING]
    warnings = []
    carry(converter, vcon, warnings)

    assert not vcon.vcon_dict.get("analysis")
    assert any("dialog this conversion did not produce" in w for w in warnings)


def test_carry_over_can_be_switched_off(tmp_path, published):
    converter = IETFSessionConverter(
        ConversionOptions(output_dir=tmp_path, preserve_analysis=False)
    )
    vcon = VConBuilder().build()
    carry(converter, vcon)

    assert not vcon.vcon_dict.get("analysis")
    # identity still survives; the two are independent
    assert vcon.vcon_dict["uuid"] == "019d3273-65c9-8df3-9dd8-dd37220d739c"


def test_first_generation_has_nothing_to_carry(converter, tmp_path):
    vcon = VConBuilder().build()
    carry(converter, vcon)

    assert not vcon.vcon_dict.get("analysis")
