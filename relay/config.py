"""Configuration management for OpenHAVoice relay.

Loads from .env file + environment variables. Supports CLI and Web UI via
the RelayConfig dataclass. Secret values are redacted in non-reveal exports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

LEGACY_ENV_PATH = Path(__file__).with_name(".env")
DEFAULT_ENV_PATH = Path("~/.config/openhavoice/relay.env").expanduser()

SECRET_KEYS = {"VOICE_PASSWORD", "VOICE_PSK", "VOICE_DEVICES", "OPENCLAW_TOKEN", "CONFIG_ADMIN_TOKEN"}
LEGACY_ENV_ALIASES = {
    "OPENCLAW_MODEL": "OPENCLAW_AGENT",
}


def default_env_path() -> Path:
    """Return the config file path used by CLI/Web saves.

    Production installs keep secrets in ~/.config/openhavoice/relay.env.
    OPENHAVOICE_CONFIG_PATH can override this; relay/.env remains supported for
    older/dev checkouts that already have one.
    """
    configured = os.environ.get("OPENHAVOICE_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    if DEFAULT_ENV_PATH.exists():
        return DEFAULT_ENV_PATH
    if LEGACY_ENV_PATH.exists():
        return LEGACY_ENV_PATH
    return DEFAULT_ENV_PATH


@dataclass
class RelayConfig:
    # ── Voice PE ──────────────────────────────────────────────
    voice_host: str = ""
    voice_psk: str = ""  # secret
    voice_password: str = ""  # secret
    voice_devices: str = ""  # secret JSON list: [{name, host, psk, password}]

    # ── STT (Whisper) ─────────────────────────────────────────
    whisper_url: str = "http://192.168.50.51:8000/v1/audio/transcriptions"
    language: str = "de"

    # ── TTS (Orpheus) ─────────────────────────────────────────
    orpheus_url: str = "http://192.168.50.52:5005/v1/audio/speech"
    orpheus_model: str = "orpheus-german-fix-ctx4k"
    orpheus_voice: str = "jana"
    tts_host: str = "0.0.0.0"
    tts_port: int = 8765
    tts_post_playback_grace_seconds: float = 1.0

    # ── OpenClaw Gateway ──────────────────────────────────────
    openclaw_url: str = "http://127.0.0.1:18789"
    openclaw_token: str = ""  # secret
    openclaw_session_key: str = ""
    openclaw_agent: str = "default"
    openclaw_message_channel: str = "voice"
    openclaw_voice_system_prompt: str = (
        "Du antwortest über einen Voice Assistant. Antworte kurz, natürlich "
        "und ohne Markdown, Listen oder Emojis. Ein bis zwei Sätze reichen."
    )

    # ── VAD / Capture ─────────────────────────────────────────
    min_speech_ms: int = 900
    end_silence_ms: int = 900
    max_capture_seconds: float = 15.0
    vad_aggressiveness: int = 2
    rms_silence_threshold: int = 500
    rms_end_silence_ms: int = 1200

    # ── Web config API ────────────────────────────────────────
    config_admin_token: str = ""  # secret; if empty, config API is localhost-only

    # ── Network / Reconnect ───────────────────────────────────
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0

    # ── Methods ───────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path | None = None) -> "RelayConfig":
        """Load config from .env file and environment variables."""
        env = _read_dotenv(path or default_env_path())
        values: dict[str, Any] = {}
        for fld in fields(cls):
            env_key = _field_to_env(fld.name)
            raw = os.environ.get(env_key, env.get(env_key, ""))
            if not raw.strip() and env_key == "OPENCLAW_AGENT":
                # Backward compatibility for configs written before the UI field
                # was renamed from model to agent.
                raw = os.environ.get("OPENCLAW_MODEL", env.get("OPENCLAW_MODEL", ""))
            if raw.strip():
                value = _coerce(raw, fld.type)
                if fld.name == "openclaw_agent":
                    value = _normalize_openclaw_agent_value(str(value))
                values[fld.name] = value
        return cls(**values)

    def save(self, path: Path | None = None) -> None:
        """Write current config to .env file, preserving comments."""
        target = path or default_env_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""

        new_lines: list[str] = []
        written_keys: set[str] = set()

        # Update existing lines in-place
        for line in existing.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            env_key = LEGACY_ENV_ALIASES.get(key.upper(), key.upper())
            field_name = _env_to_field(env_key)
            if field_name and hasattr(self, field_name):
                if env_key in written_keys:
                    continue
                value = getattr(self, field_name)
                new_lines.append(f"{env_key}={value}")
                written_keys.add(env_key)
            else:
                new_lines.append(line)

        # Append new keys that weren't in the file
        for fld in fields(self):
            env_key = _field_to_env(fld.name)
            if env_key not in written_keys:
                value = getattr(self, fld.name)
                new_lines.append(f"{env_key}={value}")

        target.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    def to_dict(self, reveal: bool = False) -> dict[str, Any]:
        """Export config as dict. Secrets are redacted unless reveal=True."""
        result: dict[str, Any] = {}
        for fld in fields(self):
            value = getattr(self, fld.name)
            env_key = _field_to_env(fld.name)
            if env_key in SECRET_KEYS and not reveal:
                result[fld.name] = "****" if value else ""
            else:
                result[fld.name] = value
        return result

    def validate(self) -> list[str]:
        """Return list of validation errors. Empty list = valid."""
        errors: list[str] = []

        if not self.voice_host.strip():
            errors.append("voice_host is required")
        if not self.voice_psk.strip():
            errors.append("voice_psk is required")

        for label, url in [("whisper_url", self.whisper_url),
                           ("orpheus_url", self.orpheus_url),
                           ("openclaw_url", self.openclaw_url)]:
            if url and not url.startswith(("http://", "https://")):
                errors.append(f"{label} must start with http:// or https://")

        if not (1 <= self.tts_port <= 65535):
            errors.append("tts_port must be between 1 and 65535")
        if self.min_speech_ms < 0:
            errors.append("min_speech_ms must be >= 0")
        if self.end_silence_ms < 0:
            errors.append("end_silence_ms must be >= 0")
        if self.max_capture_seconds <= 0:
            errors.append("max_capture_seconds must be > 0")
        if not (0 <= self.vad_aggressiveness <= 3):
            errors.append("vad_aggressiveness must be between 0 and 3")
        if self.rms_silence_threshold < 0:
            errors.append("rms_silence_threshold must be >= 0")
        if self.rms_end_silence_ms < 0:
            errors.append("rms_end_silence_ms must be >= 0")
        if self.reconnect_initial_seconds <= 0:
            errors.append("reconnect_initial_seconds must be > 0")
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            errors.append("reconnect_max_seconds must be >= reconnect_initial_seconds")

        return errors

    def update(self, key: str, value: str) -> None:
        """Set a single field by its env key or field name."""
        env_key = LEGACY_ENV_ALIASES.get(key.upper(), key.upper())
        field_name = _env_to_field(env_key) or key.lower()
        if not hasattr(self, field_name):
            raise KeyError(f"Unknown config key: {key}")
        fld_type = type(getattr(self, field_name))
        coerced = _coerce(value, fld_type)
        if field_name == "openclaw_agent":
            coerced = _normalize_openclaw_agent_value(str(coerced))
        setattr(self, field_name, coerced)

    def env_for_field(self, field_name: str) -> str:
        """Return the configured value for a field, accepting env-key or field-name."""
        name = _env_to_field(field_name.upper()) or field_name.lower()
        return name


# ── Helpers ───────────────────────────────────────────────────

def _normalize_openclaw_agent_value(value: str) -> str:
    """Store only the agent name, while accepting legacy OpenClaw model targets."""
    raw = (value or "default").strip() or "default"
    lowered = raw.lower()
    if lowered == "openclaw":
        return "default"
    for prefix in ("openclaw/", "openclaw:", "agent:"):
        if lowered.startswith(prefix):
            suffix = raw[len(prefix):].strip()
            return suffix or "default"
    return raw


def _field_to_env(name: str) -> str:
    """Convert snake_case field name to UPPER_CASE env var."""
    return name.upper()


def _env_to_field(env_key: str) -> str | None:
    """Convert UPPER_CASE env var back to field name, or None if unknown."""
    field_name = env_key.lower()
    if field_name in {f.name for f in fields(RelayConfig)}:
        return field_name
    return None


def _coerce(raw: str, target_type: type | str) -> Any:
    """Coerce a string value to the target type.

    With ``from __future__ import annotations``, dataclass field types may be
    stored as strings, so accept both actual type objects and their names.
    """
    type_name = target_type if isinstance(target_type, str) else getattr(target_type, "__name__", str(target_type))
    if target_type is bool or type_name == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if target_type is int or type_name == "int":
        return int(raw) if raw.strip() else 0
    if target_type is float or type_name == "float":
        return float(raw) if raw.strip() else 0.0
    return raw


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read KEY=value pairs from a .env file (no override of os.environ)."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip("'\"")
    return result


def load_config() -> RelayConfig:
    """Convenience: load config, apply to os.environ, return object."""
    config = RelayConfig.load()
    for fld in fields(config):
        env_key = _field_to_env(fld.name)
        value = getattr(config, fld.name)
        if value is not None and env_key not in os.environ:
            os.environ[env_key] = str(value)
    return config
