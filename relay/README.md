# OpenHAVoice Relay — Implementation Plan

> **Goal:** Python relay server that bridges Voice PE (Wyoming TCP) ↔ OpenClaw Gateway (HTTP)

**Architecture:** Single-file async Python server using only stdlib + `openai` SDK + `elevenlabs` SDK. Listens on TCP for Wyoming frames, buffers audio, transcribes via Whisper, queries OpenClaw, synthesizes via ElevenLabs, streams PCM back.

**Tech Stack:** Python 3.11+, `asyncio` (stdlib), `wave` (stdlib), `openai`, `elevenlabs`

---

## Wyoming Protocol (relevant subset)

```
Voice PE → Relay:
  {"type": "describe", "data": {"format": "pcm", "rate": 16000, ...}}
  {"type": "audio", "data": {"rate": 16000, "width": 2, "channels": 1}}
  [raw PCM bytes]
  ...
  {"type": "audio-stop", "data": {}}

Relay → Voice PE:
  {"type": "transcript", "data": {"text": "..."}}
  {"type": "synthesize", "data": {"text": "..."}}
  [raw PCM bytes as Wyoming audio events]
```

Frame format: `[4-byte big-endian length][UTF-8 JSON]` — audio data MAY follow after JSON in same TCP segment.

---

### Task 1: Project scaffold

**Files:**
- Create: `relay/requirements.txt`
- Create: `relay/.env.example`
- Create: `relay/relay.py` (skeleton)

```txt
# requirements.txt
openai>=1.0
elevenlabs>=1.0
```

```bash
# .env.example
OPENAI_API_KEY=sk-...
ELEVENLABS_API_KEY=...
OPENCLAW_URL=http://192.168.50.186:18789
OPENCLAW_TOKEN=2b89c8...
```

`relay.py` — imports + argparse scaffold:

```python
#!/usr/bin/env python3
"""OpenHAVoice Relay — Wyoming TCP ↔ OpenClaw Gateway bridge."""

import argparse
import asyncio
import os
import struct
import wave
import io
import json
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("relay")

def main():
    parser = argparse.ArgumentParser(description="OpenHAVoice Relay")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=10200)
    parser.add_argument("--openclaw-url", default=os.environ.get("OPENCLAW_URL", "http://192.168.50.186:18789"))
    parser.add_argument("--openclaw-token", default=os.environ.get("OPENCLAW_TOKEN", ""))
    parser.add_argument("--stt-model", default="whisper-1")
    parser.add_argument("--tts-voice", default="Rachel")
    args = parser.parse_args()

    log.info("Starting relay on %s:%s", args.host, args.port)
    log.info("OpenClaw: %s", args.openclaw_url)
    # TODO: start server

if __name__ == "__main__":
    main()
```

---

### Task 2: Wyoming frame parser

**File:** `relay/relay.py` — add `WyomingProtocol` class

