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
- **vCon Standard**: Output follows the [IETF vCon specification](https://datatracker.ietf.org/doc/draft-ietf-vcon-vcon-container/)

## Installation

```bash
# Clone the repository
git clone https://github.com/vcon-dev/ietf2vcon.git
cd ietf2vcon

# Install in development mode
pip install -e ".[dev]"
```

### Dependencies

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

The generated vCon follows the IETF vCon specification with extensions:

```json
{
  "vcon": "0.0.1",
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "created_at": "2024-11-07T15:30:00Z",
  "subject": "IETF 121 - VCON Working Group Session",

  "parties": [
    {"name": "Brian Rosen", "role": "chair", "mailto": "br@brianrosen.net"},
    {"name": "IETF Attendees", "role": "attendee"}
  ],

  "dialog": [
    {
      "type": "video",
      "url": "https://www.youtube.com/watch?v=DfNKgMvbn1o",
      "mimetype": "video/mp4",
      "duration": 3600
    }
  ],

  "attachments": [
    {
      "type": "lawful_basis",
      "body": {
        "lawful_basis": "legitimate_interests",
        "terms_of_service": "https://www.ietf.org/about/note-well/",
        "terms_of_service_name": "IETF Note Well",
        "purpose_grants": [
          {"purpose": "recording", "status": "granted"},
          {"purpose": "transcription", "status": "granted"}
        ]
      },
      "meta": {"spec": "draft-howe-vcon-lawful-basis-00"}
    },
    {"type": "slides", "url": "https://datatracker.ietf.org/..."},
    {"type": "agenda", "url": "..."},
    {"type": "minutes", "url": "..."}
  ],

  "analysis": [
    {
      "type": "wtf_transcription",
      "dialog": 0,
      "vendor": "youtube",
      "spec": "draft-howe-wtf-transcription-00",
      "body": {
        "transcript": {
          "text": "Welcome to the IETF 121 vCon session...",
          "language": "en",
          "duration": 3600.0
        },
        "segments": [
          {"id": 0, "start": 0.0, "end": 5.0, "text": "Welcome to the IETF 121"},
          {"id": 1, "start": 5.0, "end": 9.5, "text": "vCon session."}
        ],
        "metadata": {
          "provider": "youtube",
          "model": "auto-generated",
          "segment_count": 2080
        }
      }
    }
  ]
}
```

## Extensions Used

This tool implements several vCon extensions:

| Extension | Draft | Purpose |
|-----------|-------|---------|
| WTF Transcription | [draft-howe-vcon-wtf-extension](https://datatracker.ietf.org/doc/draft-howe-vcon-wtf-extension/) | Standardized transcript format with segments |
| Lawful Basis | [draft-howe-vcon-lawful-basis](https://datatracker.ietf.org/doc/draft-howe-vcon-consent/) | GDPR-compliant consent/legal basis |

## Data Sources

| Component | Source | URL |
|-----------|--------|-----|
| Meeting Metadata | Datatracker API | https://datatracker.ietf.org/api/v1/ |
| Video Recordings | YouTube | https://www.youtube.com/@ietf |
| Video Recordings | Meetecho | https://meetings.conf.meetecho.com/ |
| Materials | Datatracker | https://datatracker.ietf.org/meeting/{num}/materials/ |
| Chat Logs | Zulip | https://zulip.ietf.org/ |

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
  --transcript-source [auto|youtube|whisper]
                               Transcript source (default: auto)
  --export-srt                 Export transcript as SRT file
  --export-webvtt              Export transcript as WebVTT file
  --whisper-model [tiny|base|small|medium|large]
                               Whisper model size (default: base)
  --no-chat                    Skip Zulip chat logs
  --zulip-email TEXT           Zulip email
  --zulip-api-key TEXT         Zulip API key
  -v, --verbose                Enable verbose output
  --help                       Show this message and exit
```

## License

MIT

## Contributing

Contributions are welcome! Please see the [IETF vCon Working Group](https://datatracker.ietf.org/group/vcon/about/) for more information about the vCon specification.
