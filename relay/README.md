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
  → relay sends transcript to OpenClaw Chat Completions
  → relay synthesizes OpenClaw response through Orpheus/Jana
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

# Generate default .env
python -m relay.cli generate

# Edit .env with your settings
vim .env

# Validate
python -m relay.cli validate

# Start the relay
python -m relay.relay
```

### CLI Configuration

```bash
python -m relay.cli show              # Display current config (secrets redacted)
python -m relay.cli show --reveal     # Show secrets
python -m relay.cli show --json       # JSON output
python -m relay.cli set KEY VALUE     # Update a single field
python -m relay.cli validate          # Check config validity
python -m relay.cli generate          # Generate .env.example
```

### Web UI

When the relay is running, open `http://<host>:8765/` for the configuration dashboard.

**API endpoints:**
- `GET /config` — JSON config (secrets redacted)
- `GET /config?reveal=1` — JSON config with secrets
- `PUT /config` — Update fields (JSON body)
- `POST /config/validate` — Validate without saving
- `POST /config/reload` — Reload from .env file

The process stays connected as the active Voice PE backend and reconnects automatically if the device reboots or the TCP connection drops. Config saves or reloads that change Voice PE connection details automatically restart the in-process device connection loops; the manual restart button is only a fallback for full service restarts.

Then press the Voice PE button and speak.

## Configuration keys

`python -m relay.cli generate` writes a template that follows the `RelayConfig` schema. The same keys can be edited through the Web UI.

| Key | Purpose |
| --- | --- |
| `VOICE_HOST` | Single Voice PE host/IP fallback when `VOICE_DEVICES` is empty. |
| `VOICE_PSK` | Noise PSK for the single Voice PE fallback. Secret. |
| `VOICE_PASSWORD` | Optional ESPHome API password for the single-device fallback. Secret. |
| `VOICE_DEVICES` | Optional JSON list of devices: `name`, `host`, `psk`, `password`, `enabled`. Secret. |
| `WHISPER_URL` / `LANGUAGE` | Whisper-compatible STT endpoint and language hint. |
| `ORPHEUS_URL` / `ORPHEUS_MODEL` / `ORPHEUS_VOICE` | TTS endpoint, model, and voice. |
| `TTS_HOST` / `TTS_PORT` | HTTP bind address for the UI and WAV playback URLs. |
| `TTS_POST_PLAYBACK_GRACE_SECONDS` | Grace period before ending the Voice PE assist run after TTS URL handoff. |
| `OPENCLAW_URL` / `OPENCLAW_TOKEN` | OpenClaw gateway/chat endpoint and optional bearer token. |
| `OPENCLAW_AGENT` | OpenClaw agent name; sent to Chat Completions as `openclaw/<agent>`. |
| `OPENCLAW_SESSION_KEY` | Optional fixed session key; empty derives `openhavoice:<device-name>`. |
| `OPENCLAW_MESSAGE_CHANNEL` | Message channel header sent to OpenClaw, usually `voice`. |
| `OPENCLAW_VOICE_SYSTEM_PROMPT` | Voice-specific system prompt for concise spoken replies. |
| `MIN_SPEECH_MS` / `END_SILENCE_MS` / `MAX_CAPTURE_SECONDS` | Core capture/VAD timing. |
| `VAD_AGGRESSIVENESS` | WebRTC VAD aggressiveness, 0-3. |
| `RMS_SILENCE_THRESHOLD` / `RMS_END_SILENCE_MS` | Loudness-based silence guard. |
| `RECONNECT_INITIAL_SECONDS` / `RECONNECT_MAX_SECONDS` | Voice PE reconnect backoff range. |

The config Web UI/API intentionally has no built-in `CONFIG_ADMIN_TOKEN`; protect exposure through bind address, firewall, or reverse-proxy controls.

## Home Assistant interaction

For direct tests, disable the HA ESPHome integration entry for the test Voice PE. Do not delete the device and do not flash firmware.

If the device remains red and does not emit `START`, make sure this relay is running and connected. If it still stays red, reboot the Voice PE and retry with HA still disabled.

## OpenClaw integration

The relay uses OpenClaw's OpenAI-compatible Chat Completions endpoint. Configure `OPENCLAW_URL`, `OPENCLAW_TOKEN`, and `OPENCLAW_AGENT` in `.env`. Enter only the agent name, for example `default` or another configured agent.

By default, `OPENCLAW_SESSION_KEY` should be left empty. The relay then derives a dedicated persistent session key from the Voice PE device name:

```text
openhavoice:<device-name>
```

Set `OPENCLAW_SESSION_KEY` only when you intentionally want to override that default.
