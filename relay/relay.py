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
