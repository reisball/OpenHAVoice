# OpenHAVoice

**Run a stock Home Assistant Voice Preview Edition as a local OpenClaw/Zoe voice front-end without Home Assistant as the active voice backend.**

OpenHAVoice connects directly to the Voice PE over the **ESPHome Native API** (`:6053`) using the device's Noise PSK. It subscribes to the voice assistant stream, receives microphone PCM audio, runs local STT/TTS, sends the transcript to OpenClaw, and returns a WAV playback URL to the device.

## Credits / prior art

Huge kudos to [ottocoster/voicepe-standalone](https://github.com/ottocoster/voicepe-standalone), which independently demonstrates the key stock-firmware path: using `aioesphomeapi` to connect directly to the Home Assistant Voice PE, receive API audio from wake-word sessions, run VAD/STT locally, and operate without Home Assistant as the voice backend.

OpenHAVoice builds in the same spirit, with a focus on OpenClaw integration, per-device OpenClaw sessions, local Whisper/Orpheus services, systemd deployment, and future browser-based onboarding.

## Current status

✅ Proven on real hardware with stock Voice PE firmware.

Test device:

- Device: `home-assistant-voice-0abf52`
- IP: `192.168.50.146`
- Firmware/project: `Nabu Casa.Home Assistant Voice PE` `26.4.0`
- ESPHome: `2026.3.2`
- API: ESPHome Native API with Noise encryption
- Home Assistant integration: disabled during direct relay tests

Proven flows:

- Button-triggered voice sessions
- Wake-word-triggered sessions with `Okay Nabu`
- Dedicated OpenClaw session key: `openhavoice:<device-name>`
- Local roundtrip through Whisper STT, OpenClaw Chat Completions, Orpheus/Jana TTS, and Voice PE playback
- Persistent relay connection restores the Voice PE from red/not-ready state when HA is not connected

Example relay evidence:

```text
CONNECTED home-assistant-voice-0abf52. Backend client is active; session='openhavoice:home-assistant-voice-0abf52'
START wake='Okay Nabu' flags=1 ...
TRANSCRIPT 'Wie geht es dir heute? Alles gut bei dir?'
OPENCLAW_REPLY ...
TTS_READY http://192.168.50.30:8765/tts/...
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

## Quick start

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
VOICE_PSK=base64-noise-psk-from-esphome-config

WHISPER_URL=http://192.168.50.51:8000/v1/audio/transcriptions
ORPHEUS_URL=http://192.168.50.52:5005/v1/audio/speech
ORPHEUS_MODEL=orpheus-german-fix-ctx4k
ORPHEUS_VOICE=jana

OPENCLAW_URL=http://127.0.0.1:18789
OPENCLAW_TOKEN=...
# Agent name only, for example default or another configured agent.
OPENCLAW_AGENT=default

# Optional. Empty means: openhavoice:<device-name>
OPENCLAW_SESSION_KEY=
OPENCLAW_MESSAGE_CHANNEL=voice

TTS_HOST=0.0.0.0
TTS_PORT=8765
TTS_POST_PLAYBACK_GRACE_SECONDS=1.0
```

Run:

```bash
python relay.py
```

The relay is intended to stay running as the active backend client. If the Voice PE reboots or drops the TCP connection, the relay reconnects automatically with backoff.

## systemd service

A user service template lives in [`systemd/`](systemd/):

```bash
mkdir -p ~/.config/openhavoice ~/.config/systemd/user
cp relay/.env.example ~/.config/openhavoice/relay.env
cp systemd/openhavoice-relay.service ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now openhavoice-relay.service
journalctl --user -u openhavoice-relay.service -f
```

## Custom wake words

See [`docs/custom-wake-word.md`](docs/custom-wake-word.md) for the current custom wake-word options and constraints.

Short version: OpenHAVoice currently uses the stock Voice PE firmware wake-word session flow. The relay does not detect wake words itself, so a phrase like `Hey Zoe` requires firmware/device support or a future custom-firmware/server-side wake-word experiment.

## Testing against a Voice PE that was already added to HA

For direct relay testing with stock firmware:

1. Disable the Home Assistant ESPHome integration entry for the test Voice PE.
2. Do **not** delete the device.
3. Do **not** flash firmware.
4. Start the OpenHAVoice relay.
5. Wait for the relay to log `CONNECTED ... Backend client is active`.
6. Press the Voice PE button or say `Okay Nabu`.
7. Speak a short sentence and wait for playback.

If the LED stays red or button/wake sessions do not start, reboot the Voice PE and let the relay reconnect.

## Device controls

[`tools/openhavoice-control.py`](tools/openhavoice-control.py) provides safe ESPHome entity inspection/control helpers.

Currently supported safe controls include:

- list entities
- wake sound toggle
- mute toggle
- wake-word sensitivity

Restart is intentionally guarded by an explicit `--allow-restart` flag.

## Roadmap

Current focus areas are tracked in Gitea issues:

- Browser-based Improv BLE provisioning for brand-new Voice PE devices
- LAN/mDNS discovery and pairing flow after Wi-Fi provisioning
- Better VAD/timeout/recovery behavior
- Web UI dashboard and safe controls
- Relay metrics/events
- Optional OpenClaw paired-node integration

## Notes and constraints

- Home Assistant can remain installed, but for direct relay testing its ESPHome connection to the test device should be disabled. Otherwise HA may own the voice assistant session. See [`docs/home-assistant-handoff.md`](docs/home-assistant-handoff.md).
- The physical mute switch must be off.
- Current local STT/TTS latency is usable but not yet optimized for instant assistant feel.
- The relay sends each transcript to OpenClaw Chat Completions and speaks the returned assistant text.
- By default, each Voice PE uses a dedicated persistent session key: `openhavoice:<device-name>`.

## Security

- Never commit the Voice PE Noise PSK.
- Never commit OpenClaw tokens or service credentials.
- Never log Wi-Fi passwords during future BLE provisioning work.
- `.env` and deployed env files must stay local.
