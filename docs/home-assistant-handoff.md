# Home Assistant handoff workflow

This document describes the safe workflow for handing a stock Home Assistant Voice Preview Edition between Home Assistant and the OpenHAVoice direct relay.

The goal is to test and run OpenHAVoice without flashing custom firmware, deleting the HA device, or losing the path back to Home Assistant.

## Scope

Applies to a Voice PE that:

- is already on Wi-Fi,
- already has stock ESPHome firmware,
- exposes the ESPHome Native API on port `6053`, and
- has a known Noise PSK/API key for OpenHAVoice.

For brand-new devices that are not on Wi-Fi yet, use the future Improv BLE provisioning flow tracked separately.

## Safety rules

- Do **not** delete the Voice PE from Home Assistant during OpenHAVoice tests.
- Do **not** flash firmware just to test the direct relay.
- Do **not** remove the ESPHome device entry unless you intentionally want to re-adopt it later.
- Do **not** commit or paste the Noise PSK/API key.
- Keep the physical mute switch off during audio tests.
- If the device enters a red/not-ready LED state, prefer restarting the relay and rebooting the Voice PE over changing firmware/configuration.

## Why handoff is needed

The stock Voice PE exposes voice assistant sessions over the ESPHome Native API. Home Assistant can also connect to the same ESPHome device and may own the voice assistant backend role.

During direct-relay testing, the OpenHAVoice relay should be the active backend client. In live testing, disabling HA's ESPHome integration entry for the test Voice PE allowed OpenHAVoice to receive the voice assistant audio stream and return playback URLs directly.

## Hand off from Home Assistant to OpenHAVoice

1. In Home Assistant, go to **Settings → Devices & services**.
2. Find the ESPHome integration entry for the target Voice PE.
3. Disable the integration entry for that device.
   - Do not delete the device.
   - Do not remove entities.
   - Do not reconfigure firmware.
4. Start or restart the OpenHAVoice relay.

   ```bash
   systemctl --user restart openhavoice-relay.service
   journalctl --user -u openhavoice-relay.service -f
   ```

5. Wait for a connection log similar to:

   ```text
   CONNECTED home-assistant-voice-0abf52. Backend client is active; session='openhavoice:home-assistant-voice-0abf52'; press button and speak.
   ```

6. If the LED remains red/not-ready or voice sessions do not start, reboot the Voice PE once and let the relay reconnect.
7. Test with the button first.
8. Then test wake word, for example `Okay Nabu`.

Expected successful wake-word log:

```text
START wake='Okay Nabu' flags=1 ...
TRANSCRIPT '...'
OPENCLAW_REPLY '...'
TTS_READY http://<relay-host>:8765/tts/<token>.wav ...
SERVE_TTS token=*** remote=<voice-pe-ip>
```

## Hand back from OpenHAVoice to Home Assistant

1. Stop the OpenHAVoice relay.

   ```bash
   systemctl --user stop openhavoice-relay.service
   ```

2. In Home Assistant, re-enable the ESPHome integration entry for the Voice PE.
3. Wait for HA to reconnect to the device.
4. If HA does not reconnect cleanly, reboot the Voice PE.
5. Verify normal HA behavior:
   - device/entities become available,
   - button/wake-word pipeline works through HA,
   - LED returns to normal state.

## Troubleshooting

### Relay logs `CONNECTED`, but button/wake sessions do nothing

- Confirm the HA ESPHome integration entry is disabled for this device.
- Confirm the physical mute switch is off.
- Reboot the Voice PE and wait for the relay reconnect log.
- Check `VOICE_HOST` and `VOICE_PSK` in the relay environment.

### Device LED is red/not-ready

- Ensure either HA or OpenHAVoice is actively connected as backend client.
- Restart the OpenHAVoice relay.
- If needed, reboot the Voice PE.
- Avoid deleting/re-adopting the device as a first response.

### STT/TTS works but the LED stays active after speech

OpenHAVoice has a configurable post-TTS grace period:

```bash
TTS_POST_PLAYBACK_GRACE_SECONDS=1.0
```

If this is set too high, the device can appear to stay in the assist state after audio playback ends.

### Home Assistant reconnects but OpenHAVoice later stops receiving audio

This usually means HA has become the active backend client again. For direct relay tests, hand off to OpenHAVoice by disabling the HA ESPHome integration entry for the target device.

## Current verified test device

- Device name: `home-assistant-voice-0abf52`
- IP: `192.168.50.146`
- Firmware/project: `Nabu Casa.Home Assistant Voice PE` `26.4.0`
- ESPHome: `2026.3.2`
- OpenHAVoice session key: `openhavoice:home-assistant-voice-0abf52`
- Wake word verified: `Okay Nabu`

## Open questions

- Whether HA and OpenHAVoice can coexist with a cleaner arbitration model instead of disabling HA's ESPHome integration.
- Whether specific ESPHome voice assistant configuration settings can select a backend without full integration handoff.
- Whether future OpenHAVoice provisioning can make HA entirely optional for first setup.
