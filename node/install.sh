#!/bin/sh
# Install or update switchbotd on the Bluetooth node.
#   sh node/install.sh root@<node>
# On a Linux host it uses the host's BlueZ, installing it if needed. In a
# container it uses the host's BlueZ through the host's /run/dbus mounted at
# /mnt/host-dbus (see README).
set -eu
[ $# -eq 1 ] || { echo "usage: $0 root@<node>" >&2; exit 1; }
target=$1
cd "$(dirname "$0")"

ssh "$target" 'set -eu
	pkgs="python3-aiohttp python3-dbus-next"
	[ -S /mnt/host-dbus/system_bus_socket ] || pkgs="$pkgs bluez"
	missing=""
	for p in $pkgs; do
		dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"
	done
	if [ -n "$missing" ]; then
		apt-get update -qq
		DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $missing
	fi
	[ -S /mnt/host-dbus/system_bus_socket ] || systemctl enable -q --now bluetooth
	id switchbot >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin switchbot
	# Older BlueZ only lets root and the bluetooth group talk to it.
	if getent group bluetooth >/dev/null; then usermod -aG bluetooth switchbot; fi
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
