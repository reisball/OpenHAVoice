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
DEVICE_STATUS: dict[str, dict[str, object]] = {}

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
DEVICE_STATS: dict[str, dict[str, object]] = {}
CURRENT_DEVICE_HOST: str | None = None
CONNECTION_TASKS: list[asyncio.Task] = []


def _new_device_stats(name: str | None = None) -> dict[str, object]:
    return {
        "total_sessions": 0,
        "last_session_at": None,
        "last_stop_reason": None,
        "last_session_duration_sec": None,
        "last_in_at": None,
        "last_in_duration_sec": None,
        "last_in_turn_duration_sec": None,
        "last_openclaw_turn_duration_sec": None,
        "last_out_at": None,
        "last_out_duration_sec": None,
        "last_out_turn_duration_sec": None,
        "last_turn_duration_sec": None,
        "turn_history": [],
        "device_name": name,
        "connected": False,
    }


def _device_stats(host: str | None = None) -> dict[str, object] | None:
    host = host or CURRENT_DEVICE_HOST
    if not host:
        return None
    name = str(DEVICE_STATUS.get(host, {}).get("name") or host)
    stats = DEVICE_STATS.setdefault(host, _new_device_stats(name))
    stats.setdefault("device_name", name)
    return stats


def _set_stat(key: str, value: object, host: str | None = None) -> None:
    STATS[key] = value
    stats = _device_stats(host)
    if stats is not None:
        stats[key] = value


def _record_turn_history(duration_sec: float, host: str | None = None) -> None:
    entry = {"timestamp": time.time(), "duration_sec": round(duration_sec, 1)}
    history = list(STATS.get("turn_history") or [])
    history.append(entry)
    STATS["turn_history"] = history[-10:]
    stats = _device_stats(host)
    if stats is not None:
        device_history = list(stats.get("turn_history") or [])
        device_history.append(entry)
        stats["turn_history"] = device_history[-10:]

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
    audio = response.content
    duration = wav_duration_seconds(audio)
    if duration is None or duration <= 0:
        raise RuntimeError(
            f"Orpheus returned invalid/empty WAV: bytes={len(audio)} duration={duration}"
        )
    return audio


def session_key_for_device(device_name: str | None = None) -> str:
    configured = CFG.openclaw_session_key.strip()
    multi_device = len(_enabled_connection_devices()) > 1 if CFG is not None else False
    if configured and not multi_device:
        return configured
    suffix = (device_name or CFG.voice_host).strip()
    safe = "".join(ch if ch.isalnum() or ch in "._:-" else "-" for ch in suffix).strip("-")
    return f"openhavoice:{safe or 'voice-pe'}"


def openclaw_agent_model_value(agent: str) -> str:
    """Return the OpenClaw agent-target value expected by /v1/chat/completions."""
    value = (agent or "default").strip() or "default"
    lowered = value.lower()
    if lowered == "openclaw":
        return "openclaw/default"
    if lowered.startswith("openclaw/") or lowered.startswith("openclaw:") or lowered.startswith("agent:"):
        return value
    return f"openclaw/{value}"


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
        "model": openclaw_agent_model_value(CFG.openclaw_agent),
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
    devices = _load_voice_devices(reveal=False)
    for device in devices:
        host = device["host"]
        device.update(DEVICE_STATUS.get(host, {}))
        stats = dict(DEVICE_STATS.get(host, _new_device_stats(str(device.get("name") or host))))
        stats["device_name"] = str(device.get("name") or stats.get("device_name") or host)
        stats["connected"] = bool(device.get("connected"))
        device.update(stats)
    data["devices"] = devices
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
            "enabled": bool(item.get("enabled", True)),
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
    devices = _load_voice_devices(reveal=False)
    for device in devices:
        device.update(DEVICE_STATUS.get(device["host"], {}))
        device["active"] = device["host"] == CFG.voice_host
    return web.json_response({
        "active_host": CFG.voice_host,
        "devices": devices,
    })


