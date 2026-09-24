"""Runs switchbotd against a fake BlueZ on a private dbus-daemon.

    .venv/bin/python tests/test_switchbotd.py
"""

import asyncio
import binascii
import json
import os
import subprocess
import sys
import tempfile

from dbus_next import PropertyAccess, Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, dbus_property, method

TMP = tempfile.mkdtemp(prefix="switchbot-test-")
os.environ["SWITCHBOT_CONFIG"] = os.path.join(TMP, "config.json")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "node"))
import switchbotd as sb  # noqa: E402

BOT_MAC = "C1:23:45:67:89:AB"
OTHER_MAC = "C0:FF:EE:00:00:01"
ADAPTER_PATH = "/org/bluez/hci0"
BOT_PATH = f"{ADAPTER_PATH}/dev_{BOT_MAC.replace(':', '_')}"


class Bot:
    def __init__(self):
        self.hold = 0
        self.presses = []
        self.password = None
        self.fail_connects = 0

    def handle(self, data):
        assert data[0] == 0x57, data.hex()
        cmd = data[1] & 0x0F
        if data[1] & 0xF0 == 0x10:
            if self.password is None:
                return b"\x08"
            if data[2:6] != (binascii.crc32(self.password.encode()) & 0xFFFFFFFF).to_bytes(4, "big"):
                return b"\x09"
            rest = data[6:]
        else:
            if self.password is not None:
                return b"\x07"
            rest = data[2:]
        if cmd == 0x2:
            return bytes([1, 87, 63, 100, 0, 0, 0, 0, 0, 0, self.hold])
        if cmd == 0xF and rest[:1] == b"\x08":
            self.hold = rest[1]
            return b"\x01"
        if cmd == 0x1 and rest[:1] == b"\x00":
            self.presses.append(self.hold)
            return b"\x01\xff\x00"
        return b"\x05"


class NotifyChar(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.GattCharacteristic1")
        self.value = b""
        self.notifying = False

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return sb.NOTIFY_UUID

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return ["notify"]

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self.value

    @method()
    def StartNotify(self):
        self.notifying = True

    @method()
    def StopNotify(self):
        self.notifying = False

    def notify(self, data):
        self.value = data
        if self.notifying:
            self.emit_properties_changed({"Value": data})


class WriteChar(ServiceInterface):
    def __init__(self, bot, notify):
        super().__init__("org.bluez.GattCharacteristic1")
        self.bot, self.notify = bot, notify
        self.write_types = []

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return sb.WRITE_UUID

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return ["read", "write-without-response", "write"]

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        self.write_types.append(options["type"].value)
        reply = self.bot.handle(bytes(value))
        asyncio.get_running_loop().call_later(0.05, self.notify.notify, reply)


class Device(ServiceInterface):
    def __init__(self, bus, path, mac, data, bot):
        super().__init__("org.bluez.Device1")
        self.bus, self.path, self.mac, self.data, self.bot = bus, path, mac, data, bot
        self.connected = False
        self.resolved = False
        self.chars = []

    @dbus_property(access=PropertyAccess.READ)
    def Address(self) -> "s":
        return self.mac

    @dbus_property(access=PropertyAccess.READ)
    def RSSI(self) -> "n":
        return -61

    @dbus_property(access=PropertyAccess.READ)
    def ServiceData(self) -> "a{sv}":
        return {sb.ADV_UUIDS[0]: Variant("ay", self.data)}

    @dbus_property(access=PropertyAccess.READ)
    def ServicesResolved(self) -> "b":
        return self.resolved

    @method()
    async def Connect(self):
        if self.bot.fail_connects:
            self.bot.fail_connects -= 1
            raise DBusError("org.bluez.Error.Failed", "le-connection-abort-by-local")
        self.connected = True
        notify = NotifyChar()
        write = WriteChar(self.bot, notify)
        svc = f"{self.path}/service0010"
        self.bus.export(f"{svc}/char0011", write)
        self.bus.export(f"{svc}/char0013", notify)
        self.chars = [f"{svc}/char0011", f"{svc}/char0013"]
        self.write = write
        asyncio.get_running_loop().call_later(0.2, setattr, self, "resolved", True)

    @method()
    def Disconnect(self):
        self.connected = self.resolved = False
        for path in self.chars:
            self.bus.unexport(path)
        self.chars = []


class Adapter(ServiceInterface):
    def __init__(self, bus, bot):
        super().__init__("org.bluez.Adapter1")
        self.bus, self.bot = bus, bot
        self.discovering = 0
        self.advertising = True
        self.devices = {}

    @dbus_property(access=PropertyAccess.READ)
    def Powered(self) -> "b":
        return True

    @method()
    def SetDiscoveryFilter(self, props: "a{sv}"):
        assert props["Transport"].value == "le"

    @method()
    def StartDiscovery(self):
        self.discovering += 1
        if self.advertising:
            asyncio.get_running_loop().call_later(0.6, self.appear)

    @method()
    def StopDiscovery(self):
        self.discovering -= 1

    def appear(self):
        for mac, data in ((BOT_MAC, b"H\x00\xd7"), (OTHER_MAC, b"c\x00\x10")):
            path = f"{ADAPTER_PATH}/dev_{mac.replace(':', '_')}"
            if path in self.devices:
                continue
            dev = Device(self.bus, path, mac, data, self.bot)
            self.devices[path] = dev
            self.bus.export(path, dev)

    def forget(self):
        for path in list(self.devices):
            self.bus.unexport(path)
        self.devices.clear()


async def main():
    sock = os.path.join(TMP, "bus")
    daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--nopidfile",
                               f"--address=unix:path={sock}"])
    address = f"unix:path={sock}"
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = address
    for _ in range(50):
        if os.path.exists(sock):
            break
        await asyncio.sleep(0.1)
    try:
        await run(address)
    finally:
        daemon.terminate()


