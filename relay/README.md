# OpenHAVoice Relay

Current proof-of-concept relay for stock Home Assistant Voice PE firmware.

The relay connects to the Voice PE through the ESPHome Native API using `aioesphomeapi`.

## Proven flow

```text
Voice PE button
  → relay receives VoiceAssistant start
  → relay receives 16 kHz mono PCM audio
  → VAD detects end of speech
  → relay writes WAV
  → relay sends WAV to Whisper STT
  → relay synthesizes fixed response through Orpheus/Jana
  → relay exposes WAV over HTTP
  → relay sends TTS_START/TTS_END events with URL
  → Voice PE downloads and plays the WAV
```

## Files

- `relay.py` — current roundtrip proof
- `.env.example` — local config template
- `requirements.txt` — Python dependencies

## Requirements

- Python 3.11+
- Network reachability to the Voice PE on TCP `6053`
- Voice PE Noise PSK from Home Assistant/ESPHome config
- Local Whisper-compatible STT endpoint
- Local Orpheus/OpenAI-compatible TTS endpoint

## Usage

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python relay.py
```

Then press the Voice PE button and speak.

## Home Assistant interaction

For direct tests, disable the HA ESPHome integration entry for the test Voice PE. Do not delete the device and do not flash firmware.

If the device remains red and does not emit `START`, reboot the Voice PE and retry with HA still disabled.

## Current limitation

The current relay uses a fixed response after STT:

```text
Test erfolgreich. Ich habe dich verstanden.
```

OpenClaw conversation integration is tracked in the issue tracker.
