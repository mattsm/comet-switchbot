#!/usr/bin/env python3
"""Press a SwitchBot Bot over BlueZ.

`serve` runs the HTTP API behind the power button injected into the Comet's
web UI; the Comet's nginx enforces the KVM login and forwards requests here
with the shared token. The other subcommands run one action and print JSON.
"""

import argparse
import asyncio
import binascii
import contextlib
import hmac
import json
import logging
import os
import re
import sys
import time

from dbus_next import BusType, Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.auth import AuthExternal

VERSION = "1.0"
CONFIG_PATH = os.environ.get("SWITCHBOT_CONFIG", "/etc/switchbot/config.json")
TOKEN_PATH = os.environ.get("SWITCHBOT_TOKEN_FILE", "/etc/switchbot/token")
LISTEN = os.environ.get("SWITCHBOT_LISTEN", "127.0.0.1:8779")
# A container that borrows the host's BlueZ has the host's /run/dbus here.
HOST_DBUS = "/mnt/host-dbus/system_bus_socket"

ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
CHAR = "org.bluez.GattCharacteristic1"
PROPS = "org.freedesktop.DBus.Properties"

WRITE_UUID = "cba20002-224d-11e6-9fb8-0002a5d5c51b"
NOTIFY_UUID = "cba20003-224d-11e6-9fb8-0002a5d5c51b"
# Bots advertise service data under the old (0x0d00) or new (0xfd3d) UUID;
# byte 0 is the model, 'H' for a Bot.
ADV_UUIDS = ("0000fd3d-0000-1000-8000-00805f9b34fb", "00000d00-0000-1000-8000-00805f9b34fb")
BOT_MODEL = 0x48

CMD_PRESS = "570100"
CMD_INFO = "5702"
CMD_SET_HOLD = "570f08"

STATUS = {
    0x02: "error while executing",
    0x03: "busy",
    0x04: "protocol version incompatible",
    0x05: "command not supported (is the Bot in switch mode? use press mode)",
    0x06: "low battery",
    0x07: "the Bot has a password; set it here",
    0x08: "the Bot has no password; clear it here",
    0x09: "wrong password",
    0x0A: "encryption method not supported",
}

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
HOLD_MAX = 60
FIND_TIMEOUT = 10.0
CONNECT_ATTEMPTS = 3

log = logging.getLogger("switchbot")


class BotError(Exception):
    pass


class BluezError(BotError):
    def __init__(self, name, text=""):
        super().__init__(f"{text} ({name})" if text else name)
        self.name = name


def encode(key, password):
    # With a password set in the SwitchBot app, the command nibble moves into
    # byte 1 and the CRC32 of the password follows it.
    if password:
        crc = binascii.crc32(password.encode("ascii")) & 0xFFFFFFFF
        key = "571" + key[3] + f"{crc:08x}" + key[4:]
    return bytes.fromhex(key)


def parse_info(resp):
    if len(resp) < 11:
        raise BotError(f"short info reply from the Bot: {resp.hex()}")
    return {
        "battery": resp[1],
        "firmware": resp[2] / 10,
        "hold_seconds": resp[10],
        "switch_mode": bool(resp[9] & 0x10),
    }


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    return {
        "mac": str(raw.get("mac", "")).upper(),
        "password": str(raw.get("password", "")),
        "hold_seconds": int(raw.get("hold_seconds", 5)),
        "adapter": str(raw.get("adapter", "")),
    }


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)


def public_config(cfg):
    return {
        "mac": cfg["mac"],
        "configured": bool(cfg["mac"]),
        "has_password": bool(cfg["password"]),
        "hold_seconds": cfg["hold_seconds"],
    }