async def devices_put(request: web.Request) -> web.Response:
    global CFG, LOCAL_IP
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    old_connection_signature = _connection_signature()
    action = str(body.get("action", "save")).strip().lower()
    host = str(body.get("host", "")).strip()
    original_host = str(body.get("original_host", "")).strip() or host
    if not host:
        return web.json_response({"error": "host is required"}, status=400)

    devices = _load_voice_devices(reveal=True)
    existing = next((d for d in devices if d.get("host") == original_host), None)
    if original_host != host and any(d.get("host") == host for d in devices if d is not existing):
        return web.json_response({"error": "another device already uses this host"}, status=400)

    if action == "delete":
        devices = [d for d in devices if d.get("host") != original_host]
        if CFG.voice_host == original_host:
            replacement = next((d for d in devices if d.get("enabled", True) and d.get("psk")), None)
            CFG.voice_host = replacement.get("host", "") if replacement else ""
            CFG.voice_psk = replacement.get("psk", "") if replacement else ""
            CFG.voice_password = replacement.get("password", "") if replacement else ""
        _save_voice_devices(devices)
        await restart_connection_tasks()
        return web.json_response({"ok": True, "active_host": CFG.voice_host, "devices": _load_voice_devices(reveal=False), "connection_restarted": True})

    if existing is None:
        psk = str(body.get("psk", "")).strip()
        if not psk:
            return web.json_response({"error": "psk is required for new devices"}, status=400)
        existing = {"name": host, "host": host, "psk": psk, "password": "", "enabled": True}
        devices.append(existing)

    name = str(body.get("name", "")).strip()
    psk = str(body.get("psk", "")).strip()
    password = str(body.get("password", "")).strip()
    activate = bool(body.get("activate", False))
    if host != existing.get("host"):
        if CFG.voice_host == existing.get("host"):
            CFG.voice_host = host
        existing["host"] = host
    if name:
        existing["name"] = name
    if psk and psk != "****":
        existing["psk"] = psk
    if password and password != "****":
        existing["password"] = password
    if "enabled" in body:
        existing["enabled"] = bool(body.get("enabled"))

    if activate:
        if not existing.get("psk"):
            return web.json_response({"error": "selected device has no psk"}, status=400)
        existing["enabled"] = True
        CFG.voice_host = existing["host"]
        CFG.voice_psk = existing["psk"]
        CFG.voice_password = existing.get("password", "")
        try:
            LOCAL_IP = local_ip_for(CFG.voice_host)
        except Exception:
            pass

    _save_voice_devices(devices)
    redacted = _load_voice_devices(reveal=False)
    for device in redacted:
        device.update(DEVICE_STATUS.get(device["host"], {}))
        device["active"] = device["host"] == CFG.voice_host
    restart_required = old_connection_signature != _connection_signature()
    restarted_devices: list[dict[str, str]] = []
    if restart_required:
        restarted_devices = await restart_connection_tasks()
    return web.json_response({
        "ok": True,
        "active_host": CFG.voice_host,
        "devices": redacted,
        "connection_restarted": restart_required,
        "active_connections": [device["host"] for device in restarted_devices],
    })

async def restart_handler(request: web.Request) -> web.Response:

    async def delayed_restart() -> None:
        await asyncio.sleep(0.5)
        proc = await asyncio.create_subprocess_shell("systemctl --user restart openhavoice-relay.service")
        await proc.communicate()

    asyncio.create_task(delayed_restart())
    return web.json_response({"ok": True, "message": "Relay restart scheduled"})


async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/tts/{token}.wav", tts_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/devices", devices_get)
    app.router.add_put("/api/devices", devices_put)
    app.router.add_post("/api/restart", restart_handler)

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


