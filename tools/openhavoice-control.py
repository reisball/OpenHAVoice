#!/usr/bin/env python3
"""Small control helper for safe ESPHome entities exposed by the Voice PE.

This intentionally only uses normal ESPHome entity commands. Voice assistant
configuration fields such as wake word / assistant pipeline are tracked
separately because they use a different API surface.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from aioesphomeapi import APIClient

SAFE_SELECTS = {"wake_word_sensitivity"}
SAFE_SWITCHES = {"mute", "wake_sound"}
SAFE_BUTTONS = {"restart"}  # exposed but requires --allow-restart


def load_env(path: str | None) -> None:
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.extend([
        Path.home() / ".config" / "openhavoice" / "relay.env",
        Path(__file__).resolve().parents[1] / "relay" / ".env",
    ])
    for env_path in candidates:
        if not env_path.exists():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        return


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing {name}; pass --env or configure ~/.config/openhavoice/relay.env")
    return value


async def connect() -> APIClient:
    client = APIClient(
        require("VOICE_HOST"),
        6053,
        os.environ.get("VOICE_PASSWORD", ""),
        noise_psk=require("VOICE_PSK"),
    )
    await client.connect(login=True)
    return client


async def list_entities() -> int:
    client = await connect()
    try:
        info = await client.device_info()
        print(f"device {info.name} mac={info.mac_address} esphome={info.esphome_version}")
        entities, _services = await client.list_entities_services()
        for entity in entities:
            cls = type(entity).__name__
            object_id = getattr(entity, "object_id", "")
            name = getattr(entity, "name", "")
            key = getattr(entity, "key", "")
            options = getattr(entity, "options", None)
            if cls in {"SwitchInfo", "SelectInfo", "ButtonInfo", "LightInfo", "MediaPlayerInfo"}:
                suffix = f" options={options}" if options else ""
                print(f"{cls:16} object_id={object_id:28} key={key:<10} name={name!r}{suffix}")
        return 0
    finally:
        await client.disconnect()


async def set_switch(object_id: str, state_text: str) -> int:
    desired = state_text.lower() in {"1", "true", "on", "yes", "enable", "enabled"}
    client = await connect()
    try:
        entities, _ = await client.list_entities_services()
        for entity in entities:
            if type(entity).__name__ == "SwitchInfo" and getattr(entity, "object_id", "") == object_id:
                if object_id not in SAFE_SWITCHES:
                    raise SystemExit(f"Refusing unsafe/unknown switch {object_id!r}")
                client.switch_command(entity.key, desired, getattr(entity, "device_id", 0))
                print(f"set switch {object_id}={desired}")
                return 0
        raise SystemExit(f"Switch not found: {object_id}")
    finally:
        await client.disconnect()


async def set_select(object_id: str, value: str) -> int:
    client = await connect()
    try:
        entities, _ = await client.list_entities_services()
        for entity in entities:
            if type(entity).__name__ == "SelectInfo" and getattr(entity, "object_id", "") == object_id:
                if object_id not in SAFE_SELECTS:
                    raise SystemExit(f"Refusing unsafe/unknown select {object_id!r}")
                options = list(getattr(entity, "options", []) or [])
                if value not in options:
                    raise SystemExit(f"Invalid value {value!r}; options: {options}")
                client.select_command(entity.key, value, getattr(entity, "device_id", 0))
                print(f"set select {object_id}={value!r}")
                return 0
        raise SystemExit(f"Select not found: {object_id}")
    finally:
        await client.disconnect()


async def press_button(object_id: str, allow_restart: bool) -> int:
    if object_id == "restart" and not allow_restart:
        raise SystemExit("Refusing restart without --allow-restart")
    client = await connect()
    try:
        entities, _ = await client.list_entities_services()
        for entity in entities:
            if type(entity).__name__ == "ButtonInfo" and getattr(entity, "object_id", "") == object_id:
                if object_id not in SAFE_BUTTONS:
                    raise SystemExit(f"Refusing unsafe/unknown button {object_id!r}")
                client.button_command(entity.key, getattr(entity, "device_id", 0))
                print(f"pressed button {object_id}")
                return 0
        raise SystemExit(f"Button not found: {object_id}")
    finally:
        await client.disconnect()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Control safe Voice PE ESPHome entities")
    parser.add_argument("--env", help="Path to env file; defaults to ~/.config/openhavoice/relay.env")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_sw = sub.add_parser("set-switch")
    p_sw.add_argument("object_id", choices=sorted(SAFE_SWITCHES))
    p_sw.add_argument("state", help="on/off")
    p_sel = sub.add_parser("set-select")
    p_sel.add_argument("object_id", choices=sorted(SAFE_SELECTS))
    p_sel.add_argument("value")
    p_btn = sub.add_parser("press-button")
    p_btn.add_argument("object_id", choices=sorted(SAFE_BUTTONS))
    p_btn.add_argument("--allow-restart", action="store_true")
    args = parser.parse_args()
    load_env(args.env)
    if args.cmd == "list":
        return await list_entities()
    if args.cmd == "set-switch":
        return await set_switch(args.object_id, args.state)
    if args.cmd == "set-select":
        return await set_select(args.object_id, args.value)
    if args.cmd == "press-button":
        return await press_button(args.object_id, args.allow_restart)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
