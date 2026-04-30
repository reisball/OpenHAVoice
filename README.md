# OpenHAVoice

**Home Assistant Voice PE → OpenClaw direct integration — no Home Assistant required.**

A voice satellite that streams audio directly to an OpenClaw-powered AI agent through a lightweight relay server.

## Architecture

```
┌─────────────────────┐      Wyoming TCP       ┌──────────────────┐      HTTP       ┌──────────────────┐
│   Voice PE HW       │ ──────────────────────▶ │   Relay Server   │ ──────────────▶ │  OpenClaw Gateway │
│   (ESP32-S3/XMOS)   │ ◀────────────────────── │   (Python)       │ ◀────────────── │  (Friday et al.)  │
└─────────────────────┘      PCM audio flow      └──────────────────┘                 └──────────────────┘
```

**Flow:**
1. Wake word detected on-device (microWakeWord, no cloud)
2. Raw PCM audio streamed to Relay via [Wyoming protocol](https://github.com/rhasspy/wyoming)
3. Relay transcribes audio (Whisper API / local)
4. Transcript sent to OpenClaw Gateway (`POST /v1/chat/completions`)
5. Agent response synthesized to speech (ElevenLabs / Piper)
6. Audio streamed back to Voice PE for playback

## Components

| Component | Location | Description |
|-----------|----------|-------------|
| **Voice PE firmware** | `firmware/` | ESPHome YAML — stock `voice_assistant` with Wyoming target override |
| **Relay server** | `relay/` | Python Wyoming ↔ OpenClaw bridge |
| **Docs** | `docs/` | Setup guide, hardware reference, pairing flow |

## Why Wyoming?

- Stock ESPHome `voice_assistant` speaks Wyoming natively — no custom C++ component needed
- Simple protocol: `[4-byte length][JSON header]` then raw PCM frames
- Mature, tested in Home Assistant ecosystem

## Quick Start

```bash
# 1. Clone
git clone http://192.168.50.70:3000/openclaw-org/OpenHAVoice.git
cd OpenHAVoice

# 2. Relay
cd relay
pip install -r requirements.txt
python relay.py --openclaw http://192.168.50.186:18789

# 3. Flash Voice PE firmware
# See firmware/README.md
```

## Status

🚧 **Early development** — relay prototype in progress.