def update_config(mac=None, password=None, hold_seconds=None):
    cfg = load_config()
    if mac is not None:
        mac = mac.strip().upper()
        if mac and not MAC_RE.match(mac):
            raise ValueError(f"not a Bluetooth address: {mac!r}")
        cfg["mac"] = mac
    if password is not None:
        if not password.isascii():
            raise ValueError("the password must be ASCII")
        cfg["password"] = password
    if hold_seconds is not None:
        hold_seconds = int(hold_seconds)
        if not 1 <= hold_seconds <= HOLD_MAX:
            raise ValueError(f"hold must be 1-{HOLD_MAX} seconds")
        cfg["hold_seconds"] = hold_seconds
    save_config(cfg)
    return public_config(cfg)


def usb_adapter(path):
    hci = path.rsplit("/", 1)[-1]
    return os.path.basename(os.path.realpath(f"/sys/class/bluetooth/{hci}/device/subsystem")) == "usb"


class AuthFromCredentials(AuthExternal):
    # Claim no uid and let the bus use the socket's peer credentials. Inside
    # an unprivileged container getuid() is 0 while the host bus sees the
    # mapped uid, so the usual claim is rejected.
    def _authentication_start(self, negotiate_unix_fd=False):
        self.negotiate_unix_fd = negotiate_unix_fd
        return "AUTH EXTERNAL"

    def _receive_line(self, line):
        if line.startswith("DATA"):
            return "DATA"
        return super()._receive_line(line)


async def system_bus():
    address = os.environ.get("DBUS_SYSTEM_BUS_ADDRESS")
    if not address and os.path.exists(HOST_DBUS):
        address = f"unix:path={HOST_DBUS}"
    return await MessageBus(bus_address=address, bus_type=BusType.SYSTEM,
                            auth=AuthFromCredentials()).connect()


class Bluez:
    def __init__(self, bus):
        self.bus = bus

    async def call(self, path, interface, member, signature="", body=None, timeout=30.0):
        msg = Message(destination="org.bluez", path=path, interface=interface, member=member,
                      signature=signature, body=body or [])
        reply = await asyncio.wait_for(self.bus.call(msg), timeout)
        if reply.message_type == MessageType.ERROR:
            raise BluezError(reply.error_name, reply.body[0] if reply.body else "")
        return reply.body

    async def objects(self):
        (objs,) = await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        return objs

    async def get(self, path, interface, prop):
        (value,) = await self.call(path, PROPS, "Get", "ss", [interface, prop])
        return value.value

    async def set(self, path, interface, prop, variant):
        await self.call(path, PROPS, "Set", "ssv", [interface, prop, variant])

    async def adapter(self, want=""):
        objs = await self.objects()
        paths = sorted(p for p, ifaces in objs.items() if ADAPTER in ifaces)
        if want:
            path = want if want.startswith("/") else f"/org/bluez/{want}"
            if path not in paths:
                raise BotError(f"Bluetooth adapter {want} not found")
        elif not paths:
            raise BotError("no Bluetooth adapter found")
        else:
            path = ([p for p in paths if usb_adapter(p)] or paths)[0]
        if not objs[path][ADAPTER]["Powered"].value:
            await self.set(path, ADAPTER, "Powered", Variant("b", True))
            await asyncio.sleep(1)
        return path

    @contextlib.asynccontextmanager
    async def discovering(self, adapter):
        await self.call(adapter, ADAPTER, "SetDiscoveryFilter", "a{sv}",
                        [{"Transport": Variant("s", "le"), "DuplicateData": Variant("b", False)}])
        await self.call(adapter, ADAPTER, "StartDiscovery")
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await self.call(adapter, ADAPTER, "StopDiscovery")

    async def exists(self, path):
        try:
            await self.get(path, DEVICE, "Address")
            return True
        except BluezError:
            return False

    async def find_device(self, adapter, mac):
        path = f"{adapter}/dev_{mac.replace(':', '_')}"
        if await self.exists(path):
            return path
        async with self.discovering(adapter):
            deadline = time.monotonic() + FIND_TIMEOUT
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                if await self.exists(path):
                    return path
        raise BotError(f"Bot {mac} not seen in {FIND_TIMEOUT:.0f}s (out of range or flat battery?)")

    async def connect(self, path):
        try:
            await self.call(path, DEVICE, "Connect", timeout=20)
        except BluezError as e:
            if e.name != "org.bluez.Error.AlreadyConnected":
                raise
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                await self.call(path, DEVICE, "Disconnect", timeout=5)
            raise BluezError("Timeout", "Bluetooth connect timed out")
        deadline = time.monotonic() + 10
        while not await self.get(path, DEVICE, "ServicesResolved"):
            if time.monotonic() > deadline:
                raise BluezError("Timeout", "connected, but the Bot's services never resolved")
            await asyncio.sleep(0.2)

    async def characteristics(self, dev_path):
        chars = {}
        for path, ifaces in (await self.objects()).items():
            if path.startswith(dev_path + "/") and CHAR in ifaces:
                props = ifaces[CHAR]
                chars[props["UUID"].value.lower()] = (path, list(props["Flags"].value))
        return chars


