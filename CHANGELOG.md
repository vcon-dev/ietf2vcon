# Changelog

## 0.3.0

Port to vCon core-05 (syntax "0.5.0"). Requires `vcon>=1.0.0,<2`. Not on the
package index: install from source at the release tag. The version is defined
once, in `src/ietf2vcon/__init__.py`, and the CLI, the package metadata and the
`converter_version` written into `ingress_info` all read it (the CLI used to
report 0.1.0).

### Document structure (core-05)

- One IETF meeting session is one core-05 Session (`sessions[0]`) with start,
  duration and receivers. Chairs are added first, so the organizer is the first
  party and the implied originator; `originator` is never written. With no known
  chair the attendees party is first.
- The recording and the Zulip chat log are Dialogs listed by the Session. Dialog
  `parties` stay the contributors: every party of the vCon.
- Every attachment carries `party` plus `session` (or `dialog` where it
  describes one). The IETF Note Well `lawful_basis` attachment is scoped with
  party plus session. Transcript analysis points at the recording Dialog, or at
  the Session when the vCon has no Dialog.
- Every attachment with an inline `body` carries `mediatype` (`application/json`
  for the JSON bodies; core-05 4.6.6 makes it a MUST for inline content), and so
  do inline analysis entries, including analysis carried over. The chat log
  written as an attachment (chat as dialog switched off) is now a JSON string
  body with `encoding: "json"`.
- `extensions` lists registered names only. The party `role` parameter belongs to
  the contact-center extension, so `CC` is written when any party has a role.
  `role` and `meta` are never written, and carry-over drops them from a previous
  file's list.
- core-05 has no `meta`: no party, dialog or attachment carries one, and a Dialog
  carried over from a 0.4.0 file has no `meta` or `metadata`. The title and
  Datatracker page of each linked material moved into a `materials` list in the
  `meeting_metadata` body (`filename`, `url`, `title`, `datatracker_url`).
- `encoding` appears only on entries that have a `body`, and the `parties`,
  `dialog`, `attachments`, `analysis` and `sessions` arrays are always present.
- Files are written in the published layout: two-space indent, raw UTF-8, one
  trailing newline.
- An external reference's `url`, `filename`, `mediatype` and `content_hash`
  describe the one file whose bytes were read. When the mirror holds a different
  sibling than the Datatracker's `uploaded_filename` (for example `.pptx` where
  the record says `.pdf`), the URL is rebuilt from the name read, or the
  attachment falls back to an unhashed body reference with a warning.
- Session polls (the catch-all `document` material type) are no longer requested
  from a proceedings location that does not exist, and a document the server
  does not have is a warning, not an error. Polls stay body references.
- The `materials/` directory under the output directory is created only when a
  file is written to it.
- The WTF server request is built as a 0.5.0 vCon with a Session.
- Recordings that are only linked (YouTube watch page, ietf.org MP3, Meetecho)
  are Dialogs with `url` and `mediatype` (`video/mp4`, or `audio/mpeg` for MP3).
  They carry no `content_hash` because none can honestly be computed for a
  mutable page or an unfetched, login-gated file, and no Dialog `meta`. This
  departs from core-05 Section 2.4 and the Appendix C `url -> content_hash`
  dependency, pending WG issue
  https://github.com/ietf-wg-vcon/draft-ietf-vcon-vcon-core/issues/40. Bytes that
  are actually fetched are hashed for real (SHA-512).

### Official schedule and session status

- Session start, duration and room come from the meeting's official schedule
  (the meeting record's `schedule`), not from the first assignment found, which
  was often from a draft schedule and gave most sessions a wrong start. A session
  absent from the official schedule has no time, duration or room; a meeting with
  no official schedule has all sessions time unknown (one warning). Assignments,
  timeslots and rooms are read once per meeting in id order and cached for the
  run, replacing four requests per session. A session with two assignments takes
  the earlier start, and the lower assignment id decides on equal starts.
- A session whose current status is not `sched` (cancelled, not meeting,
  rescheduled and so on) has no Session `start` or `duration` even if it holds a
  slot. Status comes from the latest scheduling event, read once per meeting, and
  is recorded as `status` in the `meeting_metadata` body, with null `start_time`,
  `duration_seconds` and `room` for a session that did not meet. A meeting with no
  events is judged by the schedule alone.
- A session with no scheduled start is dated by its meeting's first day (end of
  that day in UTC) when looking up chairs, and the lawful basis `granted_at`
  uses the meeting's first day at 00:00:00 UTC. No `start` is invented.
- Lawful basis `granted_at` is the session start, not the conversion time. Its
  second proof entry (the Datatracker session record "including the bluesheets
  attendance document referenced by this vCon") is kept only for a session that
  met (or has unknown status) and only if the vCon has a `bluesheets`
  attachment; the wording of the entries is unchanged.

### Chairs

- Chairs are resolved as of the session time the way the Datatracker's own
  history works: a group-history snapshot is stamped with when its state began,
  so it is in force until the next snapshot, the newest one until the group's own
  `time`, and from then on the live chair roles apply. Previously only snapshots
  were consulted, so any session after a group's latest edit got the chairs from
  before that edit. Snapshots are read in full by group id (renamed groups
  included), ties take the highest id, and a person listed in several chair rows
  is one chair. Each lookup logs which source it used, and reads are cached for
  the run.
- Chairs are omitted in three cases: before group history exists (or the group
  was created later); ambiguous history, meaning several snapshots share the
  chosen snapshot's `time` (or, for live roles, the group's own `time`) but hold
  different chair people, compared by person so a changed address alone is not
  ambiguous; and no date at all. Ambiguous sessions are counted separately in the
  `convert-all` summary. A failed read never turns into current chairs: it fails
  the session. Current chairs are used only for a call with no date.

