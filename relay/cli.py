#!/usr/bin/env python3
"""OpenHAVoice configuration CLI.

Usage:
  python -m relay.cli show              # Display current config
  python -m relay.cli set KEY VALUE      # Update a single field
  python -m relay.cli validate           # Validate config
  python -m relay.cli generate           # Generate .env.example
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dataclasses import fields

from .config import RelayConfig


def cmd_show(config: RelayConfig, args: argparse.Namespace) -> None:
    reveal = getattr(args, "reveal", False)
    data = config.to_dict(reveal=reveal)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
    else:
        max_key_len = max(len(k) for k in data)
        for key, value in data.items():
            print(f"{key:<{max_key_len}}  {value}")


def cmd_set(config: RelayConfig, args: argparse.Namespace) -> None:
    try:
        config.update(args.key, args.value)
        config.save()
        field = config.env_for_field(args.key)
        print(f"✓ {args.key}={getattr(config, field)}")
        print("  Config saved. Restart or POST /config/reload to apply.")
    except KeyError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"✗ Invalid value: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_validate(config: RelayConfig, args: argparse.Namespace) -> None:
    errors = config.validate()
    if errors:
        print("✗ Configuration errors:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    else:
        print("✓ Configuration is valid.")


def _field_type_name(type_value: object) -> str:
    if hasattr(type_value, "__name__"):
        return type_value.__name__  # type: ignore[no-any-return]
    return str(type_value).strip("'\"")


def cmd_generate(config: RelayConfig, args: argparse.Namespace) -> None:
    """Generate a .env.example from RelayConfig field metadata."""
    target = Path(args.output) if getattr(args, "output", None) else Path(".env.example")

    lines: list[str] = []
    current_section: str | None = None

    for fld in fields(RelayConfig):
        section = str(fld.metadata.get("section", "General"))
        if section != current_section:
            if lines:
                lines.append("")
            lines.append(f"# ── {section} {'─' * max(0, 60 - len(section))}")
            current_section = section

        description = str(fld.metadata.get("description", "")).strip()
        if description:
            lines.append(f"# {description}")
        lines.append(f"# type: {_field_type_name(fld.type)}")
        env_key = fld.name.upper()
        default = getattr(config, fld.name)
        lines.append(f"{env_key}={default}")
        lines.append("")

    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"✓ Generated {target}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openhavoice-config",
        description="OpenHAVoice configuration management",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # show
    p_show = sub.add_parser("show", help="Display current configuration")
    p_show.add_argument("--reveal", action="store_true", help="Show secret values in plain text")
    p_show.add_argument("--json", action="store_true", help="Output as JSON")

    # set
    p_set = sub.add_parser("set", help="Update a configuration value")
    p_set.add_argument("key", help="Config key (UPPER_CASE or lower_case)")
    p_set.add_argument("value", help="New value")

    # validate
    sub.add_parser("validate", help="Validate current configuration")

    # generate
    p_gen = sub.add_parser("generate", help="Generate .env.example file")
    p_gen.add_argument("--output", default=".env.example", help="Output path")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = RelayConfig.load()

    handlers = {
        "show": cmd_show,
        "set": cmd_set,
        "validate": cmd_validate,
        "generate": cmd_generate,
    }
    handler = handlers[args.command]
    handler(config, args)


if __name__ == "__main__":
    main()
