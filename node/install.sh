#!/bin/sh
# Install or update switchbotd on the Bluetooth node container.
#   sh node/install.sh root@<node>
# The container must have the host's /run/dbus bind-mounted at
# /mnt/host-dbus (see README).
set -eu
[ $# -eq 1 ] || { echo "usage: $0 root@<node>" >&2; exit 1; }
target=$1
cd "$(dirname "$0")"

ssh "$target" 'set -eu
	[ -S /mnt/host-dbus/system_bus_socket ] || { echo "no host D-Bus socket at /mnt/host-dbus" >&2; exit 1; }
	if ! python3 -c "import aiohttp, dbus_next" 2>/dev/null; then
		apt-get update -qq
		DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-aiohttp python3-dbus-next
	fi
	id switchbot >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin switchbot
	install -d -m 0755 /opt/switchbot
	install -d -m 0750 -o switchbot -g switchbot /etc/switchbot
	if [ ! -s /etc/switchbot/token ]; then
		head -c 32 /dev/urandom | od -An -tx1 | tr -d " \n" > /etc/switchbot/token
		chown switchbot:switchbot /etc/switchbot/token
		chmod 0600 /etc/switchbot/token
	fi'
scp -q switchbotd.py "$target:/opt/switchbot/switchbotd.py"
scp -q switchbot.service "$target:/etc/systemd/system/switchbot.service"
ssh "$target" 'systemctl daemon-reload
	systemctl enable -q switchbot
	systemctl restart switchbot
	sleep 2
	systemctl is-active switchbot
	python3 -c "import urllib.request as u; t = open(\"/etc/switchbot/token\").read().strip(); print(u.urlopen(u.Request(\"http://127.0.0.1:8779/api/status\", headers={\"Authorization\": \"Bearer \" + t})).read().decode())"'