async def speak(text: str) -> float:
    assert CLIENT is not None
    tts_turn_start = time.time()
    loop = asyncio.get_running_loop()
    audio = await loop.run_in_executor(None, synthesize, text)
    token = uuid.uuid4().hex
    TTS_FILES[token] = audio
    port = CFG.tts_port
    url = f"http://{LOCAL_IP}:{port}/tts/{token}.wav"
    print(f"TTS_READY {url} bytes={len(audio)} text={text!r}", flush=True)
    CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_TTS_START, {"text": text})
    CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_TTS_END, {"url": url})
    last_tts_timestamp = time.time()
    _set_stat("last_tts_timestamp", last_tts_timestamp)
    _set_stat("last_out_at", last_tts_timestamp)
    tts_turn_duration = last_tts_timestamp - tts_turn_start
    _set_stat("last_out_turn_duration_sec", round(tts_turn_duration, 1))
    out_duration = wav_duration_seconds(audio)
    _set_stat("last_out_duration_sec", round(out_duration, 1) if out_duration is not None else None)
    return tts_turn_duration


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
        _set_stat("last_stop_reason", reason)
        _set_stat("last_session_at", time.time())
        _set_stat("last_session_duration_sec", round(duration, 1) if duration is not None else None)

    try:
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_VAD_END, None)
    except Exception as exc:  # noqa: BLE001 - probe script should keep running
        print(f"WARN vad_end failed {exc!r}", flush=True)

    if abort or size < 3200 or SPEECH_MS < CFG.min_speech_ms:
        if STARTED_AT is not None:
            _set_stat("last_turn_duration_sec", round(time.time() - STARTED_AT, 1))
        try:
            CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_RUN_END, None)
        except Exception:
            pass
        return

    wav_path = Path(__file__).with_name(f"roundtrip-{int(time.time())}.wav")
    write_wav(wav_path, bytes(BUFFER))
    print(f"WAV {wav_path}", flush=True)

    loop = asyncio.get_running_loop()
    stt_turn_duration: float | None = None
    openclaw_turn_duration: float | None = None
    tts_turn_duration: float | None = None
    try:
        stt_started_at = time.time()
        text = await loop.run_in_executor(None, transcribe, wav_path)
        stt_finished_at = time.time()
        stt_turn_duration = stt_finished_at - stt_started_at
        _set_stat("last_in_at", stt_finished_at)
        _set_stat("last_in_duration_sec", round(input_duration, 1) if input_duration is not None else None)
        _set_stat("last_in_turn_duration_sec", round(stt_turn_duration, 1))
        print(f"TRANSCRIPT {text!r}", flush=True)
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_END, {"text": text})
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_INTENT_START, None)
        CLIENT.send_voice_assistant_event(E.VOICE_ASSISTANT_INTENT_END, None)
        _set_stat("last_stt_text", text)

        openclaw_started_at = time.time()
        response_text = await loop.run_in_executor(None, openclaw_chat, text)
        openclaw_finished_at = time.time()
        openclaw_turn_duration = openclaw_finished_at - openclaw_started_at
        _set_stat("last_openclaw_turn_duration_sec", round(openclaw_turn_duration, 1))
        _set_stat("last_llm_text", response_text)
        print(f"OPENCLAW_REPLY {response_text!r}", flush=True)
        tts_turn_duration = await speak(response_text)
        post_tts_grace_seconds = CFG.tts_post_playback_grace_seconds
        if post_tts_grace_seconds > 0:
            await asyncio.sleep(post_tts_grace_seconds)
    except Exception as exc:  # noqa: BLE001
        print(f"ROUNDTRIP_FAILED {exc!r}", flush=True)
    finally:
        if STARTED_AT is not None:
            component_durations = [input_duration, stt_turn_duration, openclaw_turn_duration, tts_turn_duration]
            if all(value is not None for value in component_durations):
                total_turn_duration = round(sum(round(float(value), 1) for value in component_durations if value is not None), 1)
            else:
                total_turn_duration = round(time.time() - STARTED_AT, 1)
            _set_stat("last_turn_duration_sec", total_turn_duration)
            _record_turn_history(total_turn_duration)
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
    stats = _device_stats()
    if stats is not None:
        stats["total_sessions"] = int(stats.get("total_sessions") or 0) + 1
        stats["current_session_started_at"] = STARTED_AT
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


async def run_connected(device: dict[str, str]) -> None:
    """Hold one long-lived ESPHome API connection until it fails or is cancelled."""
    global CLIENT, STARTED_AT, STOPPING, CURRENT_DEVICE_HOST
    host = device["host"]
    psk = device["psk"]
    password = device.get("password", "")
    name_hint = device.get("name") or host
    DEVICE_STATUS[host] = {"online": False, "connected": False, "status": "connecting", "name": name_hint}

    client = APIClient(host, 6053, password, noise_psk=psk)
    unsubscribe = None

    def mark_current_device() -> None:
        global CLIENT, CURRENT_DEVICE_HOST
        CLIENT = client
        CURRENT_DEVICE_HOST = host
        os.environ["OPENHAVOICE_DEVICE_NAME"] = str(DEVICE_STATUS.get(host, {}).get("name") or name_hint)

    async def wrap_start(conversation_id, flags, audio_settings, wake_word_phrase):
        mark_current_device()
        return await on_start(conversation_id, flags, audio_settings, wake_word_phrase)

    async def wrap_stop(abort: bool):
        mark_current_device()
        await on_stop(abort)

    async def wrap_audio(data: bytes):
        mark_current_device()
        await on_audio(data)

    try:
        await client.connect(login=True)
        info = await client.device_info()
        os.environ["OPENHAVOICE_DEVICE_NAME"] = info.name
        print(
            f"CONNECTED {info.name} @ {host}. Backend client is active; "
            f"session={session_key_for_device(info.name)!r}; press button and speak.",
            flush=True,
        )
        DEVICE_STATUS[host] = {
            "online": True,
            "connected": True,
            "status": "connected",
            "name": info.name,
            "mac": getattr(info, "mac_address", ""),
            "last_seen_at": time.time(),
        }
        DEVICE_STATS.setdefault(host, _new_device_stats(info.name))["device_name"] = info.name
        _set_stat("device_name", info.name, host)
        _set_stat("connected", True, host)
        unsubscribe = client.subscribe_voice_assistant(
            handle_start=wrap_start,
            handle_stop=wrap_stop,
            handle_audio=wrap_audio,
        )

        max_capture_seconds = CFG.max_capture_seconds
        last_probe = 0.0
        while True:
            await asyncio.sleep(0.2)
            if STARTED_AT and not STOPPING and time.time() - STARTED_AT > max_capture_seconds:
                mark_current_device()
                await finish("timeout")
            if time.time() - last_probe > 5:
                # Force stale TCP/ESPHome connections to notice unplugged/offline devices.
                await client.device_info()
                DEVICE_STATUS.setdefault(host, {})["last_seen_at"] = time.time()
                last_probe = time.time()
    finally:
        if unsubscribe:
            unsubscribe()
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001 - reconnect loop handles recovery
            print(f"WARN disconnect failed {host} {exc!r}", flush=True)
        if CLIENT is client:
            CLIENT = None
        STARTED_AT = None
        STOPPING = False
        DEVICE_STATUS.setdefault(host, {}).update({"online": False, "connected": False, "status": "offline"})
        stats = _device_stats(host)
        if stats is not None:
            stats["connected"] = False
        if STATS.get("device_name") == DEVICE_STATUS.get(host, {}).get("name"):
            STATS["connected"] = any(bool(v.get("connected")) for v in DEVICE_STATUS.values())


