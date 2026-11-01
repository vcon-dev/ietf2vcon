"""vCon core-05 (syntax 0.5.0) conformance of the builder and converter."""

import importlib.util
import json
from pathlib import Path

import pytest

from ietf2vcon.models import IETFPerson
from ietf2vcon.vcon_builder import VConBuilder

VALIDATOR = Path(__file__).parent.parent / "scripts" / "validate_vcons.py"


@pytest.fixture(scope="module")
def validator():
    spec = importlib.util.spec_from_file_location("validate_vcons", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.VConValidator()


def full_builder(meeting, session, video, messages, transcript, materials, chairs=True):
    b = VConBuilder()
    b.set_meeting_metadata(meeting, session)
    b.add_ingress_info(source="ietf2vcon")
    b.add_ietf_note_well()
    if chairs:
        b.add_persons([IETFPerson(name="Chair One", role="chair")])
    b.add_attendees_party()
    d = b.add_video_dialog(video, session)
    b.add_chat_dialog(messages, session)
    b.add_materials(materials)
    b.add_transcript(transcript, d)
    return b


def check(validator, tmp_path, builder):
    path = tmp_path / "x.vcon.json"
    path.write_text(builder.to_json())
    ok, errors, _ = validator.validate_file(path)
    assert ok, errors
    return json.loads(path.read_text())


def test_full_vcon_is_core05(
    validator, tmp_path, sample_ietf_meeting, sample_ietf_session, sample_video_metadata,
    sample_chat_messages, sample_transcription_result, sample_materials,
):
    b = full_builder(
        sample_ietf_meeting, sample_ietf_session, sample_video_metadata,
        sample_chat_messages, sample_transcription_result, sample_materials,
    )
    data = check(validator, tmp_path, b)
    assert data["vcon"] == "0.5.0"
    assert len(data["sessions"]) == 1
    sess = data["sessions"][0]
    assert sess["dialog"] == [0, 1]
    assert sess["parties"] == [0, 1]
    assert "originator" not in sess  # organizer is first party
    assert sess["duration"] == 3600
    assert [d["type"] for d in data["dialog"]] == ["recording", "text"]
    assert "events" not in data


def test_every_attachment_has_party_and_scope(
    validator, tmp_path, sample_ietf_meeting, sample_ietf_session, sample_video_metadata,
    sample_chat_messages, sample_transcription_result, sample_materials,
):
    b = full_builder(
        sample_ietf_meeting, sample_ietf_session, sample_video_metadata,
        sample_chat_messages, sample_transcription_result, sample_materials,
    )
    data = check(validator, tmp_path, b)
    assert data["attachments"]
    for att in data["attachments"]:
        assert "party" in att
        assert "dialog" in att or "session" in att
    lb = [a for a in data["attachments"] if a["purpose"] == "lawful_basis"]
    assert len(lb) == 1
    assert lb[0]["session"] == 0 and "party" in lb[0]
    assert "lawful_basis" in data["extensions"]


def test_transcript_points_at_recording_dialog(
    validator, tmp_path, sample_ietf_meeting, sample_ietf_session, sample_video_metadata,
    sample_chat_messages, sample_transcription_result, sample_materials,
):
    b = full_builder(
        sample_ietf_meeting, sample_ietf_session, sample_video_metadata,
        sample_chat_messages, sample_transcription_result, sample_materials,
    )
    data = check(validator, tmp_path, b)
    assert data["analysis"][0]["dialog"] == 0


def test_transcript_without_dialog_scopes_to_session(sample_ietf_meeting, sample_ietf_session,
                                                    sample_transcription_result):
    b = VConBuilder()
    b.set_meeting_metadata(sample_ietf_meeting, sample_ietf_session)
    b.add_attendees_party()
    b.add_transcript(sample_transcription_result, -1)
    analysis = b.build().vcon_dict["analysis"][0]
    assert "dialog" not in analysis and analysis["sessions"] == 0


def test_no_chairs_leaves_attendees_first_and_no_originator(
    validator, tmp_path, sample_ietf_meeting, sample_ietf_session, sample_video_metadata,
    sample_chat_messages, sample_transcription_result, sample_materials,
):
    b = full_builder(
        sample_ietf_meeting, sample_ietf_session, sample_video_metadata,
        sample_chat_messages, sample_transcription_result, sample_materials, chairs=False,
    )
    data = check(validator, tmp_path, b)
    assert data["sessions"][0]["parties"] == [0]
    assert data["parties"][0]["name"] == "IETF Attendees"


def test_no_legacy_dialog_fields(sample_ietf_meeting, sample_ietf_session, sample_video_metadata):
    b = VConBuilder()
    b.set_meeting_metadata(sample_ietf_meeting, sample_ietf_session)
    b.add_attendees_party()
    b.add_video_dialog(sample_video_metadata, sample_ietf_session)
    d = b.build().vcon_dict["dialog"][0]
    for legacy in ("party_history", "originator", "recordings", "recording_set"):
        assert legacy not in d


def test_validator_rejects_attachment_without_scope(validator, tmp_path):
    bad = {
        "vcon": "0.5.0", "uuid": "018f2c3e-7b1a-8c4d-9e2f-0a1b2c3d4e5f",
        "created_at": "2025-01-01T00:00:00Z",
        "parties": [{"name": "a"}],
        "attachments": [{"purpose": "x", "party": 0, "body": "b", "encoding": "none"}],
    }
    path = tmp_path / "bad.vcon.json"
    path.write_text(json.dumps(bad))
    ok, errors, _ = validator.validate_file(path)
    assert not ok
    assert any("dialog or session" in e or "Schema" in e for e in errors)


def test_wtf_server_payload_is_core05():
    import inspect

    from ietf2vcon import transcription

    src = inspect.getsource(transcription)
    assert '"vcon": "0.5.0"' in src and '"vcon": "0.4.0"' not in src


# ---- known deviation: recording url without content_hash (WG issue #40) ----

def _vcon_with(dialog=None, attachment=None):
    data = {
        "vcon": "0.5.0", "uuid": "018f2c3e-7b1a-8c4d-9e2f-0a1b2c3d4e5f",
        "created_at": "2025-01-01T00:00:00Z",
        "parties": [{"name": "a"}],
        "sessions": [{"start": "2025-01-01T00:00:00Z", "dialog": [0]}],
        "dialog": [dialog or {
            "type": "recording", "start": "2025-01-01T00:00:00Z", "parties": [0],
            "mediatype": "video/mp4", "url": "https://www.youtube.com/watch?v=abc",
        }],
    }
    if attachment:
        data["attachments"] = [attachment]
    return data


def _run(validator, tmp_path, data):
    path = tmp_path / "k.vcon.json"
    path.write_text(json.dumps(data))
    ok, errors, _ = validator.validate_file(path)
    return ok, errors, list(validator.known_deviations)


def test_recording_dialog_has_url_no_hash_no_meta(
    sample_ietf_meeting, sample_ietf_session, sample_video_metadata
):
    b = VConBuilder()
    b.set_meeting_metadata(sample_ietf_meeting, sample_ietf_session)
    b.add_attendees_party()
    b.add_video_dialog(sample_video_metadata, sample_ietf_session)
    d = b.build().vcon_dict["dialog"][0]
    assert d["url"] == sample_video_metadata.url
    assert "content_hash" not in d and "meta" not in d


def test_validator_passes_only_known_deviation(validator, tmp_path):
    ok, errors, known = _run(validator, tmp_path, _vcon_with())
    assert ok and errors == []
    assert len(known) == 1 and "dialog/0" in known[0]


@pytest.mark.parametrize("url", [
    "https://youtu.be/abc",
    "https://www.ietf.org/audio/ietf90/room1.mp3",
])
def test_known_deviation_recording_hosts(validator, tmp_path, url):
    d = {"type": "recording", "start": "2025-01-01T00:00:00Z", "parties": [0],
         "mediatype": "audio/mpeg", "url": url}
    ok, _, known = _run(validator, tmp_path, _vcon_with(dialog=d))
    assert ok and len(known) == 1


def test_validator_fails_url_without_hash_on_non_recording_url(validator, tmp_path):
    d = {"type": "recording", "start": "2025-01-01T00:00:00Z", "parties": [0],
         "mediatype": "video/mp4", "url": "https://example.com/rec.mp4"}
    ok, errors, known = _run(validator, tmp_path, _vcon_with(dialog=d))
    assert not ok and known == []
    assert any("content_hash" in e for e in errors)


def test_validator_fails_url_without_hash_on_attachment(validator, tmp_path):
    att = {"purpose": "x", "party": 0, "session": 0,
           "mediatype": "text/html", "url": "https://www.youtube.com/watch?v=abc"}
    ok, errors, known = _run(validator, tmp_path, _vcon_with(attachment=att))
    assert not ok
    assert any("attachments" in e and "content_hash" in e for e in errors)
    assert len(known) == 1  # the dialog still reports; the attachment fails


def test_validator_fails_known_deviation_plus_other_error(validator, tmp_path):
    data = _vcon_with()
    data["dialog"][0]["type"] = "bogus"
    ok, errors, _ = _run(validator, tmp_path, data)
    assert not ok and errors
