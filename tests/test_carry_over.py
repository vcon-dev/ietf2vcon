"""Regeneration must not silently rewrite or drop what was said.

Re-running ASR over a session produces different text than what was published,
and without a provenance record nobody can tell which version they are holding.
A regeneration meant to fix attachment structure therefore carries the existing
transcript across rather than reproducing it.
"""

import json

import pytest

from ietf2vcon.converter import ConversionOptions, IETFSessionConverter
from ietf2vcon.errors import CarryOverError
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


def test_dangling_analysis_fails_the_session_instead_of_being_dropped(converter, tmp_path):
    """An analysis pointing past the end of the dialog is not carried, so the
    new document would have fewer entries than the file it replaces: refuse."""
    (tmp_path / "ietf125_6lo_35225.vcon.json").write_text(json.dumps({
        "uuid": "u", "dialog": [], "analysis": [dict(TRANSCRIPT, dialog=3)],
    }))
    vcon = VConBuilder().build()
    vcon.vcon_dict["dialog"] = [RECORDING]
    warnings = []
    with pytest.raises(CarryOverError, match="1 analysis entries and the new document has 0"):
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


def test_carried_dialog_is_listed_by_the_session(converter, published):
    """core-05: a Session lists its Dialogs, so adopted dialog joins sessions[0]."""
    vcon = VConBuilder().build()
    carry(converter, vcon)

    assert vcon.vcon_dict["sessions"][0]["dialog"] == [0]


# ---- extensions, Dialog meta, and party lists on carried content ----

def build_with_parties(count, role_on_first=False):
    builder = VConBuilder()
    builder.add_ietf_note_well()
    for n in range(count):
        builder.add_party(
            name=f"Party {n}", role="chair" if role_on_first and n == 0 else None
        )
    return builder.build()


def write_previous(tmp_path, parties, dialog_parties, **extra):
    """A previous 0.4.0 file as the old maintenance script left it."""
    (tmp_path / "ietf125_6lo_35225.vcon.json").write_text(json.dumps({
        "vcon": "0.4.0",
        "uuid": "019d3273-65c9-8df3-9dd8-dd37220d739c",
        "created_at": "2026-03-16T03:30:00+00:00",
        "parties": [{"name": f"Old {n}"} for n in range(parties)],
        "dialog": [dict(RECORDING, parties=dialog_parties, duration=3600,
                        meta={"source": "youtube"}, **extra)],
        "analysis": [dict(TRANSCRIPT, dialog=0)],
        "extensions": ["lawful_basis", "wtf_transcription", "role", "meta"],
    }))


def test_previous_role_and_meta_are_not_carried_and_cc_follows_the_parties(
    converter, published
):
    with_role = build_with_parties(2, role_on_first=True)
    carry(converter, with_role)
    extensions = with_role.vcon_dict["extensions"]
    assert "CC" in extensions
    assert not {"role", "meta"} & set(extensions)
    assert {"lawful_basis", "wtf_transcription"} <= set(extensions)
    assert len(extensions) == len(set(extensions))

    without_role = build_with_parties(2)
    carry(converter, without_role)
    extensions = without_role.vcon_dict["extensions"]
    assert "CC" not in extensions
    assert not {"role", "meta"} & set(extensions)
    assert {"lawful_basis", "wtf_transcription"} <= set(extensions)


def test_previous_cc_is_dropped_when_no_party_has_a_role(converter, tmp_path):
    (tmp_path / "ietf125_6lo_35225.vcon.json").write_text(json.dumps({
        "uuid": "u", "dialog": [RECORDING], "analysis": [TRANSCRIPT],
        "extensions": ["CC", "wtf_transcription"],
    }))
    vcon = build_with_parties(1)
    carry(converter, vcon)
    assert "CC" not in vcon.vcon_dict["extensions"]


