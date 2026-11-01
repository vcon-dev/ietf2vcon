# Changelog

## 0.3.0

Port to vCon core-05 (syntax "0.5.0"). Requires `vcon>=1.0.0,<2`.

### Changed

- One IETF meeting session is one core-05 Session (`sessions[0]`) with
  start, duration and receivers. Chairs are added first, so the organizer is
  the first party and the implied originator; `originator` is never written.
  With no known chair the attendees party is first.
- The recording and the Zulip chat log are Dialogs listed by the Session.
  Dialog `parties` stay the contributors.
- Every attachment carries `party` plus `session` (or `dialog` where it
  describes one). The IETF Note Well `lawful_basis` attachment is scoped with
  party plus session.
- Transcript analysis points at the recording Dialog, or at the Session when
  the vCon has no Dialog.
- Recordings that are only linked (YouTube watch page, ietf.org MP3) are an
  inline `text/uri-list` link with the real media type in
  `meta.recording_mediatype`. core-05 allows `url` only with a `content_hash`,
  and no honest hash exists for a page or an unfetched file.
- Regeneration carry-over migrates a previous 0.4.0 file with the reference
  library before reusing its dialog and analysis, and lists carried dialog in
  the Session.
- The WTF server request is built as a 0.5.0 vCon with a Session.
- `scripts/validate_vcons.py` validates against the vendored core-05
  Appendix C JSON Schema (`scripts/schemas/appendix-c-json-schema.json`) and
  checks Session wiring, attachment scope and lawful basis scoping.

### Not emitted

- Events. The Datatracker and Meetecho give no join or drop times.

### Migration

Existing 0.4.0 files are not rewritten in place. Rebuild by reconverting;
Existing uuids and transcripts are kept (preserve_uuid and preserve_analysis default on) when the output directory still holds the previous files.