async def run(address):
    bus = await MessageBus(bus_address=address).connect()
    bot = Bot()
    adapter = Adapter(bus, bot)
    bus.export(ADAPTER_PATH, adapter)
    await bus.request_name("org.bluez")
    cfg = sb.load_config()

    scan = await sb.act_scan(cfg, 1.5)
    assert [b["mac"] for b in scan["bots"]] == [BOT_MAC], scan
    assert scan["bots"][0]["battery"] == 0x57, scan
    assert adapter.discovering == 0, "discovery left running"
    print("scan ok:", scan["message"])

    sb.update_config(mac=BOT_MAC)
    cfg = sb.load_config()

    info = await sb.act_info(cfg)
    assert info["battery"] == 87 and info["firmware"] == 6.3, info
    print("info ok:", info["message"])

    dev = adapter.devices[BOT_PATH]
    assert not dev.connected and not dev.chars, "session left the Bot connected"

    bot.hold = 7
    await sb.act_press(cfg)
    assert bot.presses == [0], f"press ran with hold {bot.presses}"
    print("press ok (a stale hold of 7s was cleared first)")

    bot.presses.clear()
    res = await sb.act_hold(cfg, 1)
    assert bot.presses == [1] and bot.hold == 0, (bot.presses, bot.hold)
    assert "warning" not in res, res
    print("hold ok: pressed with hold 1s, reset to 0")

    bot.fail_connects = 2
    await sb.act_press(cfg)
    print("press ok after 2 failed connects")

    bot.password = "hunter2"
    try:
        await sb.act_press(cfg)
        raise AssertionError("press worked without the password")
    except sb.BotError as e:
        assert "has a password" in str(e), e
    sb.update_config(password="wrong")
    try:
        await sb.act_press(sb.load_config())
        raise AssertionError("press worked with a wrong password")
    except sb.BotError as e:
        assert "wrong password" in str(e), e
    sb.update_config(password="hunter2")
    await sb.act_press(sb.load_config())
    print("password ok: missing and wrong passwords reported, right one works")
    bot.password = None
    sb.update_config(password="")

    assert set(dev.write.write_types) == {"command"}, dev.write.write_types

    adapter.forget()
    adapter.advertising = False
    sb.FIND_TIMEOUT = 1.5
    try:
        await sb.act_press(sb.load_config())
        raise AssertionError("press worked with the Bot out of range")
    except sb.BotError as e:
        assert "not seen" in str(e), e
        missing = str(e)
    assert adapter.discovering == 0, "discovery left running"
    adapter.advertising = True
    print("out-of-range ok:", missing)

    await http_checks()
    print("ALL OK")


async def http_checks():
    from aiohttp.test_utils import TestClient, TestServer

    token = "t" * 40
    client = TestClient(TestServer(sb.make_app(token)))
    await client.start_server()
    try:
        auth = {"Authorization": f"Bearer {token}"}
        ui = {**auth, "X-Requested-With": "switchbot"}
        assert (await client.get("/api/status")).status == 401
        assert (await client.get("/api/status", headers={"Authorization": "Bearer nope"})).status == 401
        r = await client.get("/api/status", headers=auth)
        status = await r.json()
        assert r.status == 200 and status["mac"] == BOT_MAC and "password" not in status, status
        assert (await client.post("/api/press", headers=auth)).status == 403
        r = await client.post("/api/press", headers=ui)
        body = await r.json()
        assert body["ok"] and body["action"] == "press", body
        r = await client.post("/api/config", headers=ui, data=json.dumps({"mac": "nope"}))
        assert r.status == 400, r.status
        r = await client.post("/api/hold", headers=ui, data=json.dumps({"seconds": "5"}))
        assert r.status == 400, r.status
        r = await client.get("/api/status", headers=auth)
        assert (await r.json())["last"]["action"] == "press"
        print("http ok: token, CSRF header, validation, last result")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