def test_carried_dialog_from_0_4_0_has_no_meta_or_metadata(converter, tmp_path):
    write_previous(tmp_path, 2, [0, 1], metadata={"x": 1})
    vcon = build_with_parties(2)
    carry(converter, vcon)

    dialog = vcon.vcon_dict["dialog"][0]
    assert "meta" not in dialog and "metadata" not in dialog
    assert dialog["url"] == RECORDING["url"]


def test_inline_carried_analysis_gains_a_mediatype(converter, tmp_path):
    inline = {
        "type": "wtf_transcription", "dialog": 0, "vendor": "youtube",
        "body": "{\"segments\": []}", "encoding": "json",
    }
    (tmp_path / "ietf125_6lo_35225.vcon.json").write_text(json.dumps({
        "uuid": "u", "dialog": [RECORDING], "analysis": [inline, TRANSCRIPT],
    }))
    vcon = build_with_parties(1)
    carry(converter, vcon)

    carried = vcon.vcon_dict["analysis"]
    assert carried[0]["mediatype"] == "application/json"
    assert carried[1] == TRANSCRIPT  # external: left as it was


@pytest.mark.parametrize(
    "parties, previous",
    [
        (3, [0, 1, 2, 3]),  # points past the end of parties
        (4, [0, 1, 2]),     # in range, attendees party missing
    ],
)
def test_carried_dialog_lists_every_party_of_the_new_build(
    converter, tmp_path, caplog, parties, previous
):
    write_previous(tmp_path, parties, previous)
    vcon = build_with_parties(parties)
    with caplog.at_level("INFO", logger="ietf2vcon.converter"):
        carry(converter, vcon)

    everyone = list(range(len(vcon.vcon_dict["parties"])))
    assert everyone == list(range(parties))
    dialog = vcon.vcon_dict["dialog"][0]
    assert all(i < len(vcon.vcon_dict["parties"]) for i in dialog["parties"])
    assert dialog["parties"] == everyone
    # The Session's list comes from the new build, not from the carried dialog.
    assert vcon.vcon_dict["sessions"][0]["parties"] == everyone
    assert vcon.vcon_dict["sessions"][0]["dialog"] == [0]
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("CARRY-OVER dialog adopted") for m in messages)
    assert any(m.startswith("CARRY-OVER parties rebuilt") for m in messages)


def test_a_correct_party_list_is_not_reported_as_rebuilt(converter, tmp_path, caplog):
    write_previous(tmp_path, 3, [0, 1, 2])
    vcon = build_with_parties(3)
    with caplog.at_level("INFO", logger="ietf2vcon.converter"):
        carry(converter, vcon)

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("CARRY-OVER dialog adopted") for m in messages)
    assert not any(m.startswith("CARRY-OVER parties rebuilt") for m in messages)


def test_a_carried_text_dialog_with_a_single_sender_is_left_and_warned(
    converter, tmp_path, caplog
):
    chat = {"type": "text", "start": "2026-03-16T03:30:00+00:00", "parties": [1],
            "mediatype": "text/plain", "body": "hi", "encoding": "none"}
    (tmp_path / "ietf125_6lo_35225.vcon.json").write_text(json.dumps({
        "vcon": "0.5.0", "uuid": "u", "dialog": [chat], "analysis": [TRANSCRIPT],
    }))
    vcon = build_with_parties(3)
    with caplog.at_level("INFO", logger="ietf2vcon.converter"):
        carry(converter, vcon)

    assert vcon.vcon_dict["dialog"][0]["parties"] == [1]
    warned = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("ietf125_6lo_35225.vcon.json" in r.getMessage() for r in warned)


# ---- the recording Dialog survives whether or not there is analysis ----

def write_previous_without_analysis(tmp_path, analysis=...):
    data = {
        "vcon": "0.4.0",
        "uuid": "019d3273-65c9-8df3-9dd8-dd37220d739c",
        "created_at": "2026-03-16T03:30:00+00:00",
        "parties": [{"name": f"Old {n}"} for n in range(3)],
        "dialog": [dict(RECORDING, parties=[0, 1, 2, 3], duration=3600,
                        meta={"source": "youtube"})],
        "extensions": ["lawful_basis"],
    }
    if analysis is not ...:
        data["analysis"] = analysis
    (tmp_path / "ietf125_6lo_35225.vcon.json").write_text(json.dumps(data))