async def connection_loop(device: dict[str, str]) -> None:
    """Reconnect forever so Voice PE devices keep a backend client instead of going red."""
    reconnect_delay = CFG.reconnect_initial_seconds
    reconnect_max = CFG.reconnect_max_seconds
    host = device["host"]

    while True:
        try:
            await run_connected(device)
            reconnect_delay = CFG.reconnect_initial_seconds
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate service-style retry loop
            DEVICE_STATUS.setdefault(host, {}).update({"online": False, "connected": False, "status": "offline", "error": str(exc)})
            print(f"DISCONNECTED {host} {type(exc).__name__}: {exc}; retrying in {reconnect_delay:.1f}s", flush=True)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, reconnect_max)


def _enabled_connection_devices() -> list[dict[str, str]]:
    devices = [d for d in _load_voice_devices(reveal=True) if d.get("enabled", True) and d.get("psk")]
    if not devices and CFG.voice_host and CFG.voice_psk:
        devices = [{"name": CFG.voice_host, "host": CFG.voice_host, "psk": CFG.voice_psk, "password": CFG.voice_password, "enabled": True}]
    return devices


def _connection_signature(devices: list[dict[str, str]] | None = None) -> tuple[tuple[str, str, str, str, bool], ...]:
    """Return the connection-affecting device config in a comparable form."""
    selected = devices if devices is not None else _enabled_connection_devices()
    return tuple(
        (
            str(device.get("name", "")),
            str(device.get("host", "")),
            str(device.get("psk", "")),
            str(device.get("password", "")),
            bool(device.get("enabled", True)),
        )
        for device in selected
    )


def _mark_all_devices_restarting() -> None:
    for status in DEVICE_STATUS.values():
        status.update({"online": False, "connected": False, "status": "restarting"})
    for stats in DEVICE_STATS.values():
        stats["connected"] = False
    STATS["connected"] = False


async def stop_connection_tasks() -> None:
    """Cancel all active Voice PE connection loops."""
    global CONNECTION_TASKS
    if not CONNECTION_TASKS:
        return
    for task in CONNECTION_TASKS:
        task.cancel()
    await asyncio.gather(*CONNECTION_TASKS, return_exceptions=True)
    CONNECTION_TASKS = []


async def start_connection_tasks() -> list[dict[str, str]]:
    """Start connection loops for the currently enabled Voice PE devices."""
    global CONNECTION_TASKS, LOCAL_IP
    devices = _enabled_connection_devices()
    if not devices:
        CONNECTION_TASKS = []
        return []
    LOCAL_IP = local_ip_for(devices[0]["host"])
    CONNECTION_TASKS = [asyncio.create_task(connection_loop(device)) for device in devices]
    return devices


async def restart_connection_tasks() -> list[dict[str, str]]:
    """Apply changed Voice PE connection config without a manual service restart."""
    _mark_all_devices_restarting()
    await stop_connection_tasks()
    return await start_connection_tasks()


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

    devices = _enabled_connection_devices()
    if not devices:
        raise SystemExit("No enabled Voice PE devices configured")
    LOCAL_IP = local_ip_for(devices[0]["host"])

    runner = await start_http_server()
    await start_connection_tasks()
    try:
        await asyncio.Future()
    finally:
        await stop_connection_tasks()
        await runner.cleanup()


# ── Config Web UI handlers ───────────────────────────────────