class Notifications:
    def __init__(self, bus, bluez, path):
        self.bus, self.bluez, self.path = bus, bluez, path
        self.queue = asyncio.Queue()
        self.rule = (f"type='signal',sender='org.bluez',interface='{PROPS}',"
                     f"member='PropertiesChanged',path='{path}'")

    def _handler(self, msg):
        if (msg.message_type == MessageType.SIGNAL and msg.path == self.path
                and msg.member == "PropertiesChanged" and msg.body and msg.body[0] == CHAR):
            value = msg.body[1].get("Value")
            if value is not None:
                self.queue.put_nowait(bytes(value.value))

    async def _dbus(self, member, rule):
        await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                    interface="org.freedesktop.DBus", member=member,
                                    signature="s", body=[rule]))

    async def start(self):
        await self._dbus("AddMatch", self.rule)
        self.bus.add_message_handler(self._handler)
        await self.bluez.call(self.path, CHAR, "StartNotify")

    async def stop(self):
        with contextlib.suppress(Exception):
            await self.bluez.call(self.path, CHAR, "StopNotify", timeout=5)
        self.bus.remove_message_handler(self._handler)
        with contextlib.suppress(Exception):
            await self._dbus("RemoveMatch", self.rule)

    async def next(self, timeout):
        return await asyncio.wait_for(self.queue.get(), timeout)

    def drain(self):
        while not self.queue.empty():
            self.queue.get_nowait()