def structural_errors(vcon, tmp_path):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "validate_vcons.py"
    spec = importlib.util.spec_from_file_location("validate_vcons", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    validator = module.VConValidator()
    out = tmp_path / "out" / "x.vcon.json"
    out.parent.mkdir()
    out.write_text(json.dumps(vcon.vcon_dict))
    ok, errors, _ = validator.validate_file(out)
    return errors, validator.known_deviations


@pytest.mark.parametrize("analysis", [[], ...], ids=["empty analysis", "no analysis key"])
def test_dialog_is_carried_when_the_previous_file_has_no_analysis(
    converter, tmp_path, caplog, analysis
):
    write_previous_without_analysis(tmp_path, analysis)
    vcon = build_with_parties(3)
    with caplog.at_level("INFO", logger="ietf2vcon.converter"):
        carry(converter, vcon)

    dialog = vcon.vcon_dict["dialog"]
    assert len(dialog) == 1
    for key in ("url", "mediatype", "start"):
        assert dialog[0][key] == RECORDING[key]
    assert dialog[0]["duration"] == 3600
    assert "meta" not in dialog[0] and "metadata" not in dialog[0]
    assert dialog[0]["parties"] == list(range(len(vcon.vcon_dict["parties"])))
    assert vcon.vcon_dict["sessions"][0]["dialog"] == [0]
    assert not vcon.vcon_dict.get("analysis")
    assert any(
        r.getMessage().startswith("CARRY-OVER dialog adopted") for r in caplog.records
    )
    # this build has no official session time, so the only warning is that the
    # carried start could not be verified
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warned) == 1 and "unverified" in warned[0]

    errors, known = structural_errors(vcon, tmp_path)
    assert errors == []
    assert len(known) == 1  # the recording url without content_hash


def test_dialog_without_analysis_is_not_carried_when_carry_over_is_off(
    tmp_path, caplog
):
    write_previous_without_analysis(tmp_path, [])
    converter = IETFSessionConverter(
        ConversionOptions(output_dir=tmp_path, preserve_analysis=False)
    )
    vcon = build_with_parties(3)
    with caplog.at_level("INFO", logger="ietf2vcon.converter"):
        carry(converter, vcon)

    assert not vcon.vcon_dict.get("dialog")
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("ietf125_6lo_35225.vcon.json" in m and "lost the recording dialog" in m for m in warned)


def test_a_new_dialog_is_left_alone_and_no_warning_is_logged(
    converter, tmp_path, caplog
):
    write_previous_without_analysis(tmp_path, [])
    vcon = build_with_parties(3)
    fresh = dict(RECORDING, url="https://youtu.be/new", parties=[0, 1, 2])
    vcon.vcon_dict["dialog"] = [fresh]
    with caplog.at_level("INFO", logger="ietf2vcon.converter"):
        carry(converter, vcon)

    assert vcon.vcon_dict["dialog"] == [fresh]
    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_previous_file_with_neither_dialog_nor_analysis_carries_nothing(
    converter, tmp_path, caplog
):
    (tmp_path / "ietf125_6lo_35225.vcon.json").write_text(json.dumps({
        "vcon": "0.4.0", "uuid": "019d3273-65c9-8df3-9dd8-dd37220d739c",
        "created_at": "2026-03-16T03:30:00+00:00", "parties": [{"name": "a"}],
    }))
    vcon = build_with_parties(1)
    with caplog.at_level("INFO", logger="ietf2vcon.converter"):
        carry(converter, vcon)

    assert not vcon.vcon_dict.get("dialog") and not vcon.vcon_dict.get("analysis")
    assert vcon.vcon_dict["uuid"] == "019d3273-65c9-8df3-9dd8-dd37220d739c"
    assert not any(r.levelname == "WARNING" for r in caplog.records)


