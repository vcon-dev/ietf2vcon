"""vCon core-05 (syntax 0.5.0) conformance of the builder and converter."""

import importlib.util
import json
from pathlib import Path

import pytest

from ietf2vcon.models import IETFPerson
from ietf2vcon.vcon_builder import VConBuilder

VALIDATOR = Path(__file__).parent.parent / "scripts" / "validate_vcons.py"
RECORDING_URL_LABEL = "recording Dialog url (YouTube / IETF recording) without content_hash"


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


def test_full_vcon_has_no_undefined_meta_and_declares_cc(
    validator, tmp_path, sample_ietf_meeting, sample_ietf_session, sample_video_metadata,
    sample_chat_messages, sample_transcription_result, sample_materials,
):
    b = full_builder(
        sample_ietf_meeting, sample_ietf_session, sample_video_metadata,
        sample_chat_messages, sample_transcription_result, sample_materials,
    )
    data = check(validator, tmp_path, b)
    assert sorted(data["extensions"]) == ["CC", "lawful_basis", "wtf_transcription"]
    for kind in ("parties", "dialog", "attachments"):
        assert all("meta" not in item for item in data[kind])
    for att in data["attachments"]:
        if att.get("body") is not None:
            assert att["mediatype"] == "application/json"


def test_chat_log_attachment_is_json_with_mediatype(
    validator, tmp_path, monkeypatch, sample_ietf_meeting, sample_ietf_session,
    sample_chat_messages,
):
    """With chat_as_dialog off, the chat log is an inline JSON attachment."""
    from ietf2vcon import converter as converter_module
    from ietf2vcon.converter import ConversionOptions, IETFSessionConverter

    class FakeZulip:
        def __init__(self, **kwargs):
            pass

        def get_session_messages(self, *args):
            return sample_chat_messages

        def close(self):
            pass

    monkeypatch.setattr(converter_module, "ZulipClient", FakeZulip)
    converter = IETFSessionConverter(ConversionOptions(
        output_dir=tmp_path, chat_as_dialog=False,
        zulip_email="x@example.com", zulip_api_key="k",
    ))
    b = VConBuilder()
    b.set_meeting_metadata(sample_ietf_meeting, sample_ietf_session)
    b.add_ingress_info()
    b.add_ietf_note_well()
    b.add_attendees_party()
    errors = []
    assert converter._process_chat(b, sample_ietf_session, errors, []) == 2
    assert not errors

    data = check(validator, tmp_path, b)
    chat = next(a for a in data["attachments"] if a["purpose"] == "chat_log")
    assert chat["encoding"] == "json"
    assert chat["mediatype"] == "application/json"
    assert [m["content"] for m in json.loads(chat["body"])] == [
        m.content for m in sample_chat_messages
    ]


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


# ---- url without content_hash: allowed and counted (WG issue #40) ----

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
    return ok, errors, list(validator.url_without_hash)


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


def test_chat_dialog_has_no_meta(sample_ietf_meeting, sample_ietf_session, sample_chat_messages):
    b = VConBuilder()
    b.set_meeting_metadata(sample_ietf_meeting, sample_ietf_session)
    b.add_attendees_party()
    i = b.add_chat_dialog(sample_chat_messages, sample_ietf_session)
    d = b.build().vcon_dict["dialog"][i]
    assert d["type"] == "text" and "meta" not in d


def test_validator_passes_recording_url_without_hash(validator, tmp_path):
    ok, errors, known = _run(validator, tmp_path, _vcon_with())
    assert ok and errors == []
    assert known == [f"dialog/0: {RECORDING_URL_LABEL}"]


@pytest.mark.parametrize("url", [
    "https://youtu.be/abc",
    "https://www.ietf.org/audio/ietf90/room1.mp3",
])
def test_recording_hosts_are_labelled_recording_links(validator, tmp_path, url):
    d = {"type": "recording", "start": "2025-01-01T00:00:00Z", "parties": [0],
         "mediatype": "audio/mpeg", "url": url}
    ok, _, known = _run(validator, tmp_path, _vcon_with(dialog=d))
    assert ok and known == [f"dialog/0: {RECORDING_URL_LABEL}"]


def test_validator_counts_url_without_hash_on_non_recording_url(validator, tmp_path):
    d = {"type": "recording", "start": "2025-01-01T00:00:00Z", "parties": [0],
         "mediatype": "video/mp4", "url": "https://example.com/rec.mp4"}
    ok, errors, known = _run(validator, tmp_path, _vcon_with(dialog=d))
    assert ok and errors == []
    assert known == ["dialog/0: Dialog url without content_hash"]


def test_validator_counts_url_without_hash_on_attachment(validator, tmp_path):
    att = {"purpose": "x", "start": "2025-01-01T00:00:00Z", "party": 0, "session": 0,
           "mediatype": "text/html", "url": "https://www.youtube.com/watch?v=abc"}
    ok, errors, known = _run(validator, tmp_path, _vcon_with(attachment=att))
    assert ok and errors == []
    assert sorted(known) == [
        "attachments/0: Attachment url without content_hash",
        f"dialog/0: {RECORDING_URL_LABEL}",
    ]


@pytest.mark.parametrize("field", ["redacted_from", "amended_from"])
def test_validator_fails_url_without_hash_on_redacted_or_amended_from(
    validator, tmp_path, field
):
    data = _vcon_with()
    data[field] = {"uuid": "018f2c3e-7b1a-8c4d-9e2f-0a1b2c3d4e60",
                   "url": "https://example.com/original.vcon.json"}
    ok, errors, known = _run(validator, tmp_path, data)
    assert not ok
    assert any(field in e and "content_hash" in e for e in errors), errors
    assert len(known) == 1  # only the recording dialog


def test_validator_fails_recording_url_plus_other_error(validator, tmp_path):
    data = _vcon_with()
    data["dialog"][0]["meta"] = {"title": "x"}
    ok, errors, known = _run(validator, tmp_path, data)
    assert not ok and any("meta" in e for e in errors)
    assert len(known) == 1
