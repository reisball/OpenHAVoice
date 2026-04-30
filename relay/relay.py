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

        # TODO: replace fixed response with OpenClaw chat response.
        await speak("Test erfolgreich. Ich habe dich verstanden.")
        await asyncio.sleep(8)
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


async def main() -> None:
    global CLIENT, LOCAL_IP
    load_env()
    host = require("VOICE_HOST")
    psk = require("VOICE_PSK")
    LOCAL_IP = local_ip_for(host)

    runner = await start_http_server()
    CLIENT = APIClient(host, 6053, os.environ.get("VOICE_PASSWORD", ""), noise_psk=psk)
    await CLIENT.connect(login=True)
    try:
        info = await CLIENT.device_info()
        print(f"CONNECTED {info.name}. Press button, speak, then wait for playback.", flush=True)
        unsubscribe = CLIENT.subscribe_voice_assistant(
            handle_start=on_start,
            handle_stop=on_stop,
            handle_audio=on_audio,
        )
        try:
            max_capture_seconds = float(os.environ.get("MAX_CAPTURE_SECONDS", "20"))
            while True:
                await asyncio.sleep(0.2)
                if STARTED_AT and not STOPPING and time.time() - STARTED_AT > max_capture_seconds:
                    await finish("timeout")
        finally:
            if unsubscribe:
                unsubscribe()
    finally:
        await CLIENT.disconnect()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