async def config_get(request: web.Request) -> web.Response:
    """GET /config — return current config as JSON, always redacting secrets."""
    return web.json_response(CFG.to_dict(reveal=False))


async def config_put(request: web.Request) -> web.Response:
    """PUT /config — update one or more config fields."""
    global CFG
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    old_connection_signature = _connection_signature()
    candidate = copy.deepcopy(CFG)
    updated: list[str] = []
    errors: list[str] = []
    secret_fields = {"VOICE_PSK", "VOICE_PASSWORD", "VOICE_DEVICES", "OPENCLAW_TOKEN"}
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
    restart_required = old_connection_signature != _connection_signature()
    restarted_devices: list[dict[str, str]] = []
    if restart_required:
        restarted_devices = await restart_connection_tasks()
    return web.json_response({
        "ok": True,
        "updated": updated,
        "config": CFG.to_dict(reveal=False),
        "connection_restarted": restart_required,
        "devices": _load_voice_devices(reveal=False),
        "active_connections": [device["host"] for device in restarted_devices],
    })


async def config_validate(request: web.Request) -> web.Response:
    """POST /config/validate — validate current config without saving."""
    errors = CFG.validate()
    return web.json_response({"valid": len(errors) == 0, "errors": errors})


async def config_reload(request: web.Request) -> web.Response:
    """POST /config/reload — reload config from env file."""
    try:
        global CFG
        old_connection_signature = _connection_signature()
        CFG = load_config()
        VAD.set_mode(CFG.vad_aggressiveness)
        errors = CFG.validate()
        restart_required = not errors and old_connection_signature != _connection_signature()
        restarted_devices: list[dict[str, str]] = []
        if restart_required:
            restarted_devices = await restart_connection_tasks()
        return web.json_response({
            "ok": True,
            "message": "Config reloaded." + (" Voice PE connections restarted." if restart_required else ""),
            "connection_restarted": restart_required,
            "active_connections": [device["host"] for device in restarted_devices],
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
  .config-space-title {{ grid-column: 1 / -1; border-top: 1px solid var(--line); padding-top: 18px; margin-top: 10px; color: var(--blue); font-size: 1rem; font-weight: 800; letter-spacing: .02em; }}
  .config-space-title:first-child {{ border-top: 0; margin-top: 0; padding-top: 0; }}
  #service-card, #device-card {{ margin-bottom: 14px; }}
  h2 {{ color: var(--orange); font-size: .85rem; text-transform: uppercase; letter-spacing: .08em; margin: 0 0 14px; }}
  .fields {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
  .field.full {{ grid-column: 1 / -1; }}
  .field.spacer {{ visibility: hidden; }}
  .section-divider {{ grid-column: 1 / -1; border-top: 1px solid var(--line); margin: 4px 0 2px; padding-top: 12px; color: var(--orange); font-size: .72rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
  label {{ display: flex; justify-content: space-between; gap: 8px; color: var(--muted); font-size: .76rem; font-weight: 700; letter-spacing: .04em; margin-bottom: 5px; }}
  .current {{ color: var(--blue); font-weight: 600; max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-transform: none; letter-spacing: 0; }}
  input, textarea, select {{ width: 100%; padding: 9px 10px; background: #0d1117; border: 1px solid var(--line); border-radius: 8px; color: var(--text); font: inherit; }}
  input[type="checkbox"] {{ width: auto; padding: 0; margin: 0; accent-color: var(--green); transform: scale(1.05); }}
  input:focus, textarea:focus, select:focus {{ outline: none; border-color: var(--blue); box-shadow: 0 0 0 2px #58a6ff33; }}
  textarea {{ resize: vertical; min-height: 82px; }}
  .hint {{ color: var(--muted); font-size: .76rem; margin-top: 4px; }}
  .check-label {{ justify-content: flex-start; align-items: center; gap: 8px; margin-bottom: 4px; }}
  #voice-device-status {{ max-width: 100%; white-space: normal; overflow: visible; text-overflow: clip; }}
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
  .device-list {{ display: grid; gap: 10px; }}
  .device-status-list {{ display: grid; gap: 16px; }}
  .device-status-section {{ border-top: 1px solid var(--line); padding-top: 14px; }}
  .device-status-section:first-child {{ border-top: 0; padding-top: 0; }}
  .device-status-fields {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
  .device-status-fields .current {{ max-width: 100%; font-size: 1rem; }}
  .mini-table {{ width: 100%; border-collapse: collapse; margin-top: 2px; color: var(--text); }}
  .mini-table th, .mini-table td {{ border-top: 1px solid var(--line); padding: 7px 0; text-align: left; }}
  .mini-table th {{ color: var(--muted); font-size: .76rem; font-weight: 700; letter-spacing: .04em; }}
  .mini-table td:last-child, .mini-table th:last-child {{ text-align: right; color: var(--blue); }}
  .device-row {{ border: 1px solid var(--line); border-radius: 12px; padding: 12px; background: #0d1117; display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; }}
  .device-title {{ font-weight: 800; color: var(--text); }}
  .device-meta {{ color: var(--muted); font-size: .82rem; margin-top: 3px; }}
  .device-actions {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }}
  .status-pill {{ display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: .72rem; font-weight: 800; margin-left: 8px; }}
  .status-pill.on {{ background: var(--green-bg); color: var(--green); }}
  .status-pill.off {{ background: var(--red-bg); color: var(--red); }}
  .dialog-backdrop {{ position: fixed; inset: 0; background: #0008; display: none; align-items: center; justify-content: center; padding: 16px; z-index: 10; }}
  .dialog-backdrop.open {{ display: flex; }}
  .dialog {{ width: min(560px, 100%); background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 16px; }}
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
      <div class="field" style="align-items:flex-end; justify-content:flex-end;"><button class="secondary" type="button" id="relay-restart">Restart relay</button></div>
    </div>
  </div>

  <!-- Device Overview Dashboard -->
  <div class="card wide" id="device-card">
    <h2>Device Status</h2>
    <div id="device-overview" class="device-status-list"></div>
  </div>

  <div id="msg" class="msg"></div>

  <form id="config-form">
    <div class="grid">
      <div class="config-space-title">Voice PE Devices</div>
      <section class="card">
        <h2>Device Connection</h2>
        <div class="fields">
          <div class="field full"><label>Device</label><select id="voice-device-select"></select></div>
          <div class="field"><label>Device Name</label><input id="voice-device-name" placeholder="home-assistant-voice-…"></div>
          <div class="field"><label>Host / IP</label><input id="voice-device-host" placeholder="192.168.50.xxx"></div>
          <div class="field"><label>Noise PSK</label><input id="voice-device-psk" type="password" placeholder="leave blank to keep existing"></div>
          <div class="field"><label>Password</label><input id="voice-device-password" type="password" placeholder="optional / leave blank"></div>
          <div class="field full"><label>Status</label><span id="voice-device-status" class="current">—</span></div>
        </div>
        <div class="actions">
          <button class="secondary" type="button" id="voice-device-add">Add new device</button>
          <button type="button" id="voice-device-save">Save</button>
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

      <section class="card">
        <h2>Reconnect</h2>
        <div class="fields">
          <div class="field"><label>RECONNECT_INITIAL_SECONDS <span class="current" data-current="RECONNECT_INITIAL_SECONDS"></span></label><input name="RECONNECT_INITIAL_SECONDS" type="number" step="0.1"></div>
          <div class="field"><label>RECONNECT_MAX_SECONDS <span class="current" data-current="RECONNECT_MAX_SECONDS"></span></label><input name="RECONNECT_MAX_SECONDS" type="number" step="0.1"></div>
        </div>
      </section>

      <div class="config-space-title">Local Services</div>
      <section class="card">
        <h2>STT / TTS Services</h2>
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

      <div class="config-space-title">OpenClaw Config</div>
      <section class="card wide">
        <h2>OpenClaw Gateway</h2>
        <div class="fields">
          <div class="field"><label>OPENCLAW_URL <span class="current" data-current="OPENCLAW_URL"></span></label><input name="OPENCLAW_URL"></div>
          <div class="field"><label>OPENCLAW_AGENT <span class="current" data-current="OPENCLAW_AGENT"></span></label><input name="OPENCLAW_AGENT"><div class="hint">Nur Agentname eingeben, z.B. <code>default</code> oder ein konfigurierter Agent.</div></div>
          <div class="field"><label>OPENCLAW_SESSION_KEY <span class="current" data-current="OPENCLAW_SESSION_KEY"></span></label><input name="OPENCLAW_SESSION_KEY"><div class="hint">Default leer = <code>openhavoice:&lt;device-name&gt;</code></div></div>
          <div class="field"><label>OPENCLAW_MESSAGE_CHANNEL <span class="current" data-current="OPENCLAW_MESSAGE_CHANNEL"></span></label><input name="OPENCLAW_MESSAGE_CHANNEL"></div>
          <div class="field"><label>OPENCLAW_TOKEN <span class="current" data-current="OPENCLAW_TOKEN"></span></label><input name="OPENCLAW_TOKEN" type="password" placeholder="leave blank to keep existing"><div class="hint">Default: leer · Secret wird nie angezeigt.</div></div>
          <div class="field full"><label>OPENCLAW_VOICE_SYSTEM_PROMPT <span class="current" data-current="OPENCLAW_VOICE_SYSTEM_PROMPT"></span></label><textarea name="OPENCLAW_VOICE_SYSTEM_PROMPT"></textarea></div>
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
const SECRET_FIELDS = new Set(['VOICE_PSK', 'VOICE_PASSWORD', 'VOICE_DEVICES', 'OPENCLAW_TOKEN']);
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
  fillForm(result.config || {{}}); msg('✓ Saved. ' + (result.updated?.length ? 'Updated: ' + result.updated.join(', ') : 'No changes.') + (result.connection_restarted ? ' Voice PE connections restarted.' : ''));
}}
async function reloadConfig() {{
  const r = await fetch('/config/reload', {{ method: 'POST', headers: headers() }});
  const result = await r.json().catch(() => ({{error: 'Invalid response'}}));
  if (!r.ok) {{ msg('✗ ' + (result.error || 'Reload failed'), 'err'); return; }}
  msg('✓ Reloaded from file' + (result.connection_restarted ? '. Voice PE connections restarted.' : '')); await loadConfig();
}}
let voiceDevices = [];
let selectedDeviceHost = '';
function deviceStatusLabel(d) {{
  if (!d) return ['—', ''];
  if (!d.enabled) return ['Disabled', 'off'];
  if (d.connected || d.online) return ['Online', 'on'];
  return ['Offline', 'off'];
}}
function renderDevices() {{
  const select = document.getElementById('voice-device-select');
  select.innerHTML = '';
  if (!voiceDevices.length) {{
    select.innerHTML = '<option value="">No devices configured</option>';
    selectedDeviceHost = '';
    fillSelectedDevice(null);
    return;
  }}
  for (const d of voiceDevices) {{
    const opt = document.createElement('option');
    opt.value = d.host;
    opt.textContent = `${{d.name || d.host}} (${{d.host}})`;
    select.appendChild(opt);
  }}
  if (!voiceDevices.some(d => d.host === selectedDeviceHost)) selectedDeviceHost = voiceDevices[0].host;
  select.value = selectedDeviceHost;
  fillSelectedDevice(voiceDevices.find(d => d.host === selectedDeviceHost));
}}
function fillSelectedDevice(d) {{
  document.getElementById('voice-device-name').value = d?.name || '';
  document.getElementById('voice-device-host').value = d?.host || '';
  document.getElementById('voice-device-psk').value = '';
  document.getElementById('voice-device-password').value = '';
  const [label, cls] = deviceStatusLabel(d);
  const status = document.getElementById('voice-device-status');
  status.textContent = d ? `${{label}} · PSK ${{d.psk ? 'configured' : 'missing'}} · Password ${{d.password ? 'configured' : 'empty'}}` : '—';
  status.style.color = cls === 'on' ? 'var(--green)' : (cls === 'off' ? 'var(--red)' : '');
}}
function openDeviceDialog() {{
  selectedDeviceHost = '';
  document.getElementById('voice-device-select').value = '';
  fillSelectedDevice(null);
  document.getElementById('voice-device-name').focus();
}}
async function loadDevices() {{
  const r = await fetch('/api/devices');
  const data = await r.json().catch(() => ({{devices: []}}));
  if (!r.ok) return;
  voiceDevices = data.devices || [];
  renderDevices();
}}
async function putDevice(payload) {{
  const r = await fetch('/api/devices', {{ method: 'PUT', headers: headers({{'Content-Type':'application/json'}}), body: JSON.stringify(payload) }});
  const result = await r.json().catch(() => ({{error: 'Invalid response'}}));
  if (!r.ok) {{ msg('✗ ' + (result.error || 'Device action failed'), 'err'); return null; }}
  voiceDevices = result.devices || [];
  selectedDeviceHost = payload.host || selectedDeviceHost;
  renderDevices();
  await loadConfig();
  return result;
}}
async function saveVoiceDevice() {{
  const host = document.getElementById('voice-device-host').value.trim();
  const result = await putDevice({{
    original_host: selectedDeviceHost || host,
    name: document.getElementById('voice-device-name').value.trim(),
    host,
    psk: document.getElementById('voice-device-psk').value,
    password: document.getElementById('voice-device-password').value,
  }});
  if (result) msg('✓ Device saved' + (result.connection_restarted ? '. Voice PE connections restarted.' : ''));
}}
async function restartRelay() {{
  const r = await fetch('/api/restart', {{ method: 'POST', headers: headers() }});
  const result = await r.json().catch(() => ({{error: 'Invalid response'}}));
  if (!r.ok) {{ msg('✗ ' + (result.error || 'Restart failed'), 'err'); return; }}
  msg('✓ Relay restart scheduled. Reconnect in a few seconds.');
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
function deviceField(label, value, color='') {{
  const style = color ? ` style="color:${{color}}"` : '';
  return `<div class="field"><label>${{label}}</label><span class="current"${{style}}>${{value}}</span></div>`;
}}
function formatTime(ts) {{
  return ts ? new Date(ts * 1000).toLocaleTimeString() : '—';
}}
function formatSeconds(value) {{
  return value != null ? Number(value).toFixed(1) + 's' : '—';
}}
function renderTurnHistory(history) {{
  if (!history || !history.length) return '<div class="hint">No completed turns yet.</div>';
  const rows = history.slice().reverse().map((turn) => `
    <tr>
      <td>${{formatDateTime(turn.timestamp)}}</td>
      <td>${{formatSeconds(turn.duration_sec)}}</td>
    </tr>`).join('');
  return `<table class="mini-table"><thead><tr><th>Date / Time</th><th>Total Turn Duration</th></tr></thead><tbody>${{rows}}</tbody></table>`;
}}
function renderDeviceStatus(devices) {{
  const root = document.getElementById('device-overview');
  if (!devices || !devices.length) {{
    root.innerHTML = '<div class="hint">No devices configured.</div>';
    return;
  }}
  root.innerHTML = devices.map((d) => {{
    const connected = !!d.connected;
    const status = connected ? 'Connected' : 'Disconnected';
    const color = connected ? 'var(--green)' : 'var(--red)';
    return `<div class="device-status-section"><div class="device-status-fields">
      ${{deviceField('Device', d.device_name || d.name || d.host || '—')}}
      ${{deviceField('Status', status, color)}}
      ${{deviceField('Sessions', d.total_sessions || 0)}}
      ${{deviceField('Last Stop', d.last_stop_reason || '—')}}
      <div class="field spacer" aria-hidden="true"></div>
      <div style="grid-column:1/-1;border-top:1px solid var(--border);padding:4px 0;font-weight:600;color:var(--text);">Last In — STT</div>
      ${{deviceField('Timestamp', formatTime(d.last_in_at))}}
      ${{deviceField('Message Length', formatSeconds(d.last_in_duration_sec))}}
      ${{deviceField('Turn Duration', formatSeconds(d.last_in_turn_duration_sec))}}
      <div class="field spacer" aria-hidden="true"></div>
      <div style="grid-column:1/-1;border-top:1px solid var(--border);padding:4px 0;font-weight:600;color:var(--text);">Last Out — TTS</div>
      ${{deviceField('Timestamp', formatTime(d.last_out_at))}}
      ${{deviceField('Message Length', formatSeconds(d.last_out_duration_sec))}}
      ${{deviceField('Turn Duration', formatSeconds(d.last_out_turn_duration_sec))}}
      <div class="field spacer" aria-hidden="true"></div>
      <div style="grid-column:1/-1;border-top:1px solid var(--border);padding:4px 0;font-weight:600;color:var(--text);">OpenClaw Turn Duration</div>
      ${{deviceField('Turn Duration', formatSeconds(d.last_openclaw_turn_duration_sec))}}
      <div class="field spacer" aria-hidden="true"></div>
      <div class="field spacer" aria-hidden="true"></div>
      <div style="grid-column:1/-1;border-top:1px solid var(--border);padding:4px 0;font-weight:600;color:var(--text);">Total Turn Duration</div>
      ${{deviceField('Last Total', formatSeconds(d.last_turn_duration_sec))}}
      <div style="grid-column:1/-1">${{renderTurnHistory(d.turn_history)}}</div>
    </div></div>`;
  }}).join('');
}}
async function updateDeviceOverview() {{
  try {{
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('svc-start').textContent = s.service_started_at
      ? formatDateTime(s.service_started_at)
      : '—';
    document.getElementById('svc-uptime').textContent = formatDuration(s.service_uptime_sec);
    renderDeviceStatus(s.devices || []);
  }} catch {{ /* ignore */ }}
}}
setInterval(updateDeviceOverview, 3000);
updateDeviceOverview();

document.getElementById('reload-btn').addEventListener('click', reloadConfig);
document.getElementById('config-form').addEventListener('submit', saveConfig);
document.getElementById('voice-device-select').addEventListener('change', () => {{ selectedDeviceHost = document.getElementById('voice-device-select').value; renderDevices(); }});
document.getElementById('voice-device-add').addEventListener('click', () => openDeviceDialog());
document.getElementById('voice-device-save').addEventListener('click', saveVoiceDevice);
document.getElementById('relay-restart').addEventListener('click', restartRelay);
applyDefaultHints();
loadConfig();
loadDevices();
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


if __name__ == "__main__":
    asyncio.run(main())
