# ietf2vcon

Convert IETF meeting sessions to vCon (Virtual Conversation Container) format.

This tool fetches IETF meeting recordings from YouTube, meeting materials from the Datatracker, generates transcriptions, and captures Zulip chat logs - combining them all into a standardized vCon document.

## Features

- **Video Recordings**: Fetch from YouTube (IETF channel) or Meetecho
- **Meeting Materials**: Slides, agendas, minutes, bluesheets from Datatracker
- **Transcription**: YouTube auto-captions (default) or OpenAI Whisper
- **WTF Format**: Transcripts use the [World Transcription Format](https://datatracker.ietf.org/doc/draft-howe-vcon-wtf-extension/) extension
- **Subtitle Export**: Export transcripts as SRT or WebVTT files
- **Chat Logs**: Capture Zulip chat from IETF sessions
- **Lawful Basis**: Automatically includes IETF Note Well as privacy/consent documentation
- **vCon Standard**: Output is vCon core-05 (syntax `0.5.0`, [draft-ietf-vcon-vcon-core](https://datatracker.ietf.org/doc/draft-ietf-vcon-vcon-core/))
- **Safe regeneration**: A published file is replaced only by a complete, verified result (see Failure handling)

## Installation

Version 0.3.0 is not on the Python package index. Install it from source at the
release tag:

```bash
git clone https://github.com/vcon-dev/ietf2vcon.git
cd ietf2vcon
git checkout v0.3.0
pip install .
```

Requires Python 3.10 or later and the reference library `vcon>=1.0.0,<2`
(vCon core-05). For development, use `pip install -e ".[dev]"`.

### Dependencies

- `vcon` (the reference library) for building and migrating vCons
- `yt-dlp` for YouTube video and caption handling
- `openai-whisper` for local transcription (optional)
- IETF Zulip credentials for chat logs (optional)

## Quick Start

```bash
# Convert IETF 121 vcon working group session
ietf2vcon convert --meeting 121 --group vcon

# View the resulting vCon
ietf2vcon info ./output/ietf121_vcon_33406.vcon.json
```

## Usage

### Basic Conversion

```bash
# Convert a session (includes video URL, materials, and YouTube captions)
ietf2vcon convert --meeting 121 --group vcon

# Specify output location
ietf2vcon convert --meeting 121 --group httpbis --output ./my-vcon.json

# Skip transcript
ietf2vcon convert --meeting 121 --group vcon --no-transcript
```

### Transcript Options

By default, transcripts are fetched from YouTube auto-generated captions.

```bash
# Use YouTube captions (default, fastest)
ietf2vcon convert --meeting 121 --group vcon

# Use Whisper for higher quality (requires video download)
ietf2vcon convert --meeting 121 --group vcon \
    --transcript-source whisper \
    --download-video \
    --whisper-model base

# Export as subtitle files
ietf2vcon convert --meeting 121 --group vcon \
    --export-srt \
    --export-webvtt
```

Available Whisper models: `tiny`, `base`, `small`, `medium`, `large`

### With Zulip Chat Logs

```bash
# Include chat logs (requires Zulip API credentials)
export ZULIP_EMAIL="your-email@example.com"
export ZULIP_API_KEY="your-api-key"

ietf2vcon convert --meeting 121 --group vcon
```

Or pass credentials directly:

```bash
ietf2vcon convert --meeting 121 --group vcon \
    --zulip-email user@example.com \
    --zulip-api-key YOUR_KEY
```

### Video Options

```bash
# Skip video entirely
ietf2vcon convert --meeting 121 --group vcon --no-video

# Download and embed video inline (creates large vCon)
ietf2vcon convert --meeting 121 --group vcon --download-video

# Prefer Meetecho over YouTube
ietf2vcon convert --meeting 121 --group vcon --video-source meetecho
```

### Convert a Whole Meeting

```bash
# Every group the Datatracker lists for IETF 126 (creates files as needed)
ietf2vcon convert-all --meeting 126 --output-dir ./ietf126

# Regenerate the files already in the output directory, in place, and create
# no new ones (filenames and uuids do not change)
ietf2vcon convert-all --meeting 126 --existing-only --no-transcript --no-video \
    --rsync-mirror /path/to/ietf-mirror --output-dir ./ietf126 --parallel 1

# An interim meeting (no mirror: the mirror does not cover interims)
ietf2vcon convert-all --meeting interim-2026-vcon-02 --existing-only \
    --no-transcript --no-video --output-dir ./interim-2026-vcon-02
```

Options:

- `--groups GROUP` (repeatable): only these groups.
- `--existing-only`: only regenerate sessions that already have a published file
  in the output directory; groups the Datatracker lists that have no file are
  skipped and listed in the summary as "no published file". It is an error if
  there is nothing to regenerate.
- `--rsync-mirror DIR`: read materials from a local proceedings mirror before
  falling back to HTTP (populate it with `ietf2vcon sync --meeting N`). If the
  mirror root, or `proceedings/<N>/` for a numbered meeting, is missing, the run
  stops. Interim meetings are exempt from the proceedings check.
- `--parallel N`: conversions in parallel (default 1).
- `--max-rps RATE`: most requests per second to the Datatracker and ietf.org,
  shared by all of them (default 8; also `IETF2VCON_MAX_RPS`; 0 turns the
  throttle off). `convert` accepts it too.
- `--no-transcript`, `--no-video`, `--transcript-source`, `-v`: as for `convert`.

The run ends with a summary: how many sessions succeeded, every failure with its
reason, published sessions not found, groups skipped by `--existing-only`,
sessions whose chairs were omitted as ambiguous, how many materials came from the
mirror and over HTTP, and how many the server said are not available. The exit
code is 1 if any session failed.

### List Available Sessions

```bash
# List all sessions for a meeting
ietf2vcon list-sessions --meeting 121

# Filter by working group
ietf2vcon list-sessions --meeting 121 --group vcon
```

### List Materials

```bash
# List available materials for a session
ietf2vcon list-materials --meeting 121 --group vcon
```

### Inspect a vCon

```bash
# Display information about a vCon file
ietf2vcon info ./output/ietf121_vcon_33406.vcon.json
```

## Output Format

The generated vCon is core-05 (syntax `0.5.0`). One IETF meeting session is one
Session Object (`sessions[0]`); the recording and the chat log are Dialogs that
the Session lists, and every attachment is scoped with a party plus the Session
(or the Dialog it describes). An abridged example:

```json
{
  "vcon": "0.5.0",
  "uuid": "019d3273-65c9-8df3-9dd8-dd37220d739c",
  "created_at": "2026-03-16T03:30:00+00:00",
  "updated_at": "2026-11-14T16:49:13+00:00",
  "subject": "IETF 121 - VCON Working Group Session",
  "extensions": ["CC", "lawful_basis", "wtf_transcription"],

  "sessions": [
    {"start": "2024-11-07T15:30:00+00:00", "duration": 3600, "parties": [0, 1], "dialog": [0]}
  ],

  "parties": [
    {"name": "Chair One", "role": "chair", "mailto": "chair@example.com"},
    {"name": "IETF Attendees", "role": "attendee"}
  ],

  "dialog": [
    {
      "type": "recording",
      "start": "2024-11-07T15:30:00+00:00",
      "duration": 3600,
      "parties": [0, 1],
      "mediatype": "video/mp4",
      "url": "https://www.youtube.com/watch?v=DfNKgMvbn1o"
    }
  ],

  "attachments": [
    {
      "purpose": "meeting_metadata",
      "start": "2026-11-14T16:49:07+00:00",
      "party": 0,
      "session": 0,
      "mediatype": "application/json",
      "encoding": "json",
      "body": "{\"working_group\": \"vcon\", \"status\": \"sched\", \"materials\": [{\"filename\": \"slides-121-vcon-chairs-00.pdf\", \"url\": \"https://www.ietf.org/proceedings/121/slides/slides-121-vcon-chairs-00.pdf\", \"title\": \"Chair slides\"}]}"
    },
    {
      "purpose": "slides",
      "start": "2026-11-14T16:49:07+00:00",
      "party": 0,
      "session": 0,
      "mediatype": "application/pdf",
      "filename": "slides-121-vcon-chairs-00.pdf",
      "url": "https://www.ietf.org/proceedings/121/slides/slides-121-vcon-chairs-00.pdf",
      "content_hash": "sha512-..."
    }
  ],

  "analysis": [
    {
      "type": "wtf_transcription",
      "dialog": 0,
      "vendor": "youtube",
      "mediatype": "application/json",
      "encoding": "json",
      "body": "{\"transcript\": {...}, \"segments\": [...], \"metadata\": {...}}"
    }
  ]
}
```

The `parties`, `dialog`, `attachments`, `analysis` and `sessions` arrays are
always present, and `encoding` appears only on entries that have a `body`.

## Extensions Used

This tool implements several vCon extensions:

| Extension | Draft | Purpose |
|-----------|-------|---------|
| WTF Transcription | [draft-howe-vcon-wtf-extension](https://datatracker.ietf.org/doc/draft-howe-vcon-wtf-extension/) | Standardized transcript format with segments |
| Lawful Basis | [draft-howe-vcon-lawful-basis](https://datatracker.ietf.org/doc/draft-howe-vcon-consent/) | GDPR-compliant consent/legal basis |
| Contact Center (`CC`) | [draft-ietf-vcon-cc-extension](https://datatracker.ietf.org/doc/draft-ietf-vcon-cc-extension/) | Defines the party `role` parameter (chair, attendee) |

`extensions` lists registered extension names only. `CC` is written when any
party has a `role`, `lawful_basis` and `wtf_transcription` when those are in
use. Nothing else is written there.

### Inline content and materials

- Every attachment with an inline `body` carries a `mediatype`
  (`application/json` for the JSON bodies this tool writes), as core-05
  Section 4.6.6 requires. Inline analysis entries carry one too.
- core-05 has no `meta` parameter, so no party, dialog or attachment has one.
  The title and Datatracker page of each linked material are listed in the
  `materials` member of the `meeting_metadata` attachment body, one entry per
  material in attachment order, keyed by `filename`. The entry also carries the
  attachment `url`, because one filename can be published at two paths (an
  agenda and minutes both named `ntp.txt`). A missing value is left out.

### Official schedule and session status

- Session start, duration and room come from the meeting's official schedule
  (the `schedule` field of the meeting record), never from another schedule of
  the meeting. A session that is not in the official schedule has none of the
  three. If a meeting genuinely has no official schedule, every session of it is
  time unknown and one warning is logged; if the schedule cannot be read the
  session fails (see Failure handling). The schedule's assignments, the
  meeting's timeslots and its rooms are read once per meeting, in id order, and
  cached for the run. Timeslot times are UTC (a value without an offset is
  converted from the meeting's time zone). A session with two assignments takes
  the earlier start; if the starts are equal, the assignment with the lower id
  decides the duration. The Datatracker's agenda lists both assignments and
  does not prefer one.
- A session that did not meet has no start. The session record's `status` is
  always empty, so the status is taken from the latest scheduling event of each
  session (newest time, highest id on a tie), read once per meeting. Only
  `sched` counts as held. A session with any other status (`canceled`,
  `canceledpa`, `notmeet`, `resched`, `deleted`, `disappr`, `apprw`, `schedw`,
  `appr`, `scheda`) is treated like one missing from the official schedule: no
  Session `start` or `duration`, no room, lawful basis `granted_at` and the chair
  lookup use the meeting's first day, and a reused recording keeps its old
  values with an "unverified" warning. The `meeting_metadata` body records
  `"status"` (left out when unknown) with null `start_time`, `duration_seconds`
  and `room` for a session that did not meet. A meeting with no scheduling
  events at all is judged by the schedule alone; events that cannot be read fail
  the session.
- Each lawful basis grant has `granted_at` set to the session start, the same
  value as the Session `start`; with no scheduled time, the meeting's first day
  at 00:00:00 UTC. The lawful basis lists its second proof entry (the
  Datatracker session record "including the bluesheets attendance document
  referenced by this vCon") only when the session's status is `sched` or
  unknown and the file actually has a `bluesheets` attachment; otherwise only the
  Note Well entry.

### Chairs

- Chairs are the people in the role at the session time (or, for a session with
  no scheduled time, the end of the meeting's first day, UTC). The Datatracker
  writes a group-history snapshot as a copy of the state an edit is about to
  replace, stamped with the time that state began. So a snapshot is in force
  until the next one, the newest snapshot until the group's own `time` (its
  latest edit), and from that time on the live chair roles are the truth.
  History is read in full by group id, so a renamed group is found. One person in
  several chair rows is one chair, with the lowest role id's email. Lookups are
  cached for the run, per group.
- Chairs are omitted, and the attendees party is then first, in three situations:
  1. **Before group history exists:** the session is earlier than any recorded
     history, or the group was created later.
  2. **Ambiguous history:** several snapshots share one `time` but hold
     different chair people (older tools wrote such snapshots without moving the
     group's `time`), so the time says nothing about when those people served.
     Ambiguity is judged on people, not addresses, and is warned and counted
     separately by `convert-all`.
  3. **No date:** a session with neither a start time nor a meeting date has
     nothing to date the roles against, so none are asserted.
- A chair lookup that fails is not one of these cases: it fails the session (see
  Failure handling). Current chairs are used only for a call with no date at
  all, never for a dated session.

### Which session is converted, and regeneration in place

- If the output directory already holds a file for the meeting and group
  (`ietf<N>_<group>_<id>.vcon.json`), `convert-all` regenerates the session whose
  id is in that filename, keeping the filename and uuid; a group with several
  such files has each regenerated. If a published session id (or its group) is no
  longer in the Datatracker's list, nothing is written, a warning names the file,
  and the summary counts it as "published session not found".
- The work list is the groups the Datatracker lists for the meeting together
  with the groups that already have a published file. For a group with no
  previous file, the session is chosen by rule: sessions that met first (status
  `sched`, or unknown), earliest official start, then lowest integer id; if none
  met, the lowest id. The Datatracker's own order is not defined, so sessions are
  read and kept in ascending id.
- When the uuid is carried over, `created_at` is carried over too (core-05
  4.1.5) and `updated_at` is the conversion time. A reused recording Dialog lists
  every party of the new vCon in its `parties`, drops any `meta` or `metadata`,
  and takes the session's official `start` (and `duration`, except for a YouTube
  video, which keeps its own length); without an official time the carried values
  are kept and a warning lists the recording as unverified. Reuse is logged at
  INFO with the prefix `CARRY-OVER`. A previous transcript is kept only when
  the new build produces none.
- Output is written in the published layout: two-space indent, raw UTF-8 and one
  trailing newline.

### Failure handling

A published file is replaced only by a complete, verified result; anything else
is an error for that session and leaves the file untouched, so the run reports it
and a later run can redo it.

- Files are written to a hidden temporary file and moved into place, so a killed
  run never leaves a truncated vCon (leftovers are removed at the start of the
  next run).
- A previous file that cannot be read or migrated, or a new document that would
  change its uuid or `created_at`, lose its recording Dialog or have fewer
  analysis entries, is refused.
- A Datatracker read that still fails after the retries (the meeting, the
  official schedule, session statuses, a group name, the materials list, a
  chair's person record or address, the chair history) fails the session, and so
  does a material that cannot be fetched because of a timeout, a refused
  connection, 429 or a 5xx. A material the server says is not there (404, 403,
  410) is still kept as an unhashed body reference and counted by status in the
  summary, because a 403 can mean the client is being blocked. A paged list that
  ends with fewer rows than the server's count is an error.
- A read every session of the meeting depends on (the meeting record, the
  official schedule with its timeslots and rooms, the session statuses) that
  fails stops the run at once: the remaining sessions are not attempted, the
  summary says how many and why, and their files are untouched. An empty or
  failed session listing is an error. `convert-all` exits non-zero if any session
  failed and lists each reason.

### Client behaviour

Requests are spaced by one throttle shared by everything that talks to the
Datatracker or ietf.org: 8 per second by default, changed with `--max-rps` or the
`IETF2VCON_MAX_RPS` environment variable (0 turns it off). A 429 or 503 waits for
`Retry-After` (at most five minutes); timeouts and 5xx are retried up to five
attempts with growing, jittered waits (about 5, 10, 20 and 40 seconds), each
retry logged as a warning; other 4xx answers are final.

### External references

An externally referenced attachment's `url`, `filename`, `mediatype` and
`content_hash` describe one file, the one whose bytes were read. The
Datatracker's name for the upload can differ from the file found in the mirror
(the record says `.pdf`, the mirror holds only the `.pptx`); the URL is then
built from the name actually read, in the same proceedings directory, and if no
URL can be formed the attachment is an unhashed body reference with a warning.
`scripts/validate_vcons.py` reports an attachment whose URL's last path segment
is a file name different from its `filename` (a Datatracker document name with
no extension is not compared).

## Data Sources

| Component | Source | URL |
|-----------|--------|-----|
| Meeting Metadata | Datatracker API | https://datatracker.ietf.org/api/v1/ |
| Video Recordings | YouTube | https://www.youtube.com/@ietf |
| Video Recordings | Meetecho | https://meetings.conf.meetecho.com/ |
| Materials | Datatracker | https://datatracker.ietf.org/meeting/{num}/materials/ |
| Chat Logs | Zulip | https://zulip.ietf.org/ |


## Recording links carry no content_hash (core issue #40)

Linked recordings (YouTube watch pages, ietf.org audio, Meetecho) are Dialogs
with `url` and `mediatype` and no `content_hash`. None can honestly be
computed: a watch page is mutable HTML and the audio files are login-gated or
not fetched. No hash is ever invented; recording bytes that are actually
fetched get a real SHA-512. This follows the WG direction on
https://github.com/ietf-wg-vcon/draft-ietf-vcon-vcon-core/issues/40: a `url`
without `content_hash` is allowed on a Dialog, Attachment or Analysis when the
producer cannot get stable bytes. The core-05 text (Section 2.4 and the
Appendix C `url -> content_hash` dependency) still says MUST until the WG
revises it. The Python `vcon` library allows it from 1.0.6.

`scripts/validate_vcons.py` validates against the vendored Appendix C schema.
A `url` without `content_hash` on a Dialog, Attachment or Analysis is counted
and reported (recording links are labelled as such), not failed. It still
fails on `redacted_from` and `amended_from`, and any other schema error still
fails. The validator does not check the `content_hash` format. It also fails a
file with a `meta` key on a party, dialog or attachment, with `role` or `meta`
in `extensions`, with a party `role` and no `CC` in `extensions`, or with a
party, dialog, session or attachment index out of range. An analysis entry
needs a `body` or a `url`; one with neither fails.

## Known limitations

Two things are done outside the converter today and are meant to move into it in
a later version:

- **Chair determination.** The chair determination record and the confirmation
  of ambiguous chairs from session minutes are done by a separate
  post-processing tool that ships in the dataset repository
  (`scripts/chair_determination.py` in `vcon-dev/ietf-meeting-vcons`), not by
  the converter. A converter run rebuilds `parties` without those chairs, and
  keeps an existing determination record only when it builds no new analysis.
- **Type of unhashed document references.** The file type the converter writes
  into an unhashed document reference (a body reference to a document that could
  not be fetched or whose bytes cannot be hashed) is a guess, `application/pdf`.
  The dataset's `scripts/reference_types.py` corrects it afterwards.

## Zulip Chat Integration

To include chat logs, you need IETF Zulip credentials:

1. Log in to https://zulip.ietf.org/ using your Datatracker credentials
2. Go to Settings → Account & Privacy → API Key
3. Generate an API key
4. Use the key with `--zulip-api-key` or set `ZULIP_API_KEY` environment variable

## Programmatic Usage

```python
from ietf2vcon import IETFSessionConverter
from ietf2vcon.converter import ConversionOptions

# Configure options
options = ConversionOptions(
    include_video=True,
    video_source="youtube",
    include_materials=True,
    include_transcript=True,
    transcription_source="auto",  # youtube first, then whisper
    export_srt=True,
    include_chat=False,
)

# Convert
converter = IETFSessionConverter(options)
result = converter.convert_session(
    meeting_number=121,
    group_acronym="vcon",
)

# Access the vCon
vcon = result.vcon
print(f"Created vCon with {len(vcon.dialog)} dialogs")
print(f"Transcript segments: {result.has_transcript}")

# Save to file
output_path = converter.save_vcon(result)
print(f"Saved to: {output_path}")
```

### Using the VConBuilder Directly

```python
from ietf2vcon.vcon_builder import VConBuilder
from ietf2vcon.models import IETFMeeting, IETFSession

# Build a vCon manually
builder = VConBuilder()
builder.set_subject("My Custom vCon")
builder.add_party(name="Speaker", email="speaker@example.com", role="presenter")
builder.add_ietf_note_well()  # Add IETF Note Well as lawful basis
builder.add_ingress_info(source="custom-tool")

vcon = builder.build()
print(vcon.to_json())
```

## Testing

```bash
# Install development dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run with coverage
pytest --cov=ietf2vcon

# Run only unit tests (fast)
pytest tests/test_models.py tests/test_transcription.py

# Skip slow integration tests
pytest -m "not slow"
```

## CLI Reference

```
ietf2vcon convert [OPTIONS]

Options:
  -m, --meeting INTEGER        IETF meeting number (required)
  -g, --group TEXT             Working group acronym (required)
  -s, --session INTEGER        Session index if multiple (default: 0)
  -o, --output PATH            Output file path
  --output-dir PATH            Output directory (default: ./output)
  --video-source [youtube|meetecho|both]
                               Video source preference (default: youtube)
  --download-video             Download video and embed inline
  --no-video                   Skip video recording
  --no-materials               Skip meeting materials
  --inline-materials           Download and embed materials inline
  --no-transcript              Skip transcript
  --transcript-source [auto|youtube|meetecho|mlx-whisper|wtf-server|whisper]
                               Transcript source (default: auto)
  --export-srt                 Export transcript as SRT file
  --export-webvtt              Export transcript as WebVTT file
  --whisper-model [tiny|base|small|medium|large]
                               Whisper model size (default: base)
  --no-chat                    Skip Zulip chat logs
  --zulip-email TEXT           Zulip email
  --zulip-api-key TEXT         Zulip API key
  --rsync-mirror PATH          Local rsync mirror root (checked before HTTP)
  --max-rps FLOAT              Most requests per second (default 8; 0 = off)
  -v, --verbose                Enable verbose output
  --help                       Show this message and exit

ietf2vcon convert-all [OPTIONS]

Options:
  -m, --meeting TEXT           IETF meeting number, or interim name (required)
  -o, --output-dir PATH        Output directory
  --groups TEXT                Only these groups (repeatable)
  --existing-only              Regenerate only sessions that already have a file
  --rsync-mirror PATH          Local rsync mirror root (must exist when given)
  --parallel INTEGER           Conversions in parallel (default 1)
  --max-rps FLOAT              Most requests per second (default 8; 0 = off)
  --no-transcript              Skip transcript generation
  --no-video                   Skip video references
  --transcript-source [auto|youtube|meetecho|mlx-whisper|wtf-server|whisper]
  -v, --verbose                Enable verbose output

ietf2vcon sync --meeting N [--mirror-dir PATH] [--dry-run]
ietf2vcon list-sessions --meeting N [--group G]
ietf2vcon list-materials --meeting N --group G
ietf2vcon info FILE
```

## License

MIT

## Contributing

Contributions are welcome! Please see the [IETF vCon Working Group](https://datatracker.ietf.org/group/vcon/about/) for more information about the vCon specification.