```python
class WyomingProtocol(asyncio.Protocol):
    """Async protocol handler for one Voice PE connection."""

    def __init__(self, on_utterance):
        self._buffer = b""
        self._audio_chunks: list[bytes] = []
        self._on_utterance = on_utterance  # async callback(transcript) -> tts_audio_bytes
        self._transport = None

    def connection_made(self, transport):
        self._transport = transport
        peer = transport.get_extra_info("peername")
        log.info("Voice PE connected from %s:%s", *peer)

    def connection_lost(self, exc):
        log.info("Voice PE disconnected")

    def data_received(self, data: bytes):
        self._buffer += data
        self._parse_frames()

    def _parse_frames(self):
        while len(self._buffer) >= 4:
            json_len = struct.unpack(">I", self._buffer[:4])[0]
            frame_end = 4 + json_len
            if len(self._buffer) < frame_end:
                return  # incomplete frame header

            try:
                msg = json.loads(self._buffer[4:frame_end])
            except json.JSONDecodeError:
                log.warning("Invalid JSON in Wyoming frame")
                self._buffer = self._buffer[frame_end:]
                continue

            # Audio data may trail after JSON
            audio_data = self._buffer[frame_end:]
            self._buffer = b""  # consumed what we could

            self._handle_message(msg, audio_data)

    def _handle_message(self, msg: dict, audio_data: bytes):
        msg_type = msg.get("type", "")

        if msg_type == "describe":
            log.info("Voice PE described: %s", msg.get("data", {}))
        elif msg_type == "audio":
            if audio_data:
                self._audio_chunks.append(audio_data)
        elif msg_type == "audio-stop":
            log.info("End of utterance — %d audio chunks, %d bytes",
                     len(self._audio_chunks),
                     sum(len(c) for c in self._audio_chunks))
            asyncio.ensure_future(self._process_utterance())

    async def _process_utterance(self):
        try:
            pcm = b"".join(self._audio_chunks)
            self._audio_chunks = []

            # Hand off to pipeline
            response_text, tts_pcm = await self._on_utterance(pcm)

            # Send transcript back
            self._send_wyoming("transcript", {"text": ""})  # Voice PE may display

            # Send synthesize header
            self._send_wyoming("synthesize", {"text": response_text})

            # Stream PCM audio back as Wyoming audio events
            # 1024 bytes per frame (~32ms at 16kHz 16bit mono)
            chunk_size = 1024
            for offset in range(0, len(tts_pcm), chunk_size):
                chunk = tts_pcm[offset:offset + chunk_size]
                self._send_wyoming("audio", {
                    "rate": 16000,
                    "width": 2,
                    "channels": 1,
                }, audio_payload=chunk)

        except Exception:
            log.exception("Error processing utterance")

    def _send_wyoming(self, msg_type: str, data: dict, audio_payload: bytes = b""):
        payload = json.dumps({"type": msg_type, "data": data}).encode()
        header = struct.pack(">I", len(payload))
        self._transport.write(header + payload + audio_payload)
```

---

### Task 3: PCM → WAV converter (for Whisper)

**File:** `relay/relay.py` — add helper

```python
def pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 16000, channels: int = 1, bits: int = 16) -> bytes:
    """Convert raw PCM to WAV file bytes (in memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(bits // 8)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()
```

---

### Task 4: STT client (Whisper API)

**File:** `relay/relay.py` — add `Transcriber` class

```python
from openai import OpenAI

class Transcriber:
    def __init__(self, api_key: str, model: str = "whisper-1"):
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def transcribe(self, pcm: bytes) -> str:
        wav = pcm_to_wav_bytes(pcm)
        # OpenAI Whisper expects a file-like object
        result = self._client.audio.transcriptions.create(
            model=self._model,
            file=("audio.wav", wav, "audio/wav"),
        )
        return result.text.strip()
```

---

### Task 5: OpenClaw client

**File:** `relay/relay.py` — add `OpenClawClient` class

```python
import aiohttp  # or use urllib for stdlib-only

class OpenClawClient:
    def __init__(self, base_url: str, token: str = ""):
        self._url = base_url.rstrip("/") + "/v1/chat/completions"
        self._token = token

    async def chat(self, message: str, session_id: str = "voice-pe") -> str:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        headers["x-openclaw-session-key"] = session_id

        body = {
            "model": "openclaw",
            "messages": [{"role": "user", "content": message}],
        }

        # Use aiohttp for async HTTP
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(self._url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                data = await resp.json()
                choices = data.get("choices", [])
                if choices:
                    return choices[0]["message"]["content"]
                return "Sorry, I didn't understand."
```

---

### Task 6: TTS client (ElevenLabs)

**File:** `relay/relay.py` — add `Synthesizer` class

ElevenLabs returns MP3. We need to decode MP3 → PCM. Use `ffmpeg` subprocess (available everywhere) or `pydub` (needs ffmpeg anyway).

