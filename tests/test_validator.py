"""Checks added to scripts/validate_vcons.py for core-05 conformance."""

import importlib.util
import json
from pathlib import Path

import pytest

VALIDATOR = Path(__file__).parent.parent / "scripts" / "validate_vcons.py"
NOW = "2025-01-01T00:00:00Z"
WTF_URL = "https://example.com/t.wtf.json"


@pytest.fixture(scope="module")
def validator():
    spec = importlib.util.spec_from_file_location("validate_vcons", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.VConValidator()


def base():
    return {
        "vcon": "0.5.0", "uuid": "018f2c3e-7b1a-8c4d-9e2f-0a1b2c3d4e5f",
        "created_at": NOW,
        "parties": [{"name": "a"}, {"name": "b"}],
        "sessions": [{"start": NOW, "parties": [0, 1], "dialog": [0]}],
        "dialog": [{
            "type": "text", "start": NOW, "parties": [0, 1], "mediatype": "text/plain",
            "body": "hi", "encoding": "none",
        }],
        "attachments": [{
            "purpose": "x", "start": NOW, "party": 0, "session": 0,
            "mediatype": "application/json", "body": "{}", "encoding": "json",
        }],
        "analysis": [{
            "type": "wtf_transcription", "dialog": 0, "vendor": "v",
            "url": WTF_URL, "content_hash": "sha512-abc", "mediatype": "application/json",
        }],
    }


def run(validator, tmp_path, data):
    path = tmp_path / "v.vcon.json"
    path.write_text(json.dumps(data))
    ok, errors, _ = validator.validate_file(path)
    return ok, errors


def mutated(change):
    data = base()
    change(data)
    return data


def test_baseline_is_valid(validator, tmp_path):
    ok, errors = run(validator, tmp_path, base())
    assert ok, errors


class TestAnalysisContent:
    def test_external_transcript_is_accepted(self, validator, tmp_path):
        ok, errors = run(validator, tmp_path, base())
        assert ok and not any("body" in e for e in errors)

    def test_inline_transcript_is_accepted(self, validator, tmp_path):
        def change(d):
            d["analysis"] = [{
                "type": "wtf_transcription", "dialog": 0, "vendor": "v",
                "body": json.dumps({"segments": [{"start": 0, "end": 1, "text": "x"}]}),
                "encoding": "json", "mediatype": "application/json",
            }]
        ok, errors = run(validator, tmp_path, mutated(change))
        assert ok, errors

    def test_neither_body_nor_url_is_an_error(self, validator, tmp_path):
        def change(d):
            d["analysis"] = [{"type": "summary", "dialog": 0, "vendor": "v"}]
        ok, errors = run(validator, tmp_path, mutated(change))
        assert not ok and any("missing both body and url" in e for e in errors)

    def test_url_without_content_hash_is_counted_not_failed(self, validator, tmp_path):
        # core issue #40: a url without content_hash is allowed on Analysis
        def change(d):
            del d["analysis"][0]["content_hash"]
        ok, errors = run(validator, tmp_path, mutated(change))
        assert ok, errors
        assert validator.url_without_hash == ["analysis/0: Analysis url without content_hash"]


class TestUndefinedMeta:
    @pytest.mark.parametrize("where, label", [
        (("parties", 0), "Party 0"),
        (("attachments", 0), "Attachment 0"),
        (("dialog", 0), "Dialog 0"),
    ])
    def test_meta_is_an_error(self, validator, tmp_path, where, label):
        def change(d):
            d[where[0]][where[1]]["meta"] = {"title": "x"}
        ok, errors = run(validator, tmp_path, mutated(change))
        assert not ok
        assert any(e.startswith(label) and "meta" in e for e in errors)


class TestExtensionNames:
    @pytest.mark.parametrize("name", ["role", "meta"])
    def test_non_extension_names_are_errors(self, validator, tmp_path, name):
        ok, errors = run(validator, tmp_path, mutated(lambda d: d.update(extensions=[name])))
        assert not ok and any(repr(name) in e for e in errors)

    def test_role_requires_cc(self, validator, tmp_path):
        def change(d):
            d["parties"][0]["role"] = "chair"
        ok, errors = run(validator, tmp_path, mutated(change))
        assert not ok and any('"CC"' in e for e in errors)

        def with_cc(d):
            change(d)
            d["extensions"] = ["CC"]
        ok, errors = run(validator, tmp_path, mutated(with_cc))
        assert ok, errors


def test_inline_attachment_without_mediatype_is_reported_once(validator, tmp_path):
    def change(d):
        del d["attachments"][0]["mediatype"]
    ok, errors = run(validator, tmp_path, mutated(change))
    assert not ok
    assert len([e for e in errors if "mediatype" in e]) == 1


class TestIndexRanges:
    @pytest.mark.parametrize("change, expected", [
        (lambda d: d["sessions"][0].update(parties=[0, 2]), "Session 0: party index 2"),
        (lambda d: d["sessions"][0].update(dialog=[0, 1]), "Session 0: dialog index 1"),
        (lambda d: d["dialog"][0].update(parties=[0, 1, 2, 3]), "Dialog 0: party index 2"),
        (lambda d: d["attachments"][0].update(party=5), "Attachment 0: party index 5"),
        (lambda d: d["attachments"][0].update(dialog=4), "Attachment 0: dialog index 4"),
        (lambda d: d["attachments"][0].update(session=3), "Attachment 0: session index 3"),
        (lambda d: d["analysis"][0].update(dialog=7), "Analysis 0: dialog index 7"),
        (lambda d: d["analysis"][0].update(sessions=[0, 9]), "Analysis 0: sessions index 9"),
    ])
    def test_out_of_range_is_an_error(self, validator, tmp_path, change, expected):
        ok, errors = run(validator, tmp_path, mutated(change))
        assert not ok
        assert any(expected in e for e in errors), errors

    def test_in_range_indexes_pass(self, validator, tmp_path):
        def change(d):
            d["attachments"][0]["dialog"] = 0
            d["analysis"][0]["sessions"] = [0]
        ok, errors = run(validator, tmp_path, mutated(change))
        assert ok, errors


def test_recording_url_without_hash_is_counted(validator, tmp_path):
    data = base()
    data["dialog"] = [{
        "type": "recording", "start": NOW, "parties": [0, 1], "mediatype": "video/mp4",
        "url": "https://www.youtube.com/watch?v=abc",
    }]
    path = tmp_path / "k.vcon.json"
    path.write_text(json.dumps(data))
    ok, errors, _ = validator.validate_file(path)
    assert ok, errors
    assert len(validator.url_without_hash) == 1


class TestUrlNamesTheFile:
    """An attachment's url must name the file its filename and hash describe."""

    @staticmethod
    def with_attachment(**fields):
        def change(d):
            d["attachments"].append({
                "purpose": "slides", "start": NOW, "party": 0, "session": 0,
                "mediatype": "application/pdf", "content_hash": "sha512-abc", **fields,
            })
        return mutated(change)

    def test_a_matching_last_segment_passes(self, validator, tmp_path):
        data = self.with_attachment(
            url="https://www.ietf.org/proceedings/92/slides/slides-92-supa-2.pdf",
            filename="slides-92-supa-2.pdf",
        )
        ok, errors = run(validator, tmp_path, data)
        assert ok, errors

    def test_the_supa_defect_is_an_error(self, validator, tmp_path):
        data = self.with_attachment(
            url="https://www.ietf.org/proceedings/92/slides/slides-92-supa-2.pdf",
            filename="slides-92-supa-2.pptx",
        )
        ok, errors = run(validator, tmp_path, data)
        assert not ok
        assert any(
            "Attachment 1" in e and "slides-92-supa-2.pdf" in e and "slides-92-supa-2.pptx" in e
            for e in errors
        )

    def test_a_query_string_and_fragment_are_ignored(self, validator, tmp_path):
        data = self.with_attachment(
            url="https://www.ietf.org/proceedings/92/slides/deck.pdf?download=1#page=2",
            filename="deck.pdf",
        )
        ok, errors = run(validator, tmp_path, data)
        assert ok, errors

    def test_percent_escapes_are_undone(self, validator, tmp_path):
        data = self.with_attachment(
            url="https://www.ietf.org/proceedings/92/slides/slides%2092%20supa.pptx",
            filename="slides 92 supa.pptx",
        )
        ok, errors = run(validator, tmp_path, data)
        assert ok, errors

    def test_a_datatracker_document_name_without_an_extension_is_not_compared(
        self, validator, tmp_path
    ):
        data = self.with_attachment(
            url="https://datatracker.ietf.org/meeting/98/materials/materials-98-codec-opus-update",
            filename="materials-98-codec-opus-update-00.patch",
        )
        ok, errors = run(validator, tmp_path, data)
        assert ok, errors

    def test_an_attachment_with_a_url_and_no_filename_is_not_compared(self, validator, tmp_path):
        data = self.with_attachment(url="https://www.ietf.org/proceedings/92/slides/deck.pdf")
        ok, errors = run(validator, tmp_path, data)
        assert ok, errors

    def test_a_different_file_name_with_the_same_stem_is_still_an_error(self, validator, tmp_path):
        data = self.with_attachment(
            url="https://www.ietf.org/proceedings/92/agenda/agenda-92-x-00.txt",
            filename="agenda-92-x-00.md",
        )
        ok, errors = run(validator, tmp_path, data)
        assert not ok and any("url names" in e for e in errors)
