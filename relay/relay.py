#!/usr/bin/env python3
"""OpenHAVoice relay — stock Voice PE ↔ local STT/TTS over ESPHome Native API.

This proof-of-concept intentionally uses the Voice PE stock ESPHome firmware.
It does not implement Wyoming. The Voice PE voice assistant stream is exposed via
ESPHome Native API on TCP/6053 and requires the device Noise PSK.

Config management: CLI via `python -m relay.cli`, Web UI via /config routes.
"""

from __future__ import annotations

import asyncio
import copy
import io
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

try:
    from .config import RelayConfig, load_config
except ImportError:  # Allows systemd/direct execution as relay/relay.py
    from config import RelayConfig, load_config  # type: ignore[no-redef]

CFG: RelayConfig = None  # type: ignore[assignment]  # set in main()


BUFFER = bytearray()
CLIENT: APIClient | None = None
STARTED_AT: float | None = None
STOPPING = False
SPEECH_SEEN = False
SILENT_MS = 0
SPEECH_MS = 0
LOW_RMS_MS = 0
TTS_FILES: dict[str, bytes] = {}
LOCAL_IP = ""
SERVICE_STARTED_AT = time.time()

# Device Overview stats (in-memory, reset on restart)
STATS: dict[str, object] = {
    "total_sessions": 0,
    "last_session_at": None,
    "last_stop_reason": None,
    "last_session_duration_sec": None,
    "device_name": None,
    "connected": False,
    "service_started_at": SERVICE_STARTED_AT,
}

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


def wav_duration_seconds(data: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate = wav.getframerate()
            if not rate:
                return None
            return wav.getnframes() / rate
    except Exception:
        return None


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


async def status_handler(_request: web.Request) -> web.Response:
    data = dict(STATS)
    data["service_started_at"] = SERVICE_STARTED_AT
    data["service_uptime_sec"] = round(time.time() - SERVICE_STARTED_AT, 1)
    return web.json_response(data)


def _load_voice_devices(reveal: bool = False) -> list[dict[str, str]]:
    try:
        raw = json.loads(CFG.voice_devices or "[]")
    except json.JSONDecodeError:
        raw = []
    devices: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        host = str(item.get("host", "")).strip()
        if not host:
            continue
        device = {
            "name": name or host,
            "host": host,
            "psk": str(item.get("psk", "")),
            "password": str(item.get("password", "")),
        }
        if not reveal:
            device["psk"] = "****" if device["psk"] else ""
            device["password"] = "****" if device["password"] else ""
        devices.append(device)
    return devices


def _save_voice_devices(devices: list[dict[str, str]]) -> None:
    CFG.voice_devices = json.dumps(devices, ensure_ascii=False, separators=(",", ":"))
    CFG.save()


async def devices_get(request: web.Request) -> web.Response:
    denied = _require_config_auth(request)
    if denied is not None:
        return denied
    return web.json_response({
        "active_host": CFG.voice_host,
        "devices": _load_voice_devices(reveal=False),
    })


async def devices_put(request: web.Request) -> web.Response:
    global CFG, LOCAL_IP
    denied = _require_config_auth(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    name = str(body.get("name", "")).strip()
    host = str(body.get("host", "")).strip()
    psk = str(body.get("psk", "")).strip()
    password = str(body.get("password", "")).strip()
    activate = bool(body.get("activate", False))
    if not host:
        return web.json_response({"error": "host is required"}, status=400)

    devices = _load_voice_devices(reveal=True)
    existing = next((d for d in devices if d.get("host") == host), None)
    if existing is None:
        if not psk:
            return web.json_response({"error": "psk is required for new devices"}, status=400)
        existing = {"name": name or host, "host": host, "psk": psk, "password": password}
        devices.append(existing)
    else:
        existing["name"] = name or existing.get("name") or host
        if psk and psk != "****":
            existing["psk"] = psk
        if password and password != "****":
            existing["password"] = password

    if activate:
        if not existing.get("psk"):
            return web.json_response({"error": "selected device has no psk"}, status=400)
        CFG.voice_host = existing["host"]
        CFG.voice_psk = existing["psk"]
        CFG.voice_password = existing.get("password", "")
        try:
            LOCAL_IP = local_ip_for(CFG.voice_host)
        except Exception:
            pass

    _save_voice_devices(devices)
    return web.json_response({
        "ok": True,
        "active_host": CFG.voice_host,
        "devices": _load_voice_devices(reveal=False),
        "restart_required": activate,
    })


async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/tts/{token}.wav", tts_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/devices", devices_get)
    app.router.add_put("/api/devices", devices_put)

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
    STATS["last_tts_timestamp"] = time.time()
    STATS["last_out_at"] = STATS["last_tts_timestamp"]
    out_duration = wav_duration_seconds(audio)
    STATS["last_out_duration_sec"] = round(out_duration, 1) if out_duration is not None else None


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

    duration = None
    if STARTED_AT is not None:
        duration = time.time() - STARTED_AT
    input_duration = size / 32000 if size else None
    if reason:
        STATS["last_stop_reason"] = reason
        STATS["last_session_at"] = time.time()
        STATS["last_session_duration_sec"] = round(duration, 1) if duration is not None else None

    try:
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_VAD_END, None)
    except Exception as exc:  # noqa: BLE001 - probe script should keep running
        print(f"WARN vad_end failed {exc!r}", flush=True)

    if abort or size < 3200 or SPEECH_MS < CFG.min_speech_ms:
        if STARTED_AT is not None:
            STATS["last_turn_duration_sec"] = round(time.time() - STARTED_AT, 1)
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
        STATS["last_in_at"] = time.time()
        STATS["last_in_duration_sec"] = round(input_duration, 1) if input_duration is not None else None
        print(f"TRANSCRIPT {text!r}", flush=True)
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_END, {"text": text})
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_INTENT_START, None)
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_INTENT_END, None)
        STATS["last_stt_text"] = text

        response_text = await loop.run_in_executor(None, openclaw_chat, text)
        STATS["last_llm_text"] = response_text
        print(f"OPENCLAW_REPLY {response_text!r}", flush=True)
        await speak(response_text)
        post_tts_grace_seconds = CFG.tts_post_playback_grace_seconds
        if post_tts_grace_seconds > 0:
            await asyncio.sleep(post_tts_grace_seconds)
    except Exception as exc:  # noqa: BLE001
        print(f"ROUNDTRIP_FAILED {exc!r}", flush=True)
    finally:
        if STARTED_AT is not None:
            STATS["last_turn_duration_sec"] = round(time.time() - STARTED_AT, 1)
        try:
            CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_RUN_END, None)
        except Exception:
            pass


