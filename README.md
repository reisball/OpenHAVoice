# OpenHAVoice

**Home Assistant Voice PE → OpenClaw voice relay over the ESPHome Native API.**

OpenHAVoice turns a stock Home Assistant Voice Preview Edition into a local voice front-end for OpenClaw/Zoe without using Home Assistant as the active voice backend.

Stock Voice PE firmware exposes voice sessions through the **ESPHome Native API** on port `6053`. The relay connects with the device's Noise PSK, subscribes to the voice assistant stream, receives microphone PCM audio, runs local STT/TTS, and sends a playback URL back to the device.

## Current status

✅ Proven on real hardware with stock Voice PE firmware.

Test device:

- Device: `home-assistant-voice-0abf52`
- Firmware/project: `Nabu Casa.Home Assistant Voice PE` `26.4.0`
- ESPHome: `2026.3.2`
- API: ESPHome Native API with Noise encryption
- Home Assistant integration: temporarily disabled during direct relay tests

Proven roundtrip:

```text
Voice PE button
  → ESPHome Native API voice assistant session
  → PCM audio streamed to relay
  → local Whisper STT
  → Orpheus/Jana TTS
  → Voice PE fetches WAV from relay
  → playback on the Voice PE speaker
```

Confirmed transcript from the successful roundtrip:

```text
Hallo Zoe, das ist ein weiterer Test. 1, 2, 3.
```

Confirmed playback request from the Voice PE:

```text
SERVE_TTS remote=192.168.50.146
```

## Architecture

```text
┌────────────────────────────┐
│ Home Assistant Voice PE    │
│ stock ESPHome firmware     │
│ button / wake word / mic   │
└─────────────┬──────────────┘
              │ ESPHome Native API :6053
              │ VoiceAssistantRequest/Event/Audio
              │ Noise encrypted
              ▼
┌────────────────────────────┐
│ OpenHAVoice relay          │
│ aioesphomeapi client       │
│ VAD + HTTP WAV server      │
└───────┬──────────────┬─────┘
        │              │
        ▼              ▼
┌──────────────┐  ┌──────────────┐
│ Whisper STT  │  │ Orpheus TTS  │
│ local HTTP   │  │ Jana voice   │
└──────┬───────┘  └──────┬───────┘
       │                 │
       └────────┬────────┘
                ▼
         ┌────────────┐
         │ OpenClaw   │
         │ chat agent │
         └────────────┘
```

## Running the proof-of-concept

```bash
cd relay
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```bash
VOICE_HOST=192.168.50.x
VOICE_PSK=base64-noise-psk-from-ha-esphome-config
WHISPER_URL=http://192.168.50.51:8000/v1/audio/transcriptions
ORPHEUS_URL=http://192.168.50.52:5005/v1/audio/speech
TTS_PORT=8765
OPENCLAW_URL=http://127.0.0.1:18789
OPENCLAW_TOKEN=...
# optional; empty means openhavoice:<device-name>
OPENCLAW_SESSION_KEY=
```

Then run:

```bash
python relay.py
```

The relay is intended to stay running as the active backend client. If the Voice PE reboots or drops the TCP connection, the relay reconnects automatically with backoff.

For current tests:

1. Disable the Home Assistant ESPHome integration entry for the test Voice PE.
2. Do **not** delete the device.
3. Do **not** flash firmware.
4. Start the relay.
5. Press the Voice PE button.
6. Speak a short sentence.
7. Wait for playback.

## Notes and constraints

- Home Assistant can remain installed, but for direct relay testing its ESPHome connection to the test device must be disabled. Otherwise HA appears to own the voice assistant session.
- Normal ESPHome entity reads can work while HA exists, but voice assistant audio did not reach the relay until HA was disabled for the device.
- A device reboot may be needed after toggling HA integration state if the LED stays red and button sessions do not start; the relay should then reconnect automatically.
- The physical mute switch must be off.
- The relay currently uses button-triggered sessions. Wake word behavior still needs explicit testing.
- The relay sends each transcript to OpenClaw Chat Completions and speaks the returned assistant text.
- By default, each Voice PE uses a dedicated persistent session key: `openhavoice:<device-name>`.

## Security

- Never commit the Voice PE Noise PSK.
- Never commit OpenClaw tokens or service credentials.
- `.env` is ignored and should stay local.
