# systemd user service

OpenHAVoice should run as a persistent backend client for the Voice PE. If no backend client is connected, stock Voice PE firmware enters the red/not-ready state.

This directory contains a user-systemd unit for running the relay on the OpenHAVoice host.

## Install

From the repo root:

```bash
python3 -m venv relay/.venv
relay/.venv/bin/pip install -r relay/requirements.txt

mkdir -p ~/.config/openhavoice
cp relay/.env.example ~/.config/openhavoice/relay.env
# edit ~/.config/openhavoice/relay.env and add VOICE_PSK / OPENCLAW_TOKEN

mkdir -p ~/.config/systemd/user
cp systemd/openhavoice-relay.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now openhavoice-relay.service
```

## Logs

```bash
journalctl --user -u openhavoice-relay.service -f
```

## Stop / restart

```bash
systemctl --user restart openhavoice-relay.service
systemctl --user stop openhavoice-relay.service
```

## Notes

- Do not commit `~/.config/openhavoice/relay.env`; it contains secrets.
- Keep Home Assistant disabled for the specific Voice PE while OpenHAVoice is the active backend.
- If the Voice PE stays red after enabling the service, reboot the Voice PE and watch the service logs for reconnects.
