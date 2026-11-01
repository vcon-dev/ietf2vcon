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
- Recordings that are only linked (YouTube watch page, ietf.org MP3,
  Meetecho) are Dialogs with `url` and `mediatype` (`video/mp4`, or
  `audio/mpeg` for MP3), as before the port. They carry no `content_hash`
  because none can honestly be computed for a mutable page or an unfetched,
  login-gated file, and no Dialog `meta` (core-05 defines none). This departs
  from core-05 Section 2.4 and the Appendix C `url -> content_hash`
  dependency, pending WG issue
  https://github.com/ietf-wg-vcon/draft-ietf-vcon-vcon-core/issues/40.
  Bytes that are actually fetched are hashed for real (SHA-512).
- Regeneration carry-over migrates a previous 0.4.0 file with the reference
  library before reusing its dialog and analysis, and lists carried dialog in
  the Session.
- The WTF server request is built as a 0.5.0 vCon with a Session.
- `scripts/validate_vcons.py` validates against the vendored core-05
  Appendix C JSON Schema (`scripts/schemas/appendix-c-json-schema.json`) and
  checks Session wiring, attachment scope and lawful basis scoping. The one
  error "url without content_hash on a recording Dialog whose url is YouTube
  or an IETF recording" is reported separately as a known deviation (count
  plus a pointer to WG issue #40) and does not fail a file. Every other
  schema error, including url without content_hash on an attachment or on any
  other URL, still fails.

### Not emitted

- Events. The Datatracker and Meetecho give no join or drop times.

### Migration

Existing 0.4.0 files are not rewritten in place. Rebuild by reconverting;
Existing uuids and transcripts are kept (preserve_uuid and preserve_analysis default on) when the output directory still holds the previous files.
