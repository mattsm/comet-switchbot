#!/bin/sh
# Install or update the power button on a GL.iNet Comet.
#   sh comet/install.sh root@<comet> <node-ip>
# Reads the node's token over SSH (root@<node-ip>) and points the Comet's
# nginx at http://<node-ip>:8779. Run again after a Comet firmware update.
set -eu
[ $# -eq 2 ] || { echo "usage: $0 root@<comet> <node-ip>" >&2; exit 1; }
comet=$1 node=$2
cd "$(dirname "$0")"

token=$(ssh "root@$node" cat /etc/switchbot/token)
[ ${#token} -ge 32 ] || { echo "no token on $node" >&2; exit 1; }

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cp ui.js manifest.yaml S90switchbot "$stage/"
sed -e "s|@NODE@|$node:8779|" -e "s|@TOKEN@|$token|" nginx.ctx-server.conf.in > "$stage/nginx.ctx-server.conf"

tar -C "$stage" --owner=0 --group=0 -cf - . | ssh "$comet" 'set -e
	d=/etc/kvmd/user/switchbot
	mkdir -p "$d" /etc/kvmd/user/scripts
	tar -xf - -C "$d"
	chmod 600 "$d/nginx.ctx-server.conf"
	chmod 755 "$d/S90switchbot"
	cp "$d/S90switchbot" /etc/kvmd/user/scripts/S90switchbot
	/etc/kvmd/user/scripts/S90switchbot restart
	echo "power button installed"'