async def on_start(conversation_id, flags, audio_settings, wake_word_phrase):
    global STARTED_AT, STOPPING, SPEECH_SEEN, SILENT_MS, SPEECH_MS, LOW_RMS_MS
    assert CLIENT is not None
    BUFFER.clear()
    STARTED_AT = time.time()
    STOPPING = False
    SPEECH_SEEN = False
    SILENT_MS = 0
    SPEECH_MS = 0
    LOW_RMS_MS = 0
    STATS["total_sessions"] = STATS.get("total_sessions", 0) + 1
    STATS["current_session_started_at"] = STARTED_AT
    print(
        f"START wake={wake_word_phrase!r} flags={flags} settings={audio_settings} conv={conversation_id!r}",
        flush=True,
    )
    CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_RUN_START, None)
    CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_VAD_START, None)
    CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_START, None)
    return 0  # API audio


async def on_audio(data: bytes) -> None:
    global SPEECH_SEEN, SILENT_MS, SPEECH_MS, LOW_RMS_MS
    if STOPPING:
        return
    BUFFER.extend(data)
    chunk_rms = audioop.rms(data, 2)
    chunk_ms = int(len(data) / 32)  # 16 kHz, 16-bit mono => 32 bytes/ms
    rms_silence_threshold = CFG.rms_silence_threshold
    if SPEECH_SEEN:
        if chunk_rms < rms_silence_threshold:
            LOW_RMS_MS += chunk_ms
        else:
            LOW_RMS_MS = 0
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
            f"silent_ms={SILENT_MS} low_rms_ms={LOW_RMS_MS} rms={chunk_rms}",
            flush=True,
        )

    end_silence_ms = CFG.end_silence_ms
    rms_end_silence_ms = CFG.rms_end_silence_ms or end_silence_ms
    min_speech_ms = CFG.min_speech_ms
    if SPEECH_SEEN and SPEECH_MS >= min_speech_ms and SILENT_MS >= end_silence_ms:
        await finish("vad_silence")
    elif SPEECH_SEEN and SPEECH_MS >= min_speech_ms and LOW_RMS_MS >= rms_end_silence_ms:
        await finish("rms_silence")