### Regeneration in place

- `convert-all` regenerates a group's published session in place: when the
  output directory already holds `ietf<N>_<group>_<id>.vcon.json` files for a
  group, the session ids in those names are converted (each one, if several), so
  filenames and uuids do not change. A published id (or group) the Datatracker no
  longer lists is left untouched with a warning, and the summary counts it. The
  work list is the groups the Datatracker lists plus the groups that already have
  a file; `--existing-only` regenerates only sessions that already have a file,
  creates none, and lists the groups it skipped.
- A group with no previous file gets the first session that met (earliest
  official start, then lowest id; lowest id if none met). Sessions are read in id
  order because the API's own order is not defined, which is why the session at
  position 0 could change between runs.
- Carry-over migrates a previous 0.4.0 file with the reference library, keeps its
  uuid and `created_at` (core-05 4.1.5; `updated_at` is the conversion time),
  and reuses its recording Dialog whenever the new build has none, whether or not
  the previous file has analysis. The carried Dialog lists every party of the new
  vCon and takes the session's official start (and duration, except for a YouTube
  video, which keeps its own length); without an official time the carried values
  stay and a warning lists the recording as unverified. Each carry-over is logged
  at INFO with the prefix `CARRY-OVER`. A previous transcript is kept only when
  the new build produces none.

### Failure handling

- A published file is replaced only by a complete, verified result. Files are
  written atomically (a hidden temporary file, fsync, then `os.replace`;
  leftovers from a killed run are removed at the next start). A previous file that
  cannot be read or migrated, or a new document that would change the uuid or
  `created_at` or drop the recording Dialog or analysis entries, fails the session
  and leaves the file untouched.
- A Datatracker read that fails after the retries (meeting, official schedule,
  statuses, group name, materials list, a chair's person record or address, the
  chair history) fails the session instead of shaping the output, as does a
  material that cannot be fetched because of a timeout, connection error, 429 or
  5xx. 404, 403 and 410 still give the unhashed body reference and are counted by
  status in the summary. A paged list that ends with fewer rows than the server's
  count is an error.
- A meeting-wide read failure (meeting record, official schedule with timeslots
  and rooms, session statuses) stops `convert-all` for the meeting at once: the
  remaining sessions are not attempted, the summary says how many and why. An
  empty or failed session listing is an error, a requested `--rsync-mirror` that
  is missing stops the run, and the exit code is 1 when any session failed.

### Client

- One throttle shared by all requests to the Datatracker and ietf.org, 8 per
  second by default (`--max-rps`, or `IETF2VCON_MAX_RPS`; 0 disables it).
  `Retry-After` is honoured on 429 and 503 (at most five minutes); timeouts and
  5xx are retried five times with jittered exponential backoff, each retry
  logged. Every list is read in id order and de-duplicated. The unused
  `tenacity` dependency is removed.
- `convert-all` summary: failures with reasons, published sessions not found,
  groups skipped by `--existing-only`, ambiguous chairs, materials served from
  the mirror and over HTTP, and materials not available by HTTP status.

### Validator

- `scripts/validate_vcons.py` validates against the vendored core-05 Appendix C
  JSON Schema (`scripts/schemas/appendix-c-json-schema.json`) and checks Session
  wiring, attachment scope and lawful basis scoping. The one error "url without
  content_hash on a recording Dialog whose url is YouTube or an IETF recording"
  is reported separately as a known deviation (count plus a pointer to WG issue
  #40) and does not fail a file. Every other schema error still fails.
- It accepts an analysis entry with `url` and `content_hash` and no `body`, and
  fails an entry with neither, or a `url` without `content_hash`. It also fails:
  `meta` on a party, dialog or attachment; `role` or `meta` in `extensions`; a
  party `role` without `CC`; any party, dialog, session or attachment index out of
  range; and an attachment whose URL's last path segment is a file name different
  from its `filename` (a Datatracker document name with no extension is not
  compared).

### Not emitted

- Events. The Datatracker and Meetecho give no join or drop times.

### Known limitations

To move into the converter in a later version:

- The chair determination record and the confirmation of ambiguous chairs from
  session minutes are done by a separate post-processing tool that ships in the
  dataset repository (`scripts/chair_determination.py` in
  `vcon-dev/ietf-meeting-vcons`), not by the converter. A converter run rebuilds
  `parties` without those chairs and keeps an existing determination record only
  when it builds no new analysis.
- The file type the converter writes into an unhashed document reference is a
  guess (`application/pdf`); the dataset's `scripts/reference_types.py` corrects
  it afterwards.

### Migration

Existing 0.4.0 files are regenerated in place with
`convert-all --existing-only` (or `convert`): uuids, `created_at` and transcripts
are kept (`preserve_uuid` and `preserve_analysis` default on) when the output
directory still holds the previous files. A file that cannot be regenerated
completely is left untouched.
