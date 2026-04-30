# OpenHAVoice tools

## `openhavoice-control.py`

Small helper for safe ESPHome entities exposed by the Voice PE.

Examples:

```bash
tools/openhavoice-control.py list
tools/openhavoice-control.py set-switch wake_sound on
tools/openhavoice-control.py set-switch mute off
tools/openhavoice-control.py set-select wake_word_sensitivity "Slightly sensitive"
```

Restart is intentionally guarded:

```bash
tools/openhavoice-control.py press-button restart --allow-restart
```

Wake word / assistant pipeline selectors are intentionally not exposed here yet.
They use the Voice Assistant configuration API and are tracked in issue #24.