# ---- the carried recording takes the official session time ----

OFFICIAL_START = "2026-07-22T12:00:00+00:00"


def build_with_official_time(parties=2, duration=5400):
    from datetime import datetime, timezone

    from ietf2vcon.models import IETFMeeting, IETFSession

    builder = VConBuilder()
    builder.set_meeting_metadata(
        IETFMeeting(number=125),
        IETFSession(
            meeting_number=125, group_acronym="6lo", session_id="35225",
            start_time=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
            duration_seconds=duration,
        ),
    )
    for n in range(parties):
        builder.add_party(name=f"Party {n}")
    return builder.build()


def write_previous_recording(tmp_path, **dialog):
    (tmp_path / "ietf125_6lo_35225.vcon.json").write_text(json.dumps({
        "vcon": "0.4.0", "uuid": "019d3273-65c9-8df3-9dd8-dd37220d739c",
        "created_at": "2026-03-16T03:30:00+00:00",
        "parties": [{"name": "a"}, {"name": "b"}],
        "dialog": [{
            "type": "recording", "start": "2026-07-20T07:00:00+00:00", "duration": 3600,
            "parties": [0, 1], "mediatype": "audio/mpeg",
            "url": "https://www.ietf.org/audio/ietf125/room1.mp3", **dialog,
        }],
        "analysis": [],
    }))


def test_carried_recording_takes_the_official_session_start(converter, tmp_path, caplog):
    write_previous_recording(tmp_path)
    vcon = build_with_official_time(duration=5400)
    with caplog.at_level("INFO", logger="ietf2vcon.converter"):
        carry(converter, vcon)

    dialog = vcon.vcon_dict["dialog"][0]
    assert dialog["start"] == vcon.vcon_dict["sessions"][0]["start"] == OFFICIAL_START
    assert dialog["duration"] == 5400  # a fresh audio dialog takes the session's
    assert dialog["url"] == "https://www.ietf.org/audio/ietf125/room1.mp3"
    assert dialog["mediatype"] == "audio/mpeg" and dialog["type"] == "recording"
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "start 2026-07-20T07:00:00+00:00 -> " + OFFICIAL_START in m for m in messages
    )
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_a_youtube_recording_keeps_its_own_duration(converter, tmp_path):
    write_previous_recording(
        tmp_path, url="https://www.youtube.com/watch?v=abc", mediatype="video/mp4",
        duration=5011,
    )
    vcon = build_with_official_time(duration=7200)
    carry(converter, vcon)

    dialog = vcon.vcon_dict["dialog"][0]
    assert dialog["start"] == OFFICIAL_START
    assert dialog["duration"] == 5011  # the video's length, not the slot's


def test_an_unchanged_start_is_not_logged_as_changed(converter, tmp_path, caplog):
    write_previous_recording(tmp_path, start=OFFICIAL_START.replace("+00:00", "Z"))
    vcon = build_with_official_time()
    with caplog.at_level("INFO", logger="ietf2vcon.converter"):
        carry(converter, vcon)

    assert vcon.vcon_dict["dialog"][0]["start"] == OFFICIAL_START
    assert not any(" -> " in r.getMessage() and "start" in r.getMessage() for r in caplog.records)


def test_without_an_official_time_the_carried_start_stays_and_is_warned(
    converter, tmp_path, caplog
):
    write_previous_recording(tmp_path)
    vcon = build_with_parties(2)  # no session start
    with caplog.at_level("INFO", logger="ietf2vcon.converter"):
        carry(converter, vcon)

    dialog = vcon.vcon_dict["dialog"][0]
    assert dialog["start"] == "2026-07-20T07:00:00+00:00" and dialog["duration"] == 3600
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("ietf125_6lo_35225.vcon.json" in m and "unverified" in m for m in warned)
