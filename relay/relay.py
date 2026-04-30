#!/usr/bin/env python3
"""OpenHAVoice relay — stock Voice PE ↔ local STT/TTS over ESPHome Native API.

This proof-of-concept intentionally uses the Voice PE stock ESPHome firmware.
It does not implement Wyoming. The Voice PE voice assistant stream is exposed via
ESPHome Native API on TCP/6053 and requires the device Noise PSK.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
import wave
from pathlib import Path

import audioop
import requests
import webrtcvad
from aioesphomeapi import APIClient, VoiceAssistantEventType as E
from aiohttp import web


BUFFER = bytearray()
CLIENT: APIClient | None = None
STARTED_AT: float | None = None
STOPPING = False
SPEECH_SEEN = False
SILENT_MS = 0
SPEECH_MS = 0
TTS_FILES: dict[str, bytes] = {}
LOCAL_IP = ""

VAD = webrtcvad.Vad(2)
FRAME_MS = 30
FRAME_BYTES = int(16000 * 2 * FRAME_MS / 1000)


def load_env() -> None:
    """Load KEY=value pairs from .env without overriding shell env vars."""
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing {name}; copy .env.example to .env and configure it")
    return value


def local_ip_for(target: str) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 1))
        return sock.getsockname()[0]
    finally:
        sock.close()


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)


def transcribe(path: Path) -> str:
    url = os.environ.get("WHISPER_URL", "http://192.168.50.51:8000/v1/audio/transcriptions")
    language = os.environ.get("LANGUAGE", "de")
    with path.open("rb") as handle:
        response = requests.post(
            url,
            files={"file": (path.name, handle, "audio/wav")},
            data={"model": "whisper-1", "language": language, "response_format": "json"},
            timeout=90,
        )
    response.raise_for_status()
    return response.json().get("text", "").strip()


def synthesize(text: str) -> bytes:
    url = os.environ.get("ORPHEUS_URL", "http://192.168.50.52:5005/v1/audio/speech")
    payload = {
        "model": os.environ.get("ORPHEUS_MODEL", "orpheus-german-fix"),
        "voice": os.environ.get("ORPHEUS_VOICE", "jana"),
        "input": text,
    }
    response = requests.post(url, json=payload, timeout=90)
    response.raise_for_status()
    return response.content


def session_key_for_device(device_name: str | None = None) -> str:
    configured = os.environ.get("OPENCLAW_SESSION_KEY", "").strip()
    if configured:
        return configured
    suffix = (device_name or os.environ.get("VOICE_HOST", "voice-pe")).strip()
    safe = "".join(ch if ch.isalnum() or ch in "._:-" else "-" for ch in suffix).strip("-")
    return f"openhavoice:{safe or 'voice-pe'}"


def openclaw_chat(message: str) -> str:
    url = os.environ.get("OPENCLAW_URL", "http://127.0.0.1:18789").rstrip("/")
    if not url.endswith("/v1/chat/completions"):
        url = url + "/v1/chat/completions"

    headers = {"Content-Type": "application/json"}
    token = os.environ.get("OPENCLAW_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    session_key = session_key_for_device(os.environ.get("OPENHAVOICE_DEVICE_NAME"))
    headers["x-openclaw-session-key"] = session_key

    channel = os.environ.get("OPENCLAW_MESSAGE_CHANNEL", "voice")
    if channel:
        headers["x-openclaw-message-channel"] = channel

    system_prompt = os.environ.get(
        "OPENCLAW_VOICE_SYSTEM_PROMPT",
        "Du antwortest als Zoe über einen Voice Assistant. Antworte kurz, natürlich "
        "und ohne Markdown, Listen oder Emojis. Ein bis zwei Sätze reichen.",
    )
    payload = {
        "model": os.environ.get("OPENCLAW_MODEL", "openclaw/default"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        "stream": False,
    }

    response = requests.post(url, json=payload, headers=headers, timeout=120)
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenClaw returned no choices")
    text = (choices[0].get("message") or {}).get("content", "").strip()
    if not text:
        raise RuntimeError("OpenClaw returned an empty message")
    return text


async def tts_handler(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    data = TTS_FILES.pop(token, None)
    if data is None:
        return web.Response(status=404, text="gone")
    print(f"SERVE_TTS token={token[:6]}…{token[-4:]} bytes={len(data)} remote={request.remote}", flush=True)
    return web.Response(body=data, headers={"Content-Type": "audio/wav"})


async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/tts/{token}.wav", tts_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    host = os.environ.get("TTS_HOST", "0.0.0.0")
    port = int(os.environ.get("TTS_PORT", "8765"))
    await web.TCPSite(runner, host, port).start()
    print(f"HTTP_TTS http://{LOCAL_IP}:{port}", flush=True)
    return runner


async def speak(text: str) -> None:
    assert CLIENT is not None
    loop = asyncio.get_running_loop()
    audio = await loop.run_in_executor(None, synthesize, text)
    token = uuid.uuid4().hex
    TTS_FILES[token] = audio
    port = int(os.environ.get("TTS_PORT", "8765"))
    url = f"http://{LOCAL_IP}:{port}/tts/{token}.wav"
    print(f"TTS_READY {url} bytes={len(audio)} text={text!r}", flush=True)
    CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_TTS_START, {"text": text})
    CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_TTS_END, {"url": url})


async def finish(reason: str, abort: bool = False) -> None:
    global STOPPING
    assert CLIENT is not None
    if STOPPING:
        return
    STOPPING = True

    size = len(BUFFER)
    print(
        f"STOP reason={reason} abort={abort} bytes={size} seconds≈{size / 32000:.2f} "
        f"speech_ms={SPEECH_MS} silent_ms={SILENT_MS}",
        flush=True,
    )

    try:
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_VAD_END, None)
    except Exception as exc:  # noqa: BLE001 - probe script should keep running
        print(f"WARN vad_end failed {exc!r}", flush=True)

    min_speech_ms = int(os.environ.get("MIN_SPEECH_MS", "300"))
    if abort or size < 3200 or SPEECH_MS < min_speech_ms:
        try:
            CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_RUN_END, None)
        except Exception:
            pass
        return

    wav_path = Path(__file__).with_name(f"roundtrip-{int(time.time())}.wav")
    write_wav(wav_path, bytes(BUFFER))
    print(f"WAV {wav_path}", flush=True)

    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(None, transcribe, wav_path)
        print(f"TRANSCRIPT {text!r}", flush=True)
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_END, {"text": text})
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_INTENT_START, None)
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_INTENT_END, None)

        response_text = await loop.run_in_executor(None, openclaw_chat, text)
        print(f"OPENCLAW_REPLY {response_text!r}", flush=True)
        await speak(response_text)
        post_tts_grace_seconds = float(os.environ.get("TTS_POST_PLAYBACK_GRACE_SECONDS", "1.0"))
        if post_tts_grace_seconds > 0:
            await asyncio.sleep(post_tts_grace_seconds)
    except Exception as exc:  # noqa: BLE001
        print(f"ROUNDTRIP_FAILED {exc!r}", flush=True)
    finally:
        try:
            CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_RUN_END, None)
        except Exception:
            pass


async def on_start(conversation_id, flags, audio_settings, wake_word_phrase):
    global STARTED_AT, STOPPING, SPEECH_SEEN, SILENT_MS, SPEECH_MS
    assert CLIENT is not None
    BUFFER.clear()
    STARTED_AT = time.time()
    STOPPING = False
    SPEECH_SEEN = False
    SILENT_MS = 0
    SPEECH_MS = 0
    print(
        f"START wake={wake_word_phrase!r} flags={flags} settings={audio_settings} conv={conversation_id!r}",
        flush=True,
    )
    CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_RUN_START, None)
    CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_VAD_START, None)
    CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_START, None)
    return 0  # API audio


async def on_audio(data: bytes) -> None:
    global SPEECH_SEEN, SILENT_MS, SPEECH_MS
    if STOPPING:
        return
    BUFFER.extend(data)
    voiced_in_chunk = False
    for offset in range(0, len(data) - FRAME_BYTES + 1, FRAME_BYTES):
        frame = data[offset : offset + FRAME_BYTES]
        try:
            voiced = VAD.is_speech(frame, 16000)
        except Exception:
            continue
        if voiced:
            voiced_in_chunk = True
            SPEECH_SEEN = True
            SPEECH_MS += FRAME_MS
            SILENT_MS = 0
        elif SPEECH_SEEN:
            SILENT_MS += FRAME_MS

    if voiced_in_chunk and SPEECH_MS <= FRAME_MS * 3:
        print("SPEECH_START", flush=True)
    if len(BUFFER) % (32000 * 2) < len(data):
        print(
            f"AUDIO seconds≈{len(BUFFER) / 32000:.1f} speech_ms={SPEECH_MS} "
            f"silent_ms={SILENT_MS} rms={audioop.rms(data, 2)}",
            flush=True,
        )

    end_silence_ms = int(os.environ.get("END_SILENCE_MS", "900"))
    min_speech_ms = int(os.environ.get("MIN_SPEECH_MS", "300"))
    if SPEECH_SEEN and SPEECH_MS >= min_speech_ms and SILENT_MS >= end_silence_ms:
        await finish("vad_silence")


async def on_stop(abort: bool) -> None:
    await finish("device_stop", abort)


async def run_connected(host: str, psk: str) -> None:
    """Hold one long-lived ESPHome API connection until it fails or is cancelled."""
    global CLIENT, STARTED_AT, STOPPING

    CLIENT = APIClient(host, 6053, os.environ.get("VOICE_PASSWORD", ""), noise_psk=psk)
    unsubscribe = None
    try:
        await CLIENT.connect(login=True)
        info = await CLIENT.device_info()
        os.environ["OPENHAVOICE_DEVICE_NAME"] = info.name
        print(
            f"CONNECTED {info.name}. Backend client is active; "
            f"session={session_key_for_device(info.name)!r}; press button and speak.",
            flush=True,
        )
        unsubscribe = CLIENT.subscribe_voice_assistant(
            handle_start=on_start,
            handle_stop=on_stop,
            handle_audio=on_audio,
        )

        max_capture_seconds = float(os.environ.get("MAX_CAPTURE_SECONDS", "20"))
        while True:
            await asyncio.sleep(0.2)
            if STARTED_AT and not STOPPING and time.time() - STARTED_AT > max_capture_seconds:
                await finish("timeout")
    finally:
        if unsubscribe:
            unsubscribe()
        if CLIENT is not None:
            try:
                await CLIENT.disconnect()
            except Exception as exc:  # noqa: BLE001 - reconnect loop handles recovery
                print(f"WARN disconnect failed {exc!r}", flush=True)
        CLIENT = None
        STARTED_AT = None
        STOPPING = False


async def connection_loop(host: str, psk: str) -> None:
    """Reconnect forever so the Voice PE keeps a backend client instead of going red."""
    reconnect_delay = float(os.environ.get("RECONNECT_INITIAL_SECONDS", "1"))
    reconnect_max = float(os.environ.get("RECONNECT_MAX_SECONDS", "30"))

    while True:
        try:
            await run_connected(host, psk)
            reconnect_delay = float(os.environ.get("RECONNECT_INITIAL_SECONDS", "1"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate service-style retry loop
            print(f"DISCONNECTED {type(exc).__name__}: {exc}; retrying in {reconnect_delay:.1f}s", flush=True)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, reconnect_max)


async def main() -> None:
    global LOCAL_IP
    load_env()
    host = require("VOICE_HOST")
    psk = require("VOICE_PSK")
    LOCAL_IP = local_ip_for(host)

    runner = await start_http_server()
    try:
        await connection_loop(host, psk)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
