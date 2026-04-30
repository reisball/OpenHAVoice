#!/usr/bin/env python3
"""OpenHAVoice relay — stock Voice PE ↔ local STT/TTS over ESPHome Native API.

This proof-of-concept intentionally uses the Voice PE stock ESPHome firmware.
It does not implement Wyoming. The Voice PE voice assistant stream is exposed via
ESPHome Native API on TCP/6053 and requires the device Noise PSK.

Config management: CLI via `python -m relay.cli`, Web UI via /config routes.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
import uuid
import wave
from pathlib import Path

import audioop
import requests
import webrtcvad
from aioesphomeapi import APIClient, VoiceAssistantEventType as E
from aiohttp import web

from .config import RelayConfig, load_config

CFG: RelayConfig = None  # type: ignore[assignment]  # set in main()


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
    url = CFG.whisper_url
    language = CFG.language
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
    url = CFG.orpheus_url
    payload = {
        "model": CFG.orpheus_model,
        "voice": CFG.orpheus_voice,
        "input": text,
    }
    response = requests.post(url, json=payload, timeout=90)
    response.raise_for_status()
    return response.content


def session_key_for_device(device_name: str | None = None) -> str:
    configured = CFG.openclaw_session_key.strip()
    if configured:
        return configured
    suffix = (device_name or CFG.voice_host).strip()
    safe = "".join(ch if ch.isalnum() or ch in "._:-" else "-" for ch in suffix).strip("-")
    return f"openhavoice:{safe or 'voice-pe'}"


def openclaw_chat(message: str) -> str:
    url = CFG.openclaw_url.rstrip("/")
    if not url.endswith("/v1/chat/completions"):
        url = url + "/v1/chat/completions"

    headers = {"Content-Type": "application/json"}
    token = CFG.openclaw_token
    if token:
        headers["Authorization"] = f"Bearer {token}"

    session_key = session_key_for_device(os.environ.get("OPENHAVOICE_DEVICE_NAME"))
    headers["x-openclaw-session-key"] = session_key

    channel = CFG.openclaw_message_channel
    if channel:
        headers["x-openclaw-message-channel"] = channel

    system_prompt = CFG.openclaw_voice_system_prompt
    payload = {
        "model": CFG.openclaw_model,
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

    # Config Web UI routes
    app.router.add_get("/config", config_get)
    app.router.add_put("/config", config_put)
    app.router.add_post("/config/validate", config_validate)
    app.router.add_post("/config/reload", config_reload)
    app.router.add_get("/", config_ui)

    runner = web.AppRunner(app)
    await runner.setup()
    host = CFG.tts_host
    port = CFG.tts_port
    await web.TCPSite(runner, host, port).start()
    print(f"HTTP_TTS http://{LOCAL_IP}:{port}", flush=True)
    return runner


async def speak(text: str) -> None:
    assert CLIENT is not None
    loop = asyncio.get_running_loop()
    audio = await loop.run_in_executor(None, synthesize, text)
    token = uuid.uuid4().hex
    TTS_FILES[token] = audio
    port = CFG.tts_port
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

    if abort or size < 3200 or SPEECH_MS < CFG.min_speech_ms:
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
        post_tts_grace_seconds = CFG.tts_post_playback_grace_seconds
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

    end_silence_ms = CFG.end_silence_ms
    min_speech_ms = CFG.min_speech_ms
    if SPEECH_SEEN and SPEECH_MS >= min_speech_ms and SILENT_MS >= end_silence_ms:
        await finish("vad_silence")


async def on_stop(abort: bool) -> None:
    await finish("device_stop", abort)


async def run_connected(host: str, psk: str) -> None:
    """Hold one long-lived ESPHome API connection until it fails or is cancelled."""
    global CLIENT, STARTED_AT, STOPPING

    CLIENT = APIClient(host, 6053, CFG.voice_password, noise_psk=psk)
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

        max_capture_seconds = CFG.max_capture_seconds
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
    reconnect_delay = CFG.reconnect_initial_seconds
    reconnect_max = CFG.reconnect_max_seconds

    while True:
        try:
            await run_connected(host, psk)
            reconnect_delay = CFG.reconnect_initial_seconds
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate service-style retry loop
            print(f"DISCONNECTED {type(exc).__name__}: {exc}; retrying in {reconnect_delay:.1f}s", flush=True)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, reconnect_max)


async def main() -> None:
    global LOCAL_IP, CFG
    CFG = load_config()

    errors = CFG.validate()
    if errors:
        print("Configuration errors:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        raise SystemExit(1)

    host = CFG.voice_host
    psk = CFG.voice_psk
    LOCAL_IP = local_ip_for(host)

    runner = await start_http_server()
    try:
        await connection_loop(host, psk)
    finally:
        await runner.cleanup()


# ── Config Web UI handlers ───────────────────────────────────

async def config_get(request: web.Request) -> web.Response:
    """GET /config — return current config as JSON (secrets redacted)."""
    reveal = request.query.get("reveal", "").lower() in ("1", "true", "yes")
    return web.json_response(CFG.to_dict(reveal=reveal))


async def config_put(request: web.Request) -> web.Response:
    """PUT /config — update one or more config fields."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    updated: list[str] = []
    errors: list[str] = []
    for key, value in body.items():
        try:
            CFG.update(key, str(value))
            updated.append(key)
        except (KeyError, ValueError) as exc:
            errors.append(f"{key}: {exc}")

    if errors:
        return web.json_response({"error": "Validation failed", "details": errors}, status=400)

    CFG.save()
    validation_errors = CFG.validate()
    return web.json_response({
        "ok": True,
        "updated": updated,
        "validation_errors": validation_errors,
    })