async def on_stop(abort: bool) -> None:
    await finish("device_stop", abort)


async def run_connected(host: str, psk: str) -> None:
    """Hold one long-lived ESPHome API connection until it fails or is cancelled."""
    global CLIENT, STARTED_AT, STOPPING
    STATS["device_name"] = None
    STATS["connected"] = False

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
        STATS["device_name"] = info.name
        STATS["connected"] = True
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
        STATS["connected"] = False


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
    VAD.set_mode(CFG.vad_aggressiveness)

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

def _config_request_allowed(_request: web.Request) -> bool:
    """Always allow config API access."""
    return True


def _require_config_auth(request: web.Request) -> web.Response | None:
    if _config_request_allowed(request):
        return None
    return web.json_response(
        {
            "error": "Config API is protected",
            "details": "Config API is open (no admin token configured).",
        },
        status=403,
    )


async def config_get(request: web.Request) -> web.Response:
    """GET /config — return current config as JSON, always redacting secrets."""
    denied = _require_config_auth(request)
    if denied is not None:
        return denied
    return web.json_response(CFG.to_dict(reveal=False))


async def config_put(request: web.Request) -> web.Response:
    """PUT /config — update one or more config fields."""
    global CFG
    denied = _require_config_auth(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    candidate = copy.deepcopy(CFG)
    updated: list[str] = []
    errors: list[str] = []
    secret_fields = {"VOICE_PSK", "VOICE_PASSWORD", "VOICE_DEVICES", "OPENCLAW_TOKEN", "CONFIG_ADMIN_TOKEN"}
    for key, value in body.items():
        key_upper = str(key).upper()
        # Browser password fields are empty/redacted placeholders unless explicitly changed.
        if key_upper in secret_fields and str(value) in {"", "****"}:
            continue
        try:
            candidate.update(str(key), str(value))
            updated.append(str(key))
        except (KeyError, ValueError) as exc:
            errors.append(f"{key}: {exc}")

    if errors:
        return web.json_response({"error": "Validation failed", "details": errors}, status=400)

    validation_errors = candidate.validate()
    if validation_errors:
        return web.json_response({"error": "Validation failed", "details": validation_errors}, status=400)

    candidate.save()
    CFG = candidate
    VAD.set_mode(CFG.vad_aggressiveness)
    return web.json_response({
        "ok": True,
        "updated": updated,
        "config": CFG.to_dict(reveal=False),
    })


async def config_validate(request: web.Request) -> web.Response:
    """POST /config/validate — validate current config without saving."""
    denied = _require_config_auth(request)
    if denied is not None:
        return denied
    errors = CFG.validate()
    return web.json_response({"valid": len(errors) == 0, "errors": errors})


async def config_reload(request: web.Request) -> web.Response:
    """POST /config/reload — reload config from env file."""
    denied = _require_config_auth(request)
    if denied is not None:
        return denied
    try:
        global CFG
        CFG = load_config()
        VAD.set_mode(CFG.vad_aggressiveness)
        errors = CFG.validate()
        return web.json_response({
            "ok": True,
            "message": "Config reloaded. Note: connection restart requires manual restart.",
            "validation_errors": errors,
        })
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def config_ui(request: web.Request) -> web.Response:
    """GET / — compact Web UI for viewing and editing relay config."""
    defaults_json = json.dumps(RelayConfig().to_dict(reveal=False), ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenHAVoice Config</title>
<style>
  :root {{
    --bg: #0d1117; --panel: #161b22; --panel2: #0f1722; --line: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --blue: #58a6ff; --orange: #f0883e;
    --green: #3fb950; --red: #f85149; --red-bg: #3a1a1a; --green-bg: #12351f;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); }}
  main {{ width: min(1180px, calc(100vw - 32px)); margin: 24px auto 56px; }}
  header {{ display: flex; align-items: end; justify-content: space-between; gap: 16px; margin-bottom: 16px; }}
  h1 {{ color: var(--blue); margin: 0; font-size: clamp(24px, 3vw, 34px); letter-spacing: -.02em; }}
  .sub {{ color: var(--muted); margin-top: 4px; }}
  .authbar {{ display: flex; gap: 8px; align-items: center;
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 10px; margin-bottom: 12px; }}
  .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
  .card {{ background: linear-gradient(180deg, var(--panel), var(--panel2)); border: 1px solid var(--line); border-radius: 14px; padding: 16px; }}
  .wide {{ grid-column: 1 / -1; }}
  #service-card, #device-card {{ margin-bottom: 14px; }}
  h2 {{ color: var(--orange); font-size: .85rem; text-transform: uppercase; letter-spacing: .08em; margin: 0 0 14px; }}
  .fields {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
  .field.full {{ grid-column: 1 / -1; }}
  .field.spacer {{ visibility: hidden; }}
  .section-divider {{ grid-column: 1 / -1; border-top: 1px solid var(--line); margin: 4px 0 2px; padding-top: 12px; color: var(--orange); font-size: .72rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
  label {{ display: flex; justify-content: space-between; gap: 8px; color: var(--muted); font-size: .76rem; font-weight: 700; letter-spacing: .04em; margin-bottom: 5px; }}
  .current {{ color: var(--blue); font-weight: 600; max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-transform: none; letter-spacing: 0; }}
  input, textarea, select {{ width: 100%; padding: 9px 10px; background: #0d1117; border: 1px solid var(--line); border-radius: 8px; color: var(--text); font: inherit; }}
  input:focus, textarea:focus, select:focus {{ outline: none; border-color: var(--blue); box-shadow: 0 0 0 2px #58a6ff33; }}
  textarea {{ resize: vertical; min-height: 82px; }}
  .hint {{ color: var(--muted); font-size: .76rem; margin-top: 4px; }}
  .hint code {{ color: #d2a8ff; background: #111722; padding: 1px 4px; border-radius: 4px; }}
  .actions {{ display: flex; gap: 10px; justify-content: flex-end; margin-top: 16px; }}
  button {{ background: #238636; color: #fff; border: 0; padding: 9px 14px; border-radius: 8px; cursor: pointer; font: inherit; font-weight: 700; }}
  button.secondary {{ background: #21262d; color: var(--text); border: 1px solid var(--line); }}
  button:hover {{ filter: brightness(1.1); }}
  .msg {{ padding: 10px 12px; border-radius: 10px; margin-bottom: 12px; display: none; }}
  .msg.ok {{ display: block; background: var(--green-bg); color: var(--green); }}
  .msg.err {{ display: block; background: var(--red-bg); color: var(--red); }}
  .readonly {{ opacity: .78; }}
  .status-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
  .status-dot.on {{ background: var(--green); box-shadow: 0 0 6px var(--green); }}
  .status-dot.off {{ background: var(--red); box-shadow: 0 0 6px var(--red); }}
  @media (max-width: 860px) {{ .grid, .fields {{ grid-template-columns: 1fr; }} header {{ display: block; }} }}
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>OpenHAVoice Configuration</h1>
      <div class="sub">Aktuelle Werte bearbeiten. Hinweise zeigen die Defaults; leere Secret-Felder behalten den aktuellen Wert.</div>
    </div>
  </header>

  <!-- Service Status Dashboard -->
  <div class="card wide" id="service-card">
    <h2>OpenHAVoice Service</h2>
    <div class="fields" style="grid-template-columns: repeat(3, 1fr);" id="service-overview">
      <div class="field"><label>Service Start</label><span id="svc-start" class="current" style="max-width:100%;font-size:1rem;">—</span></div>
      <div class="field"><label>Uptime</label><span id="svc-uptime" class="current" style="max-width:100%;font-size:1rem;">—</span></div>
    </div>
  </div>

  <!-- Device Overview Dashboard -->
  <div class="card wide" id="device-card">
    <h2>Device Status</h2>
    <div class="fields" style="grid-template-columns: repeat(3, 1fr);" id="device-overview">
      <div class="field"><label>Device</label><span id="dv-device" class="current" style="max-width:100%;font-size:1rem;">—</span></div>
      <div class="field"><label>Status</label><span id="dv-status" class="current" style="max-width:100%;font-size:1rem;">—</span></div>
      <div class="field"><label>Sessions</label><span id="dv-sessions" class="current" style="max-width:100%;font-size:1rem;">0</span></div>
      <div class="field"><label>Last In</label><span id="dv-in-at" class="current" style="max-width:100%;font-size:1rem;">—</span></div>
      <div class="field"><label>Duration</label><span id="dv-in-duration" class="current" style="max-width:100%;font-size:1rem;">—</span></div>
      <div class="field spacer" aria-hidden="true"></div>
      <div class="field"><label>Last Out</label><span id="dv-out-at" class="current" style="max-width:100%;font-size:1rem;">—</span></div>
      <div class="field"><label>Duration</label><span id="dv-out-duration" class="current" style="max-width:100%;font-size:1rem;">—</span></div>
      <div class="field spacer" aria-hidden="true"></div>
      <div class="field"><label>Turn Duration</label><span id="dv-turn-duration" class="current" style="max-width:100%;font-size:1rem;">—</span></div>
      <div class="field"><label>Last Stop</label><span id="dv-stop" class="current" style="max-width:100%;font-size:1rem;">—</span></div>
      <div class="field spacer" aria-hidden="true"></div>
    </div>
  </div>

  <div id="msg" class="msg"></div>

  <form id="config-form">
    <div class="grid">
      <section class="card">
        <h2>Voice PE Devices</h2>
        <div class="fields">
          <div class="field full"><label>Known Devices</label><select id="voice-device-select"><option value="">Manual / new device…</option></select><div class="hint">Auswählen lädt Name/Host; PSK bleibt serverseitig verborgen.</div></div>
          <div class="field"><label>Device Name</label><input id="voice-device-name" placeholder="home-assistant-voice-…"></div>
          <div class="field"><label>Host / IP</label><input id="voice-device-host" placeholder="192.168.50.xxx"></div>
          <div class="field"><label>Noise PSK</label><input id="voice-device-psk" type="password" placeholder="leave blank to keep existing"><div class="hint">Secret wird gespeichert, aber nie angezeigt.</div></div>
          <div class="field"><label>Password</label><input id="voice-device-password" type="password" placeholder="optional / leave blank"></div>
        </div>
        <div class="actions">
          <button class="secondary" type="button" id="voice-device-save">Save device</button>
          <button type="button" id="voice-device-activate">Save & activate</button>
        </div>
        <div class="hint">Aktivieren aktualisiert <code>VOICE_HOST</code>/<code>VOICE_PSK</code>; Verbindung wechselt nach Relay-Neustart.</div>
        <div class="section-divider">Active low-level config</div>
        <div class="fields">
          <div class="field"><label>VOICE_HOST <span class="current" data-current="VOICE_HOST"></span></label><input name="VOICE_HOST" required></div>
          <div class="field"><label>VOICE_PSK <span class="current" data-current="VOICE_PSK"></span></label><input name="VOICE_PSK" type="password" placeholder="leave blank to keep existing"><div class="hint">Default: leer · Secret bleibt serverseitig gespeichert.</div></div>
          <div class="field"><label>VOICE_PASSWORD <span class="current" data-current="VOICE_PASSWORD"></span></label><input name="VOICE_PASSWORD" type="password" placeholder="leave blank to keep existing"><div class="hint">Default: leer</div></div>
        </div>
      </section>

      <section class="card">
        <h2>Local Services</h2>
        <div class="fields">
          <div class="field full"><label>WHISPER_URL <span class="current" data-current="WHISPER_URL"></span></label><input name="WHISPER_URL"></div>
          <div class="field full"><label>LANGUAGE <span class="current" data-current="LANGUAGE"></span></label><input name="LANGUAGE"></div>
          <div class="section-divider">Orpheus TTS</div>
          <div class="field"><label>ORPHEUS_VOICE <span class="current" data-current="ORPHEUS_VOICE"></span></label><input name="ORPHEUS_VOICE"></div>
          <div class="field full"><label>ORPHEUS_URL <span class="current" data-current="ORPHEUS_URL"></span></label><input name="ORPHEUS_URL"></div>
          <div class="field"><label>ORPHEUS_MODEL <span class="current" data-current="ORPHEUS_MODEL"></span></label><input name="ORPHEUS_MODEL"></div>
          <div class="field"><label>TTS_POST_PLAYBACK_GRACE_SECONDS <span class="current" data-current="TTS_POST_PLAYBACK_GRACE_SECONDS"></span></label><input name="TTS_POST_PLAYBACK_GRACE_SECONDS" type="number" step="0.1"></div>
        </div>
      </section>

      <section class="card">
        <h2>Relay Endpoint</h2>
        <div class="fields">
          <div class="field"><label>TTS_HOST <span class="current" data-current="TTS_HOST"></span></label><input name="TTS_HOST"></div>
          <div class="field"><label>TTS_PORT <span class="current" data-current="TTS_PORT"></span></label><input name="TTS_PORT" type="number"></div>
        </div>
      </section>

      <section class="card">
        <h2>VAD / Capture</h2>
        <div class="fields">
          <div class="field"><label>MIN_SPEECH_MS <span class="current" data-current="MIN_SPEECH_MS"></span></label><input name="MIN_SPEECH_MS" type="number"></div>
          <div class="field"><label>END_SILENCE_MS <span class="current" data-current="END_SILENCE_MS"></span></label><input name="END_SILENCE_MS" type="number"></div>
          <div class="field"><label>MAX_CAPTURE_SECONDS <span class="current" data-current="MAX_CAPTURE_SECONDS"></span></label><input name="MAX_CAPTURE_SECONDS" type="number" step="0.1"></div>
          <div class="field"><label>VAD_AGGRESSIVENESS <span class="current" data-current="VAD_AGGRESSIVENESS"></span></label><input name="VAD_AGGRESSIVENESS" type="number" min="0" max="3"><div class="hint">0 = locker, 3 = aggressiv</div></div>
          <div class="field"><label>RMS_SILENCE_THRESHOLD <span class="current" data-current="RMS_SILENCE_THRESHOLD"></span></label><input name="RMS_SILENCE_THRESHOLD" type="number"></div>
          <div class="field"><label>RMS_END_SILENCE_MS <span class="current" data-current="RMS_END_SILENCE_MS"></span></label><input name="RMS_END_SILENCE_MS" type="number"></div>
        </div>
      </section>

      <section class="card wide">
        <h2>OpenClaw Gateway</h2>
        <div class="fields">
          <div class="field"><label>OPENCLAW_URL <span class="current" data-current="OPENCLAW_URL"></span></label><input name="OPENCLAW_URL"></div>
          <div class="field"><label>OPENCLAW_MODEL <span class="current" data-current="OPENCLAW_MODEL"></span></label><input name="OPENCLAW_MODEL"></div>
          <div class="field"><label>OPENCLAW_SESSION_KEY <span class="current" data-current="OPENCLAW_SESSION_KEY"></span></label><input name="OPENCLAW_SESSION_KEY"><div class="hint">Default leer = <code>openhavoice:&lt;device-name&gt;</code></div></div>
          <div class="field"><label>OPENCLAW_MESSAGE_CHANNEL <span class="current" data-current="OPENCLAW_MESSAGE_CHANNEL"></span></label><input name="OPENCLAW_MESSAGE_CHANNEL"></div>
          <div class="field"><label>OPENCLAW_TOKEN <span class="current" data-current="OPENCLAW_TOKEN"></span></label><input name="OPENCLAW_TOKEN" type="password" placeholder="leave blank to keep existing"><div class="hint">Default: leer · Secret wird nie angezeigt.</div></div>
          <div class="field"><label>CONFIG_ADMIN_TOKEN <span class="current" data-current="CONFIG_ADMIN_TOKEN"></span></label><input name="CONFIG_ADMIN_TOKEN" type="password" placeholder="leave blank to keep existing"><div class="hint">Default: leer = /config nur localhost.</div></div>
          <div class="field full"><label>OPENCLAW_VOICE_SYSTEM_PROMPT <span class="current" data-current="OPENCLAW_VOICE_SYSTEM_PROMPT"></span></label><textarea name="OPENCLAW_VOICE_SYSTEM_PROMPT"></textarea></div>
        </div>
      </section>

      <section class="card">
        <h2>Reconnect</h2>
        <div class="fields">
          <div class="field"><label>RECONNECT_INITIAL_SECONDS <span class="current" data-current="RECONNECT_INITIAL_SECONDS"></span></label><input name="RECONNECT_INITIAL_SECONDS" type="number" step="0.1"></div>
          <div class="field"><label>RECONNECT_MAX_SECONDS <span class="current" data-current="RECONNECT_MAX_SECONDS"></span></label><input name="RECONNECT_MAX_SECONDS" type="number" step="0.1"></div>
        </div>
      </section>
    </div>
    <div class="actions">
      <button class="secondary" type="button" id="reload-btn">Reload from file</button>
      <button type="submit">Save Configuration</button>
    </div>
  </form>
</main>
<script>
const DEFAULTS = {defaults_json};
const SECRET_FIELDS = new Set(['VOICE_PSK', 'VOICE_PASSWORD', 'VOICE_DEVICES', 'OPENCLAW_TOKEN', 'CONFIG_ADMIN_TOKEN']);
const fieldNames = Object.keys(DEFAULTS).map(k => k.toUpperCase());
function msg(text, kind='ok') {{ const el = document.getElementById('msg'); el.className = 'msg ' + kind; el.textContent = text; }}
function headers(extra={{}}) {{ return extra; }}
function applyDefaultHints() {{
  for (const name of fieldNames) {{
    const input = document.querySelector(`[name="${{name}}"]`);
    if (!input) continue;
    const key = name.toLowerCase();
    const def = DEFAULTS[key];
    if (!input.parentElement.querySelector('.hint')) {{
      const h = document.createElement('div'); h.className = 'hint'; h.innerHTML = `Default: <code>${{def === '' ? 'leer' : String(def)}}</code>`; input.after(h);
    }}
  }}
}}
function fillForm(cfg) {{
  const form = document.getElementById('config-form');
  for (const [key, val] of Object.entries(cfg)) {{
    const name = key.toUpperCase();
    const el = form.elements[name];
    const current = document.querySelector(`[data-current="${{name}}"]`);
    const display = val === '****' ? 'configured' : (val === '' || val == null ? 'empty' : String(val));
    if (current) current.textContent = display;
    if (el && val !== '****') el.value = val ?? '';
    if (el && val === '****') el.value = '';
  }}
}}
async function loadConfig() {{
  applyDefaultHints();
  const r = await fetch('/config', {{ headers: headers() }});
  const data = await r.json().catch(() => ({{error: 'Invalid response'}}));
  if (!r.ok) {{ msg('✗ ' + (data.details || data.error || 'Config load failed'), 'err'); return; }}
  fillForm(data);
}}
async function saveConfig(e) {{
  e.preventDefault();
  const form = e.target; const data = {{}};
  for (const el of form.elements) if (el.name) data[el.name] = el.value;
  const r = await fetch('/config', {{ method: 'PUT', headers: headers({{'Content-Type':'application/json'}}), body: JSON.stringify(data) }});
  const result = await r.json().catch(() => ({{error: 'Invalid response'}}));
  if (!r.ok) {{ msg('✗ ' + (result.details?.join?.('; ') || result.details || result.error || 'Save failed'), 'err'); return; }}
  fillForm(result.config || {{}}); msg('✓ Saved. ' + (result.updated?.length ? 'Updated: ' + result.updated.join(', ') : 'No changes.'));
}}
async function reloadConfig() {{
  const r = await fetch('/config/reload', {{ method: 'POST', headers: headers() }});
  const result = await r.json().catch(() => ({{error: 'Invalid response'}}));
  if (!r.ok) {{ msg('✗ ' + (result.error || 'Reload failed'), 'err'); return; }}
  msg('✓ Reloaded from file'); await loadConfig();
}}
let voiceDevices = [];
function fillDeviceMenu(activeHost='') {{
  const sel = document.getElementById('voice-device-select');
  sel.innerHTML = '<option value="">Manual / new device…</option>';
  for (const d of voiceDevices) {{
    const opt = document.createElement('option');
    opt.value = d.host; opt.textContent = `${{d.name || d.host}} (${{d.host}})`;
    if (d.host === activeHost) opt.selected = true;
    sel.appendChild(opt);
  }}
}}
function fillDeviceFields(host) {{
  const d = voiceDevices.find(x => x.host === host);
  document.getElementById('voice-device-name').value = d?.name || '';
  document.getElementById('voice-device-host').value = d?.host || '';
  document.getElementById('voice-device-psk').value = '';
  document.getElementById('voice-device-password').value = '';
}}
async function loadDevices() {{
  const r = await fetch('/api/devices');
  const data = await r.json().catch(() => ({{devices: []}}));
  if (!r.ok) return;
  voiceDevices = data.devices || [];
  fillDeviceMenu(data.active_host || '');
  fillDeviceFields(document.getElementById('voice-device-select').value);
}}
async function saveVoiceDevice(activate=false) {{
  const payload = {{
    name: document.getElementById('voice-device-name').value,
    host: document.getElementById('voice-device-host').value,
    psk: document.getElementById('voice-device-psk').value,
    password: document.getElementById('voice-device-password').value,
    activate,
  }};
  const r = await fetch('/api/devices', {{ method: 'PUT', headers: headers({{'Content-Type':'application/json'}}), body: JSON.stringify(payload) }});
  const result = await r.json().catch(() => ({{error: 'Invalid response'}}));
  if (!r.ok) {{ msg('✗ ' + (result.error || 'Device save failed'), 'err'); return; }}
  voiceDevices = result.devices || []; fillDeviceMenu(result.active_host || payload.host); fillDeviceFields(payload.host);
  await loadConfig();
  msg('✓ Device saved' + (result.restart_required ? '. Restart relay to switch connection.' : '.'));
}}
function formatDateTime(ts) {{
  return new Date(ts * 1000).toLocaleString([], {{ dateStyle: 'short', timeStyle: 'medium' }});
}}
function formatDuration(seconds) {{
  if (seconds == null) return '—';
  seconds = Math.max(0, Math.floor(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const sec = seconds % 60;
  if (h > 0) return `${{h}}h ${{m}}m`;
  if (m > 0) return `${{m}}m ${{sec}}s`;
  return `${{sec}}s`;
}}
async function updateDeviceOverview() {{
  try {{
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('dv-device').textContent = s.device_name || '—';
    document.getElementById('dv-status').textContent = s.connected ? 'Connected' : 'Disconnected';
    document.getElementById('dv-status').style.color = s.connected ? 'var(--green)' : 'var(--red)';
    document.getElementById('dv-sessions').textContent = s.total_sessions || 0;
    document.getElementById('svc-start').textContent = s.service_started_at
      ? formatDateTime(s.service_started_at)
      : '—';
    document.getElementById('svc-uptime').textContent = formatDuration(s.service_uptime_sec);
    document.getElementById('dv-in-at').textContent = s.last_in_at
      ? new Date(s.last_in_at * 1000).toLocaleTimeString()
      : '—';
    document.getElementById('dv-in-duration').textContent = s.last_in_duration_sec != null
      ? s.last_in_duration_sec.toFixed(1) + 's'
      : '—';
    document.getElementById('dv-out-at').textContent = s.last_out_at
      ? new Date(s.last_out_at * 1000).toLocaleTimeString()
      : '—';
    document.getElementById('dv-out-duration').textContent = s.last_out_duration_sec != null
      ? s.last_out_duration_sec.toFixed(1) + 's'
      : '—';
    document.getElementById('dv-turn-duration').textContent = s.last_turn_duration_sec != null
      ? s.last_turn_duration_sec.toFixed(1) + 's'
      : '—';
    document.getElementById('dv-stop').textContent = s.last_stop_reason || '—';
  }} catch {{ /* ignore */ }}
}}
setInterval(updateDeviceOverview, 3000);
updateDeviceOverview();

document.getElementById('reload-btn').addEventListener('click', reloadConfig);
document.getElementById('config-form').addEventListener('submit', saveConfig);
document.getElementById('voice-device-select').addEventListener('change', e => fillDeviceFields(e.target.value));
document.getElementById('voice-device-save').addEventListener('click', () => saveVoiceDevice(false));
document.getElementById('voice-device-activate').addEventListener('click', () => saveVoiceDevice(true));
applyDefaultHints();
loadConfig();
loadDevices();
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


if __name__ == "__main__":
    asyncio.run(main())
