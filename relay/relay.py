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

from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("relay")


def pcm_to_wav_bytes(
    pcm: bytes, sample_rate: int = 16000, channels: int = 1, bits: int = 16
) -> bytes:
    """Convert raw PCM to WAV file bytes (in memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(bits // 8)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class Transcriber:
    """Speech-to-text via OpenAI Whisper API."""

    def __init__(self, api_key: str, model: str = "whisper-1"):
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def transcribe(self, pcm: bytes) -> str:
        """Transcribe raw PCM to text. Returns stripped transcript."""
        wav = pcm_to_wav_bytes(pcm)
        result = self._client.audio.transcriptions.create(
            model=self._model,
            file=("audio.wav", wav, "audio/wav"),
        )
        return result.text.strip()


class WyomingProtocol(asyncio.Protocol):
    """Async protocol handler for one Voice PE connection."""

    def __init__(self, on_utterance):
        self._buffer = b""
        self._audio_chunks: list[bytes] = []
        self._on_utterance = on_utterance  # async callback(pcm) -> (text, pcm)
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
                return  # incomplete header, wait for more data

            try:
                msg = json.loads(self._buffer[4:frame_end])
            except json.JSONDecodeError:
                log.warning("Invalid JSON in Wyoming frame")
                self._buffer = self._buffer[frame_end:]
                continue

            audio_data = self._buffer[frame_end:]
            self._buffer = b""
            self._handle_message(msg, audio_data)

    def _handle_message(self, msg: dict, audio_data: bytes):
        msg_type = msg.get("type", "")

        if msg_type == "describe":
            log.info("Voice PE capabilities: %s", msg.get("data", {}))
        elif msg_type == "audio":
            if audio_data:
                self._audio_chunks.append(audio_data)
        elif msg_type == "audio-stop":
            total_bytes = sum(len(c) for c in self._audio_chunks)
            log.info(
                "End of utterance — %d chunks, %d bytes",
                len(self._audio_chunks),
                total_bytes,
            )
            asyncio.ensure_future(self._process_utterance())

    async def _process_utterance(self):
        try:
            pcm = b"".join(self._audio_chunks)
            self._audio_chunks = []

            response_text, tts_pcm = await self._on_utterance(pcm)

            self._send_wyoming("synthesize", {"text": response_text})

            # Stream PCM back in 1024-byte chunks (~32ms at 16kHz/16bit/mono)
            for offset in range(0, len(tts_pcm), 1024):
                chunk = tts_pcm[offset : offset + 1024]
                self._send_wyoming(
                    "audio",
                    {"rate": 16000, "width": 2, "channels": 1},
                    audio_payload=chunk,
                )
        except Exception:
            log.exception("Error processing utterance")

    def _send_wyoming(
        self, msg_type: str, data: dict, audio_payload: bytes = b""
    ):
        payload = json.dumps({"type": msg_type, "data": data}).encode()
        header = struct.pack(">I", len(payload))
        self._transport.write(header + payload + audio_payload)


def main():
    parser = argparse.ArgumentParser(description="OpenHAVoice Relay")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=10200)
    parser.add_argument(
        "--openclaw-url",
        default=os.environ.get("OPENCLAW_URL", "http://192.168.50.186:18789"),
    )
    parser.add_argument(
        "--openclaw-token",
        default=os.environ.get("OPENCLAW_TOKEN", ""),
    )
    parser.add_argument("--stt-model", default="whisper-1")
    parser.add_argument("--tts-voice", default="Rachel")
    args = parser.parse_args()

    log.info("Starting relay on %s:%s", args.host, args.port)
    log.info("OpenClaw: %s", args.openclaw_url)


if __name__ == "__main__":
    main()
