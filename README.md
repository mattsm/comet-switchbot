# comet-switchbot

A power button in the GL.iNet Comet (GL-RM1PE) web UI that presses a
SwitchBot Bot, for powering the machine behind the KVM on and off.

The Comet has no Bluetooth radio, so a small container on a Proxmox host with
Bluetooth drives the Bot:

```
browser ──https──> Comet nginx ──(KVM login check)──> http://<node>:8779 ──D-Bus──> host BlueZ ──BLE──> Bot
```

- `node/` — `switchbotd.py` (BlueZ over D-Bus + a token-protected HTTP API)
  and its systemd unit. It runs in an unprivileged LXC with the host's
  `/run/dbus` bind-mounted at `/mnt/host-dbus`. Bluetooth sockets only exist
  in the host's network namespace, so the container uses the host's
  bluetoothd rather than its own.
- `comet/` — the button (`ui.js`), an nginx drop-in that serves it and
  forwards `/switchbot/api/` to the node behind the Comet's login, and a boot
  hook (`S90switchbot`) that re-applies both at every boot. GL's own files
  are never modified.

## Setup

On the Proxmox host (the Bot must be within Bluetooth range of it):

```sh
pct create 110 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
  --hostname switchbot --cores 1 --memory 512 --swap 256 --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp,type=veth --features nesting=1 \
  --unprivileged 1 --onboot 1 --tags switchbot \
  --mp0 /run/dbus,mp=/mnt/host-dbus --ssh-public-keys <your key file>
pct start 110
```

Nothing else on the host may claim the Bluetooth adapter (for example a VM
with it passed through), or the node loses it.

Then, from a machine with SSH access to both:

```sh
sh node/install.sh root@<node-ip>
sh comet/install.sh root@<comet-ip> <node-ip>
```

Give the node a fixed address (DHCP reservation); the Comet's nginx points at
it by IP. Rerun `comet/install.sh` after a Comet firmware update if the
button is gone.

In the Comet UI, the button sits in a corner (the arrow icon moves it). Open
Setup, scan, and pick the Bot. Press and Hold ask for confirmation. Hold sets
the Bot's long-press time, presses, and resets it to 0.

The Bot must be in press mode, not switch mode. If it has a password in the
SwitchBot app, set the same one under Setup.

## CLI on the node

```sh
cd /opt/switchbot && sudo -u switchbot env DBUS_SYSTEM_BUS_ADDRESS=unix:path=/mnt/host-dbus/system_bus_socket \
  python3 switchbotd.py scan | info | press | hold 5 | config --mac C1:23:45:67:89:AB
```

## Tests

```sh
uv venv .venv && uv pip install --python .venv/bin/python aiohttp==3.11.16 dbus_next==0.2.3
.venv/bin/python tests/test_switchbotd.py   # fake BlueZ on a private dbus-daemon
sh tests/test_nginx.sh                       # drop-in in a stock nginx container
```

## Remove

```sh
ssh root@<comet> /etc/kvmd/user/scripts/S90switchbot uninstall
```
