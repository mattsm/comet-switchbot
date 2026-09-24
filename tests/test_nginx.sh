#!/bin/sh
# Checks the Comet nginx drop-in in a stock nginx container, inside a server
# block shaped like GL's (server-level auth_request, glweb at /), with fake
# kvmd auth and a fake Bluetooth node.
#   sh tests/test_nginx.sh
set -eu
cd "$(dirname "$0")/.."
T=$(mktemp -d /tmp/switchbot-nginx.XXXXXX)
NAME=switchbot-nginx-test
cleanup() {
	docker rm -f "$NAME" >/dev/null 2>&1 || true
	kill "$BACKEND" 2>/dev/null || true
	rm -rf "$T"
}
trap cleanup EXIT

PORT=18443 KVMD=19001 NODE=19779
TOKEN=0123456789abcdef0123456789abcdef

mkdir -p "$T/glweb/assets" "$T/extras/switchbot" "$T/run/switchbot/www" "$T/user/switchbot"
cat > "$T/glweb/index.html" <<'EOF'
<!doctype html><html><head><title>GLKVM</title><script type="module" src="./assets/index-X.js"></script></head><body><div id="app"></div></body></html>
EOF
echo 'console.log("gl")' > "$T/glweb/assets/index-X.js"
cp comet/ui.js "$T/user/switchbot/ui.js"
sed 's#</head>#<script src="/switchbot/ui.js" defer></script></head>#' "$T/glweb/index.html" > "$T/run/switchbot/www/index.html"
sed -e "s|@NODE@|127.0.0.1:$NODE|" -e "s|@TOKEN@|$TOKEN|" \
	-e "s|/run/switchbot/www|/t/run/switchbot/www|" -e "s|/usr/share/kvmd/glweb|/t/glweb|" \
	-e "s|/etc/kvmd/user/switchbot/ui.js|/t/user/switchbot/ui.js|" \
	comet/nginx.ctx-server.conf.in > "$T/extras/switchbot/nginx.ctx-server.conf"

cat > "$T/nginx.conf" <<EOF
events {}
http {
	include /etc/nginx/mime.types;
	upstream kvmd { server 127.0.0.1:$KVMD; }
	server {
		listen $PORT;
		absolute_redirect off;
		index index.html;
		auth_request /auth_check;
		location = /auth_check {
			internal;
			proxy_pass http://kvmd/auth/check;
			proxy_pass_request_body off;
			proxy_set_header Content-Length "";
			auth_request off;
		}
		location / {
			root /t/glweb;
			auth_request off;
		}
		location /api {
			proxy_pass http://kvmd;
			auth_request off;
		}
		include /t/extras/*/nginx.ctx-server.conf;
	}
}
EOF

chmod -R a+rX "$T"

# Fake kvmd (/auth/check wants cookie auth_token=good) and fake node (echoes
# what it received).
python3 - "$KVMD" "$NODE" <<'EOF' &
import json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Kvmd(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = "auth_token=good" in self.headers.get("Cookie", "")
        self.send_response(200 if ok else 401)
        self.end_headers()
    def log_message(self, *a): pass

class Node(BaseHTTPRequestHandler):
    def reply(self):
        body = json.dumps({"path": self.path, "auth": self.headers.get("Authorization"),
                           "xrw": self.headers.get("X-Requested-With")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = reply
    def log_message(self, *a): pass

for port, h in ((int(sys.argv[1]), Kvmd), (int(sys.argv[2]), Node)):
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", port), h).serve_forever, daemon=True).start()
threading.Event().wait()
EOF
BACKEND=$!

docker run -d --name "$NAME" --network host -v "$T:/t:ro" -v "$T/nginx.conf:/etc/nginx/nginx.conf:ro" \
	nginx:mainline-alpine >/dev/null
sleep 2
docker exec "$NAME" nginx -t 2>&1 | tail -1

fail() { echo "FAIL: $*"; docker logs "$NAME" 2>&1 | tail -5; exit 1; }
get() { curl -s -o "$T/out" -w '%{http_code}' "$@"; }

[ "$(get http://127.0.0.1:$PORT/)" = 200 ] && grep -q 'switchbot/ui.js' "$T/out" || fail "/ is not the injected index"
grep -q 'assets/index-X.js' "$T/out" || fail "injected index lost GL's script"
echo "ok: / serves GL's index with the button script"

[ "$(get http://127.0.0.1:$PORT/assets/index-X.js)" = 200 ] || fail "GL assets"
echo "ok: GL assets still served"

[ "$(get http://127.0.0.1:$PORT/switchbot/ui.js)" = 200 ] && grep -q __switchbot "$T/out" || fail "ui.js"
echo "ok: ui.js served without login"

[ "$(get http://127.0.0.1:$PORT/switchbot/api/status)" = 401 ] || fail "API reachable without login"
echo "ok: API refused without a KVM session"

[ "$(get -b auth_token=good -H 'Authorization: Bearer forged' -H 'X-Requested-With: switchbot' \
	-X POST http://127.0.0.1:$PORT/switchbot/api/press)" = 200 ] || fail "API with login"
grep -q '"path": "/api/press"' "$T/out" || fail "wrong upstream path: $(cat "$T/out")"
grep -q "\"auth\": \"Bearer $TOKEN\"" "$T/out" || fail "token not injected: $(cat "$T/out")"
grep -q '"xrw": "switchbot"' "$T/out" || fail "X-Requested-With not forwarded"
echo "ok: logged-in API call reaches the node with the Comet's token (client header replaced)"

rm "$T/run/switchbot/www/index.html"
[ "$(get http://127.0.0.1:$PORT/)" = 200 ] || fail "/ without rendered index"
grep -q 'switchbot/ui.js' "$T/out" && fail "fallback still injected"
grep -q 'assets/index-X.js' "$T/out" || fail "fallback is not GL's index"
echo "ok: without the rendered copy, GL's own index.html is served"
echo "ALL OK"