async def config_validate(request: web.Request) -> web.Response:
    """POST /config/validate — validate current config without saving."""
    errors = CFG.validate()
    return web.json_response({"valid": len(errors) == 0, "errors": errors})


async def config_reload(request: web.Request) -> web.Response:
    """POST /config/reload — reload config from .env file."""
    try:
        global CFG
        CFG = RelayConfig.load()
        errors = CFG.validate()
        return web.json_response({
            "ok": True,
            "message": "Config reloaded. Note: connection restart requires manual restart.",
            "validation_errors": errors,
        })
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def config_ui(request: web.Request) -> web.Response:
    """GET / — simple HTML form to view and edit config."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenHAVoice Config</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font: 14px/1.5 system-ui, sans-serif; background: #0d1117; color: #c9d1d9;
         max-width: 700px; margin: 2rem auto; padding: 0 1rem; }
  h1 { color: #58a6ff; margin-bottom: 1rem; }
  section { margin-bottom: 2rem; }
  h2 { color: #f0883e; font-size: 1rem; text-transform: uppercase;
       letter-spacing: .05em; margin-bottom: .5rem; }
  label { display: block; margin-bottom: .25rem; color: #8b949e; font-size: .85rem; }
  input, textarea { width: 100%; padding: .5rem; background: #161b22; border: 1px solid #30363d;
                    border-radius: 4px; color: #c9d1d9; font: inherit; margin-bottom: .75rem; }
  textarea { resize: vertical; min-height: 3rem; }
  button { background: #238636; color: #fff; border: none; padding: .5rem 1.5rem;
           border-radius: 4px; cursor: pointer; font: inherit; }
  button:hover { background: #2ea043; }
  .secret input { -webkit-text-security: disc; }
  .msg { padding: .5rem; border-radius: 4px; margin-bottom: 1rem; }
  .msg.ok { background: #1a3a2a; color: #3fb950; }
  .msg.err { background: #3a1a1a; color: #f85149; }
</style>
</head>
<body>
<h1>OpenHAVoice Configuration</h1>
<div id="msg"></div>
<form id="config-form">
  <section>
    <h2>Voice PE</h2>
    <label>VOICE_HOST</label><input name="VOICE_HOST" required>
    <label class="secret">VOICE_PSK</label><input name="VOICE_PSK" type="password" required>
    <label class="secret">VOICE_PASSWORD</label><input name="VOICE_PASSWORD" type="password">
  </section>
  <section>
    <h2>STT (Whisper)</h2>
    <label>WHISPER_URL</label><input name="WHISPER_URL">
    <label>LANGUAGE</label><input name="LANGUAGE">
  </section>
  <section>
    <h2>TTS (Orpheus)</h2>
    <label>ORPHEUS_URL</label><input name="ORPHEUS_URL">
    <label>ORPHEUS_MODEL</label><input name="ORPHEUS_MODEL">
    <label>ORPHEUS_VOICE</label><input name="ORPHEUS_VOICE">
    <label>TTS_HOST</label><input name="TTS_HOST">
    <label>TTS_PORT</label><input name="TTS_PORT" type="number">
    <label>TTS_POST_PLAYBACK_GRACE_SECONDS</label><input name="TTS_POST_PLAYBACK_GRACE_SECONDS" type="number" step="0.1">
  </section>
  <section>
    <h2>OpenClaw Gateway</h2>
    <label>OPENCLAW_URL</label><input name="OPENCLAW_URL">
    <label class="secret">OPENCLAW_TOKEN</label><input name="OPENCLAW_TOKEN" type="password">
    <label>OPENCLAW_SESSION_KEY</label><input name="OPENCLAW_SESSION_KEY">
    <label>OPENCLAW_MODEL</label><input name="OPENCLAW_MODEL">
    <label>OPENCLAW_MESSAGE_CHANNEL</label><input name="OPENCLAW_MESSAGE_CHANNEL">
    <label>System Prompt</label><textarea name="OPENCLAW_VOICE_SYSTEM_PROMPT"></textarea>
  </section>
  <section>
    <h2>VAD / Capture</h2>
    <label>MIN_SPEECH_MS</label><input name="MIN_SPEECH_MS" type="number">
    <label>END_SILENCE_MS</label><input name="END_SILENCE_MS" type="number">
    <label>MAX_CAPTURE_SECONDS</label><input name="MAX_CAPTURE_SECONDS" type="number" step="0.1">
  </section>
  <section>
    <h2>Network / Reconnect</h2>
    <label>RECONNECT_INITIAL_SECONDS</label><input name="RECONNECT_INITIAL_SECONDS" type="number" step="0.1">
    <label>RECONNECT_MAX_SECONDS</label><input name="RECONNECT_MAX_SECONDS" type="number" step="0.1">
  </section>
  <button type="submit">Save Configuration</button>
</form>
<script>
async function load() {
  const r = await fetch('/config');
  const cfg = await r.json();
  const form = document.getElementById('config-form');
  for (const [key, val] of Object.entries(cfg)) {
    const el = form.elements[key.toUpperCase()];
    if (el && val !== '****') el.value = val ?? '';
  }
}
document.getElementById('config-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const data = {};
  for (const el of form.elements) {
    if (el.name) data[el.name] = el.value;
  }
  const r = await fetch('/config', { method: 'PUT',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
  const result = await r.json();
  const msg = document.getElementById('msg');
  if (r.ok) {
    msg.className = 'msg ok';
    msg.textContent = '✓ Saved. ' + (result.validation_errors?.length
      ? 'Warnings: ' + result.validation_errors.join('; ') : 'Config valid.');
  } else {
    msg.className = 'msg err';
    msg.textContent = '✗ ' + (result.error || result.details?.join(', ') || 'Unknown error');
  }
});
load();
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


if __name__ == "__main__":
    asyncio.run(main())