class BotSession:
    """One BLE connection to the configured Bot."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.bus = self.dev = self.notify = None

    async def __aenter__(self):
        if not self.cfg["mac"]:
            raise BotError("no Bot configured; scan and pick one in Setup")
        self.bus = await system_bus()
        try:
            await self._open()
        except BaseException:
            await self._close()
            raise
        return self

    async def __aexit__(self, *exc):
        await self._close()

    async def _open(self):
        self.bluez = Bluez(self.bus)
        adapter = await self.bluez.adapter(self.cfg["adapter"])
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            self.dev = await self.bluez.find_device(adapter, self.cfg["mac"])
            try:
                await self.bluez.connect(self.dev)
                break
            except BluezError as e:
                log.info("connect attempt %d failed: %s", attempt, e)
                if attempt == CONNECT_ATTEMPTS:
                    raise BotError(f"could not connect to the Bot: {e}")
                await asyncio.sleep(1)
        chars = await self.bluez.characteristics(self.dev)
        if WRITE_UUID not in chars or NOTIFY_UUID not in chars:
            raise BotError(f"{self.cfg['mac']} is not a SwitchBot Bot (control characteristics missing)")
        self.write_path, flags = chars[WRITE_UUID]
        self.write_type = "command" if "write-without-response" in flags else "request"
        self.notify = Notifications(self.bus, self.bluez, chars[NOTIFY_UUID][0])
        await self.notify.start()

    async def _close(self):
        if self.notify:
            await self.notify.stop()
        if self.dev:
            with contextlib.suppress(Exception):
                await self.bluez.call(self.dev, DEVICE, "Disconnect", timeout=5)
        self.bus.disconnect()

    async def send(self, key, ok=(0x01,)):
        self.notify.drain()
        await self.bluez.call(self.write_path, CHAR, "WriteValue", "aya{sv}",
                              [encode(key, self.cfg["password"]), {"type": Variant("s", self.write_type)}],
                              timeout=10)
        try:
            resp = await self.notify.next(5)
        except asyncio.TimeoutError:
            raise BotError("the Bot did not answer")
        if not resp or resp[0] not in ok:
            code = resp[0] if resp else None
            raise BotError("Bot replied: " + STATUS.get(code, f"status {code}"))
        return resp


async def act_info(cfg):
    async with BotSession(cfg) as s:
        info = parse_info(await s.send(CMD_INFO))
    return {"message": f"battery {info['battery']}%, firmware {info['firmware']}", **info}


async def act_press(cfg):
    async with BotSession(cfg) as s:
        info = parse_info(await s.send(CMD_INFO))
        # A hold left behind by an interrupted `hold` would turn this press
        # into a forced power-off.
        if info["hold_seconds"]:
            await s.send(CMD_SET_HOLD + "00")
        await s.send(CMD_PRESS)
    return {"message": "pressed", "battery": info["battery"]}


async def act_hold(cfg, seconds):
    if not 1 <= seconds <= HOLD_MAX:
        raise BotError(f"hold must be 1-{HOLD_MAX} seconds")
    result = {"message": f"held for {seconds}s", "seconds": seconds}
    async with BotSession(cfg) as s:
        result["battery"] = parse_info(await s.send(CMD_INFO))["battery"]
        await s.send(CMD_SET_HOLD + f"{seconds:02x}")
        try:
            await s.send(CMD_PRESS)
            await asyncio.sleep(seconds + 1.5)
        finally:
            try:
                await s.send(CMD_SET_HOLD + "00")
            except BotError as e:
                log.warning("could not reset the Bot's hold time: %s", e)
                result["warning"] = "could not reset the Bot's hold time; the next press will fix it"
    return result


async def act_scan(cfg, seconds):
    bus = await system_bus()
    try:
        bluez = Bluez(bus)
        adapter = await bluez.adapter(cfg["adapter"])
        async with bluez.discovering(adapter):
            await asyncio.sleep(seconds)
            objs = await bluez.objects()
    finally:
        bus.disconnect()
    bots = []
    for path, ifaces in objs.items():
        dev = ifaces.get(DEVICE)
        # RSSI is only present for devices heard during this discovery.
        if not dev or not path.startswith(adapter + "/") or "RSSI" not in dev:
            continue
        service_data = dev["ServiceData"].value if "ServiceData" in dev else {}
        data = next((bytes(service_data[u].value) for u in ADV_UUIDS if u in service_data), b"")
        if len(data) < 3 or data[0] & 0x7F != BOT_MODEL:
            continue
        bots.append({
            "mac": dev["Address"].value,
            "rssi": dev["RSSI"].value,
            "battery": data[2] & 0x7F,
            "switch_mode": bool(data[1] & 0x80),
            "configured": dev["Address"].value == cfg["mac"],
        })
    bots.sort(key=lambda b: -b["rssi"])
    return {"message": f"found {len(bots)} Bot(s)", "bots": bots}


def make_app(token):
    from aiohttp import web

    lock = asyncio.Lock()
    state = {"last": None}

    async def run(action, fn):
        if lock.locked():
            return {"ok": False, "action": action, "message": "busy with another Bluetooth action"}
        async with lock:
            started = time.monotonic()
            try:
                result = {"ok": True, **await asyncio.wait_for(fn(), 120)}
            except BotError as e:
                result = {"ok": False, "message": str(e)}
            except asyncio.TimeoutError:
                result = {"ok": False, "message": "timed out"}
            except Exception as e:
                log.exception("%s failed", action)
                result = {"ok": False, "message": f"internal error: {e}"}
        result.update(action=action, elapsed=round(time.monotonic() - started, 1), at=int(time.time()))
        log.info("%s: %s", action, json.dumps(result))
        if action != "scan":
            state["last"] = result
        return result

    async def body(request):
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="invalid JSON")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="expected a JSON object")
        return data

    @web.middleware
    async def auth(request, handler):
        given = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(given, f"Bearer {token}".encode()):
            raise web.HTTPUnauthorized(text="bad or missing token")
        # The injected UI always sends this header; a cross-site form or
        # fetch can't without a CORS preflight, which the Comet's nginx
        # doesn't answer.
        if request.method == "POST" and request.headers.get("X-Requested-With") != "switchbot":
            raise web.HTTPForbidden(text="missing X-Requested-With")
        return await handler(request)

    async def status(request):
        return web.json_response({"version": VERSION, **public_config(load_config()), "last": state["last"]})

    async def press(request):
        return web.json_response(await run("press", lambda: act_press(load_config())))

    async def hold(request):
        cfg = load_config()
        seconds = (await body(request)).get("seconds", cfg["hold_seconds"])
        if not isinstance(seconds, int):
            raise web.HTTPBadRequest(text="seconds must be an integer")
        return web.json_response(await run("hold", lambda: act_hold(cfg, seconds)))

    async def info(request):
        return web.json_response(await run("info", lambda: act_info(load_config())))

    async def scan(request):
        seconds = (await body(request)).get("seconds", 8)
        if not isinstance(seconds, (int, float)) or not 1 <= seconds <= 30:
            raise web.HTTPBadRequest(text="seconds must be 1-30")
        return web.json_response(await run("scan", lambda: act_scan(load_config(), seconds)))

    async def config(request):
        data = await body(request)
        try:
            cfg = update_config(data.get("mac"), data.get("password"), data.get("hold_seconds"))
        except (TypeError, ValueError) as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"ok": True, **cfg})

    app = web.Application(middlewares=[auth])
    app.add_routes([
        web.get("/api/status", status),
        web.post("/api/press", press),
        web.post("/api/hold", hold),
        web.post("/api/info", info),
        web.post("/api/scan", scan),
        web.post("/api/config", config),
    ])
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the HTTP API (SWITCHBOT_LISTEN, default 127.0.0.1:8779)")
    p = sub.add_parser("scan", help="list nearby Bots")
    p.add_argument("--seconds", type=float, default=8)
    for name in ("press", "info"):
        sub.add_parser(name).add_argument("--mac")
    p = sub.add_parser("hold", help="press and hold (hard power-off)")
    p.add_argument("seconds", type=int, nargs="?")
    p.add_argument("--mac")
    p = sub.add_parser("config", help="show or change the saved settings")
    p.add_argument("--mac")
    p.add_argument("--password")
    p.add_argument("--hold-seconds", type=int)
    args = parser.parse_args()

    if args.cmd == "serve":
        from aiohttp import web
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        try:
            with open(TOKEN_PATH) as f:
                token = f.read().strip()
        except FileNotFoundError:
            token = ""
        if len(token) < 32:
            sys.exit(f"{TOKEN_PATH}: need a token of at least 32 characters")
        host, _, port = LISTEN.rpartition(":")
        log.info("switchbotd %s listening on %s", VERSION, LISTEN)
        web.run_app(make_app(token), host=host, port=int(port), print=None, access_log=None)
        return 0

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.cmd == "config":
            if args.mac is None and args.password is None and args.hold_seconds is None:
                result = {"ok": True, **public_config(load_config())}
            else:
                result = {"ok": True, **update_config(args.mac, args.password, args.hold_seconds)}
        else:
            cfg = load_config()
            if getattr(args, "mac", None):
                cfg["mac"] = args.mac.upper()
            if args.cmd == "scan":
                coro = act_scan(cfg, args.seconds)
            elif args.cmd == "press":
                coro = act_press(cfg)
            elif args.cmd == "hold":
                coro = act_hold(cfg, args.seconds or cfg["hold_seconds"])
            else:
                coro = act_info(cfg)
            result = {"ok": True, **asyncio.run(coro)}
    except (BotError, ValueError) as e:
        result = {"ok": False, "message": str(e)}
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
