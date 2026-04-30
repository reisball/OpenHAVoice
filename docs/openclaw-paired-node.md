# OpenClaw paired-node integration idea

OpenHAVoice currently talks to OpenClaw through the OpenAI-compatible HTTP API:

```text
Voice PE -> OpenHAVoice relay -> /v1/chat/completions -> OpenClaw session
```

That is the right short-term path because it is simple, stable, and already
works. The relay has a transcript, sends it to OpenClaw, receives assistant text,
then renders it with local TTS.

Longer term, the relay may fit better as a paired OpenClaw **node**:

```text
Voice PE -> OpenHAVoice relay == paired OpenClaw node == Gateway
```

In this model the Voice PE itself is still a stock ESPHome device. The
OpenHAVoice relay becomes the OpenClaw device identity and advertises a voice
capability on behalf of the Voice PE.

## Why consider this

Potential benefits:

- real device identity/presence in OpenClaw (`system-presence`, `node.list`)
- gateway-managed device token instead of a broad static Gateway API token
- OpenHAVoice can emit structured `node.event` voice events
- future gateway/UI integration can show connected Voice PE devices naturally
- better separation between operator API access and device/capability access
- possible support for offline/pending work via node pending APIs if useful
- a cleaner foundation for multi-device support

## Relevant OpenClaw protocol facts

From the Gateway WS protocol docs:

- All clients connect over WebSocket and declare `role` and `scopes` during the
  `connect` handshake.
- `role: "node"` is the capability-host role for paired devices.
- Node clients declare:
  - `caps`: high-level capability categories
  - `commands`: command allowlist for `node.invoke`
  - `permissions`: granular toggles
- Gateway methods relevant to this idea include:
  - `system-presence`
  - `node.pair.request/list/approve/reject/remove/verify`
  - `node.list` / `node.describe`
  - `node.invoke`
  - `node.event`
  - `node.pending.*`
- Pairing approvals are required for new device IDs unless local auto-approval
  is explicitly configured.
- Device tokens are issued after pairing and should be persisted by the client.
- All device-bearing WS connects must sign the server challenge nonce.

Important caution: node pairing is not just “a prettier API key”. It is a
capability/trust boundary. Any node commands we advertise become part of the
Gateway's device-control surface and need explicit policy decisions.

## Possible architecture

### Phase 1: node presence only

Relay connects as a paired node with minimal/no commands:

```json
{
  "role": "node",
  "caps": ["voice"],
  "commands": [],
  "permissions": {
    "voice.capture": true,
    "voice.playback": true
  }
}
```

The relay keeps using `/v1/chat/completions` or `sessions.send` for actual brain
turns. This validates pairing, identity, reconnect, token persistence, and
presence without changing the working voice path.

### Phase 2: structured voice events

Relay sends `node.event` messages for lifecycle events:

- voice device connected/disconnected
- voice session started/ended
- transcript produced
- STT/TTS/OpenClaw success/failure
- control changed

Transcript/event payload policy must be explicit. By default, avoid sending raw
audio or full transcript unless the operator enables it.

### Phase 3: native gateway session flow

Investigate whether the relay should use WS session methods instead of the
OpenAI HTTP API:

- `sessions.create` / `sessions.send`
- `chat.send` / `chat.history`
- session subscriptions for streaming/updates

This may make the voice device feel more native in OpenClaw, but it also couples
OpenHAVoice more tightly to Gateway protocol details.

### Phase 4: node commands

Only if useful, expose safe commands through `node.invoke`, for example:

- `voice.status`
- `voice.set_mute`
- `voice.set_wake_sound`
- `voice.set_sensitivity`
- maybe `voice.play_test_sound`

Avoid dangerous commands by default. Restart/update/firmware operations should
remain confirmation-gated or out of scope.

## Open questions

- Does Gateway currently define a first-class `voice` node capability, or would
  this be a convention initially?
- Should the node use `node.event` plus `/v1/chat/completions`, or move fully to
  `sessions.send`/`chat.send`?
- How should session routing work for a paired voice node?
  - default dedicated session: `openhavoice:<device-name>`
  - explicit opt-in for main/current session remains safer
- How should pairing/token bootstrap be implemented in Python?
  - challenge fetch
  - keypair generation
  - v3 signature payload
  - token persistence
- What metadata should identify the physical Voice PE?
  - ESPHome name
  - MAC address
  - IP address
  - firmware/project version
- What events may contain transcripts, and how are privacy settings surfaced?
- How should the Web UI show paired-node status alongside ESPHome device status?

## Recommendation

Keep the current HTTP chat path for the working MVP. Add paired-node support as
an optional integration path in phases:

1. pair the relay as a minimal voice node and show presence
2. send structured node events
3. evaluate WS session/chat methods for actual turns
4. expose safe device commands only after policy decisions

This gives us the elegance of OpenClaw-native device identity without breaking
the simple working path.