```python
import subprocess
import tempfile

class Synthesizer:
    def __init__(self, api_key: str, voice: str = "Rachel"):
        self._client = ElevenLabs(api_key=api_key)  # from elevenlabs import ElevenLabs
        self._voice = voice

    def synthesize(self, text: str) -> bytes:
        """Synthesize text to PCM 16kHz 16bit mono bytes."""
        # 1. Get MP3 from ElevenLabs
        audio = self._client.generate(
            text=text,
            voice=self._voice,
            model="eleven_multilingual_v2",
        )
        mp3_bytes = b"".join(audio)

        # 2. Convert MP3 → PCM via ffmpeg
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_mp3:
            tmp_mp3.write(mp3_bytes)
            mp3_path = tmp_mp3.name

        try:
            result = subprocess.run(
                ["ffmpeg", "-i", mp3_path, "-f", "s16le", "-acodec", "pcm_s16le",
                 "-ar", "16000", "-ac", "1", "-"],
                capture_output=True,
                check=True,
            )
            return result.stdout
        finally:
            os.unlink(mp3_path)
```

---

### Task 7: Wire everything together — main pipeline

**File:** `relay/relay.py` — complete `main()`

```python
async def main_async(args):
    transcriber = Transcriber(api_key=os.environ["OPENAI_API_KEY"], model=args.stt_model)
    synthesizer = Synthesizer(api_key=os.environ["ELEVENLABS_API_KEY"], voice=args.tts_voice)
    openclaw = OpenClawClient(base_url=args.openclaw_url, token=args.openclaw_token)

    async def on_utterance(pcm: bytes) -> tuple[str, bytes]:
        log.info("Transcribing %d bytes of PCM...", len(pcm))
        transcript = transcriber.transcribe(pcm)
        log.info("Transcript: %s", transcript)

        log.info("Querying OpenClaw...")
        response = await openclaw.chat(transcript)
        log.info("Response: %s", response)

        log.info("Synthesizing speech...")
        tts_pcm = synthesizer.synthesize(response)
        log.info("TTS: %d bytes PCM", len(tts_pcm))

        return response, tts_pcm

    loop = asyncio.get_running_loop()
    server = await loop.create_server(
        lambda: WyomingProtocol(on_utterance),
        host=args.host,
        port=args.port,
    )
    log.info("Relay listening on %s:%s", args.host, args.port)

    async with server:
        await server.serve_forever()

def main():
    parser = argparse.ArgumentParser(description="OpenHAVoice Relay")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=10200)
    parser.add_argument("--openclaw-url", default=os.environ.get("OPENCLAW_URL", "http://192.168.50.186:18789"))
    parser.add_argument("--openclaw-token", default=os.environ.get("OPENCLAW_TOKEN", ""))
    parser.add_argument("--stt-model", default="whisper-1")
    parser.add_argument("--tts-voice", default="Rachel")
    args = parser.parse_args()

    asyncio.run(main_async(args))
```

---

### Task 8: Dependencies (aiohttp)

**File:** `relay/requirements.txt` — final version

```txt
openai>=1.0
elevenlabs>=1.0
aiohttp>=3.9
```

Note: `ffmpeg` must be installed on the host system (not a Python dep).

---

## Verification

```bash
# Start relay
cd relay
pip install -r requirements.txt
OPENAI_API_KEY=sk-... ELEVENLABS_API_KEY=... python relay.py

# In another terminal, simulate Voice PE with netcat
echo -n '{"type":"describe","data":{"format":"pcm","rate":16000,"width":2,"channels":1}}' | \
  python3 -c "import sys,struct; d=sys.stdin.buffer.read(); sys.stdout.buffer.write(struct.pack('>I',len(d))+d)" | \
  nc localhost 10200
```

---

## Gaps / Future

- **STT fallback:** Piper or local whisper.cpp for offline
- **TTS fallback:** Piper for offline TTS (no ElevenLabs cost)
- **Multiple concurrent devices:** Already handled by asyncio Protocol
- **Wake word on relay side:** Could do VAD or second-stage wake word verification
- **Session persistence:** OpenClaw session is already keyed per device
- **Error recovery:** Currently logs and continues; could add retry logic
