# Custom wake words

Short version: with the current stock-firmware OpenHAVoice path, a custom wake word is **not just a relay setting**.

OpenHAVoice does not detect the wake word itself. The Home Assistant Voice PE firmware listens for the wake word on the device and then starts an ESPHome Voice Assistant session. The relay only receives the already-started session plus metadata such as the phrase (`Okay Nabu`).

## What works today

### Use a wake word already supported by the Voice PE firmware

This is the safe/default path.

1. Keep the stock Voice PE firmware.
2. Configure the active wake word through Home Assistant/ESPHome while Home Assistant owns the device.
3. Hand the device back to OpenHAVoice by disabling Home Assistant's ESPHome integration entry for that Voice PE, not deleting or flashing it.
4. Start/restart the OpenHAVoice relay and test the selected wake word.

This has been verified with `Okay Nabu`.

OpenHAVoice can eventually expose this as a read/change control in its Web UI, but it should be built carefully because the ESPHome voice-assistant configuration API is separate from normal entity commands and earlier direct reads were not reliable enough to write blindly.

## What does not work as a simple toggle

### Home Assistant openWakeWord custom `.tflite` models

Home Assistant supports training and adding custom openWakeWord models under `/share/openwakeword`, then selecting them in an Assist pipeline.

That path is useful when Home Assistant is the active voice backend and a device streams audio to Home Assistant for server-side wake-word detection.

It is **not the same** as OpenHAVoice's current direct stock-firmware mode, where the Voice PE performs wake-word detection before OpenHAVoice receives audio.

### `Hey Zoe` on stock firmware

A phrase like `Hey Zoe` needs firmware/device support. The relay cannot make the stock Voice PE hear a new on-device wake phrase after the fact.

## Real custom wake-word options

### Option A: wait for/implement supported Voice PE configuration

If the stock firmware exposes downloadable/external wake-word models via the ESPHome Voice Assistant configuration API, OpenHAVoice could support:

- list available wake words
- show active wake words
- safely set active wake words
- possibly provide external wake-word model metadata

This needs a cautious implementation and real-device tests.

### Option B: custom ESPHome firmware with microWakeWord

For a true custom on-device wake word, train a microWakeWord model and build custom ESPHome firmware for the Voice PE that includes it.

Tradeoffs:

- Pros: real on-device `Hey Zoe`-style wake word
- Cons: leaves the pure stock-firmware path, requires firmware build/flash, more recovery/testing work, and can affect reliability

This should be treated as an experiment, not the default OpenHAVoice MVP path.

### Option C: server-side wake-word detection

OpenHAVoice could theoretically keep a continuous microphone stream and run openWakeWord server-side, but that is a different architecture from the current ESPHome Voice Assistant event flow. It would increase bandwidth/CPU/privacy surface and would need custom relay work.

## Recommended project path

1. Keep `Okay Nabu`/stock wake word for the stable MVP.
2. Add a read-only OpenHAVoice diagnostic that reports available/active wake words from the Voice PE.
3. If reliable, add a guarded setter for firmware-supported active wake words.
4. Only then experiment with `Hey Zoe` via custom microWakeWord firmware.

In other words: first make the existing device-control surface safe and observable; firmware Frankenstein later, if it earns its beer.
