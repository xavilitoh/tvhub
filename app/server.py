#!/usr/bin/env python3
"""TVHub server: one web remote for several TVs.

Drivers
  android  Fire TV / Google TV / Android TV over ADB: remote keys, text, apps, snapshots and live
           H.264 video (`screenrecord`).
  vidaa    Hisense VIDAA over its built-in MQTT broker (via the `vidaa-control` library): remote
           keys, volume, PIN pairing. VIDAA exposes no screen, so no video.

HTTP / WebSocket
  /api/tvs ...        list, add, edit, delete, pair, apps, launch, snapshot
  /api/discover       find TVs on the network (GET: suggested networks, POST: scan)
  /ws/control         commands in (each carries the target TV id), status of every TV out
  /ws/video?tv=<id>   live video of an android TV, one WebSocket message per frame
"""
import asyncio
import json
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import web, WSMsgType

DATA = os.environ.get("DATA_DIR", "/data")
ADB = os.environ.get("ADB_BIN", "adb")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TVS_PATH = os.path.join(DATA, "tvs.json")
OLD_CFG_PATH = os.path.join(DATA, "config.json")      # v0.1 single-TV config, migrated on first start
TOKEN = os.environ.get("TVHUB_TOKEN") or os.environ.get("TVDECK_TOKEN", "")      # TVDECK_TOKEN: name used before the rename

KEY_RE = re.compile(r"^[A-Z0-9_]{2,40}$")
PKG_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,120}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,100}$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
VOLUME_KEYS = {"VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE"}
DEFAULT_PORTS = {"android": 5555, "vidaa": 36669, "webos": 3001}

# Friendly names + brand colours for Android apps we know. Anything else is shown by package name.
KNOWN_APPS = {
    "com.netflix.ninja": ("Netflix", "#e50914"),
    "com.amazon.firetv.youtube": ("YouTube", "#ff0000"),
    "com.google.android.youtube.tv": ("YouTube", "#ff0000"),
    "com.amazon.firebat": ("Prime Video", "#00a8e1"),
    "com.disney.disneyplus": ("Disney+", "#113ccf"),
    "com.hbo.hbonow": ("HBO Max", "#5822b4"),
    "com.wbd.stream": ("Max", "#002be7"),
    "com.crunchyroll.crunchyroid": ("Crunchyroll", "#f47521"),
    "org.jellyfin.androidtv": ("Jellyfin", "#7b5fd3"),
    "com.plexapp.android": ("Plex", "#e5a00d"),
    "com.spotify.tv.android": ("Spotify", "#1db954"),
    "com.hulu.plus": ("Hulu", "#1ce783"),
    "com.tubitv.ott": ("Tubi", "#fa541c"),
    "com.apple.atve.amazon.appletv": ("Apple TV", "#555a64"),
    "org.xbmc.kodi": ("Kodi", "#17b2e7"),
    "com.esaba.downloader": ("Downloader", "#2e7d32"),
    "com.formulaone.production": ("F1 TV", "#e10600"),
    "com.amazon.cloud9": ("Silk", "#ff9900"),
    "tv.twitch.android.app": ("Twitch", "#9146ff"),
}
HIDDEN_PREFIXES = ("com.amazon.tv.", "com.amazon.storm.")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def app_label(pkg):
    if not pkg:
        return ""
    if pkg in KNOWN_APPS:
        return KNOWN_APPS[pkg][0]
    if pkg.startswith("com.amazon.tv.launcher"):
        return "Inicio"
    return pkg.split(".")[-1].capitalize()


# --------------------------------------------------------------------------- hub / base driver

class Hub:
    def __init__(self):
        self.tvs = {}            # id -> driver
        self.clients = set()     # control WebSockets

    async def broadcast(self, payload):
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:
                self.clients.discard(ws)

    def save(self):
        try:
            os.makedirs(DATA, exist_ok=True)
            tmp = TVS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump([d.cfg for d in self.tvs.values()], f, indent=1)
            os.replace(tmp, TVS_PATH)
        except OSError as e:
            log("cannot save TV list:", e)


hub = Hub()
ha = None                                   # Home Assistant bridge (see ha.py), created at startup


class Driver:
    type = ""
    caps = {"video": False, "apps": False, "text": False, "snapshot": False, "pair": False}

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = "disconnected"   # disconnected | unauthorized | needs_pairing | device
        self.model = ""
        self.tasks = []
        self._last = None

    @property
    def id(self):
        return self.cfg["id"]

    @property
    def host(self):
        return self.cfg["host"]

    @property
    def port(self):
        return int(self.cfg.get("port") or DEFAULT_PORTS[self.type])

    def extra(self):
        return {}

    def status(self):
        return {"id": self.id, "name": self.cfg.get("name") or self.host, "type": self.type,
                "host": self.host, "port": self.port, "state": self.state, "model": self.model,
                "caps": self.caps, "volume_via": self.cfg.get("volume_via") or "",
                "mac": self.cfg.get("mac") or "", "tls": self.cfg.get("tls", True), **self.extra()}

    async def publish(self, force=False):
        snap = self.status()
        if force or snap != self._last:
            self._last = snap
            await hub.broadcast({"t": "status", **snap})
            if ha:
                ha.update(snap)

    async def start(self):
        self.tasks = [asyncio.create_task(self.run())]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.tasks = []

    async def run(self):
        raise NotImplementedError

    # unsupported by default
    def key(self, name):
        pass

    def text(self, s):
        pass

    async def apps(self):
        return []

    async def launch(self, pkg):
        pass

    async def snapshot(self):
        return None

    async def power(self, on):
        self.key("POWER")

    async def set_volume(self, level):
        raise NotImplementedError


# --------------------------------------------------------------------------- H.264 framing

SC = b"\x00\x00\x00\x01"
FLUSH_AFTER = 0.04    # seconds of silence before a buffered tail NAL/picture is released


class NalSplitter:
    """Splits an Annex B byte stream into NAL units (without start codes).

    A NAL is only known to be complete once the next start code arrives, so the last one stays in
    the buffer. screenrecord emits nothing while the screen is static, which would hold the final
    update back indefinitely: flush() releases that tail once the stream has gone quiet."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        b = self.buf
        idxs = []
        pos = b.find(b"\x00\x00\x01")
        while pos != -1:
            idxs.append(pos)
            pos = b.find(b"\x00\x00\x01", pos + 3)
        if len(idxs) < 2:
            return []
        nals = []
        for a, c in zip(idxs, idxs[1:]):
            nal = bytes(b[a + 3:c]).rstrip(b"\x00")  # a NAL never ends in 0x00
            if nal:
                nals.append(nal)
        del b[:idxs[-1]]
        return nals

    def has_tail(self):
        return len(self.buf) > 4

    def flush(self):
        b, self.buf = self.buf, bytearray()
        if b[:3] != b"\x00\x00\x01":
            return []
        nal = bytes(b[3:]).rstrip(b"\x00")
        return [nal] if nal else []


class FrameBuilder:
    """NAL units in, one WebSocket message per picture out.

    Message = flag byte + Annex B access unit: b'\\x01' + SPS + PPS + slices for a keyframe,
    b'\\x00' + slices otherwise. A picture can be split in several slices (hardware encoders do
    that); a slice with first_mb_in_slice == 0 starts a new picture."""

    def __init__(self):
        self.sps = None
        self.pps = None
        self.cur = None          # [is_key, [slice NALs]]

    def pending(self):
        return self.cur is not None

    def _emit(self):
        cur, self.cur = self.cur, None
        if not cur:
            return []
        key, slices = cur
        if key:
            if not (self.sps and self.pps):
                return []
            head = b"\x01" + SC + self.sps + SC + self.pps
        else:
            head = b"\x00"
        return [head + b"".join(SC + n for n in slices)]

    def push(self, nal):
        t = nal[0] & 0x1F
        if t in (7, 8):
            out = self._emit()
            if t == 7:
                self.sps = nal
            else:
                self.pps = nal
            return out
        if t in (1, 5):
            first = len(nal) > 1 and bool(nal[1] & 0x80)     # first_mb_in_slice == ue(0)
            out = self._emit() if (first or self.cur is None) else []
            if self.cur is None:
                self.cur = [t == 5, []]
            self.cur[1].append(nal)
            return out
        return []                                             # SEI, AUD, ...

    def flush(self):
        return self._emit()



# --------------------------------------------------------------------------- android driver

class Streamer:
    def __init__(self, tv):
        self.tv = tv
        self.clients = set()
        self.task = None
        self.proc = None
        self.size = "1280x720"
        self.bitrate = 4_000_000

    async def add(self, ws):
        self.clients.add(ws)
        await self.restart()          # a fresh start guarantees the new viewer gets a keyframe

    async def remove(self, ws):
        self.clients.discard(ws)
        if not self.clients:
            await self.stop()

    async def restart(self):
        await self.stop()
        if self.clients:
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        t, self.task = self.task, None
        if t:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self._kill()

    async def _kill(self):
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            p.kill()
            try:
                await p.wait()
            except Exception:
                pass
        if self.tv.state == "device":
            await self.tv.adb("-s", self.tv.serial, "shell", "pkill -2 screenrecord", timeout=5)

    async def run(self):
        tv = self.tv
        while self.clients:
            if tv.state != "device":
                await asyncio.sleep(1)
                continue
            cmd = [ADB, "-s", tv.serial, "exec-out", "screenrecord",
                   "--output-format=h264", f"--size={self.size}",
                   f"--bit-rate={self.bitrate}", "--time-limit=170", "-"]
            log(f"[{tv.cfg.get('name')}] screenrecord:", " ".join(cmd[3:]))
            p = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self.proc = p
            splitter, builder = NalSplitter(), FrameBuilder()
            try:
                while True:
                    quiet = splitter.has_tail() or builder.pending()
                    try:
                        chunk = await asyncio.wait_for(p.stdout.read(65536), FLUSH_AFTER if quiet else None)
                    except asyncio.TimeoutError:
                        chunk = None                       # stream went quiet: release what is buffered
                    if chunk == b"":
                        break
                    frames = []
                    for nal in (splitter.feed(chunk) if chunk else splitter.flush()):
                        frames += builder.push(nal)
                    if chunk is None:
                        frames += builder.flush()
                    for frame in frames:
                        await self.send_all(frame)
            finally:
                if p.returncode is None:
                    p.kill()
                    await p.wait()
            await asyncio.sleep(0.3)      # screenrecord ends every 170 s; reconnect seamlessly

    async def send_all(self, frame):
        for ws in list(self.clients):
            try:
                await ws.send_bytes(frame)
            except Exception:
                self.clients.discard(ws)


class AndroidDriver(Driver):
    type = "android"
    caps = {"video": True, "apps": True, "text": True, "snapshot": True, "pair": False}

    def __init__(self, cfg):
        super().__init__(cfg)
        self.focus = ""
        self.cmdq = asyncio.Queue()
        self.streamer = Streamer(self)

    @property
    def serial(self):
        return f"{self.host}:{self.port}"

    def extra(self):
        return {"focus": self.focus, "app": app_label(self.focus)}

    async def adb(self, *args, timeout=15):
        try:
            p = await asyncio.create_subprocess_exec(
                ADB, *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            return 127, b"", b"adb not found"
        try:
            out, err = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
            return 124, b"", b"timeout"
        return p.returncode, out, err

    async def sh(self, cmd, timeout=15):
        rc, out, _ = await self.adb("-s", self.serial, "shell", cmd, timeout=timeout)
        return rc, out.decode("utf-8", "replace")

    async def device_state(self):
        _, out, _ = await self.adb("devices", timeout=10)
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == self.serial:
                return parts[1]
        return "disconnected"

    async def start(self):
        self.tasks = [asyncio.create_task(self.run()), asyncio.create_task(self.worker())]

    async def stop(self):
        await self.streamer.stop()
        await super().stop()
        if self.host:
            await self.adb("disconnect", self.serial, timeout=5)

    async def run(self):
        while True:
            try:
                st = await self.device_state()
                if st != "device":
                    if st == "offline":
                        await self.adb("disconnect", self.serial, timeout=5)
                    await self.adb("connect", self.serial, timeout=9)
                    st = await self.device_state()
                self.state = st
                if st == "device":
                    if not self.model:
                        _, m = await self.sh("getprop ro.product.model")
                        self.model = m.strip()
                    _, f = await self.sh("dumpsys window | grep mCurrentFocus")
                    m = re.search(r"\s([A-Za-z0-9_.]+)/", f)
                    self.focus = m.group(1) if m else self.focus
                else:
                    self.model = ""
                    self.focus = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loop alive whatever happens
                log("android monitor:", e)
            await self.publish()
            await asyncio.sleep(3 if self.state == "device" else 5)

    async def worker(self):
        """Run queued shell commands in order; consecutive ones go in a single adb call."""
        while True:
            batch = [await self.cmdq.get()]
            while not self.cmdq.empty() and len(batch) < 12:
                batch.append(self.cmdq.get_nowait())
            if self.state == "device":
                await self.sh("; ".join(batch), timeout=20)

    def key(self, name):
        self.cmdq.put_nowait(f"input keyevent KEYCODE_{name}")

    def text(self, s):
        s = "".join(ch for ch in s if 32 <= ord(ch) < 127)[:200]
        s = s.replace("'", "'\\''").replace(" ", "%s")
        if s:
            self.cmdq.put_nowait(f"input text '{s}'")

    async def apps(self):
        if self.state != "device":
            return []
        _, third_out = await self.sh("pm list packages -3")
        _, all_out = await self.sh("pm list packages")

        def pk(out):
            return {l[8:].strip() for l in out.splitlines() if l.startswith("package:")}
        third = pk(third_out)
        installed = pk(all_out) | third
        res = [{"pkg": p, "name": n, "color": c, "known": True}
               for p, (n, c) in KNOWN_APPS.items() if p in installed]
        res += [{"pkg": p, "name": app_label(p), "color": "#3a3f4b", "known": False}
                for p in sorted(third)
                if p not in KNOWN_APPS and not p.startswith(HIDDEN_PREFIXES) and ".overlay." not in p]
        return res

    async def launch(self, pkg):
        if not PKG_RE.match(pkg):
            return
        _, out = await self.sh(f"monkey -p {pkg} -c android.intent.category.LEANBACK_LAUNCHER 1")
        if "No activities found" in out or "monkey aborted" in out:
            await self.sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")

    async def power(self, on):
        self.key("WAKEUP" if on else "SLEEP")

    async def snapshot(self):
        if self.state != "device":
            return None
        rc, out, _ = await self.adb("-s", self.serial, "exec-out", "screencap", "-p", timeout=20)
        return out if rc == 0 and out else None


# --------------------------------------------------------------------------- vidaa driver

try:                                    # optional: Android-only installs work without it
    import vidaa as _vidaa
    from vidaa import keys as _vk
    from vidaa.config.storage import TokenStorage as _TokenStorage
except Exception as _e:                 # noqa: BLE001
    _vidaa = None
    log("vidaa-control not available:", _e)

# canonical (Android-style) key names -> VIDAA key constants
VIDAA_KEYS = {}
if _vidaa:
    VIDAA_KEYS = {
        "DPAD_UP": _vk.KEY_UP, "DPAD_DOWN": _vk.KEY_DOWN, "DPAD_LEFT": _vk.KEY_LEFT,
        "DPAD_RIGHT": _vk.KEY_RIGHT, "DPAD_CENTER": _vk.KEY_OK, "ENTER": _vk.KEY_OK,
        "BACK": _vk.KEY_RETURNS, "HOME": _vk.KEY_HOME, "MENU": _vk.KEY_MENU,
        "VOLUME_UP": _vk.KEY_VOLUME_UP, "VOLUME_DOWN": _vk.KEY_VOLUME_DOWN, "VOLUME_MUTE": _vk.KEY_MUTE,
        "MEDIA_REWIND": _vk.KEY_REWIND, "MEDIA_FAST_FORWARD": _vk.KEY_FAST_FORWARD,
        "MEDIA_STOP": _vk.KEY_STOP, "POWER": _vk.KEY_POWER,
        "CHANNEL_UP": _vk.KEY_CHANNEL_UP, "CHANNEL_DOWN": _vk.KEY_CHANNEL_DOWN,
    }


def _up(client):
    """vidaa-control exposes is_connected as a property (a method in some versions): accept both."""
    v = getattr(client, "is_connected", False)
    return bool(v() if callable(v) else v)


class VidaaDriver(Driver):
    type = "vidaa"
    caps = {"video": False, "apps": False, "text": False, "snapshot": False, "pair": True, "volume_set": True}

    def __init__(self, cfg):
        super().__init__(cfg)
        self.pool = ThreadPoolExecutor(max_workers=1)    # one thread: keeps key presses in order
        self.client = None
        self.playing = False
        self.pair_msg = ""

    def extra(self):
        c = self.client
        vol = getattr(c, "cached_volume", None) if c else None
        return {"volume": vol, "muted": bool(getattr(c, "is_muted", False)) if c else False,
                "pair_msg": self.pair_msg}

    def _build(self):
        mac = self.cfg.get("mac") or None
        kw = dict(host=self.host, port=self.port, use_ssl=bool(self.cfg.get("tls", True)),
                  enable_persistence=True,
                  storage=_TokenStorage(Path(DATA) / "vidaa-tokens.json"))
        if mac:                          # newer firmware: dynamic credentials need the TV's MAC
            kw.update(mac_address=mac, use_dynamic_auth=True)
        return _vidaa.VidaaTV(**kw)

    def _connect(self):
        if self.client is None:
            self.client = self._build()
        return bool(self.client.connect(timeout=8))

    def _ask_volume(self):
        try:
            self.client.get_volume(timeout=3)
        except Exception:
            pass

    async def run(self):
        loop = asyncio.get_running_loop()
        asked = False
        while True:
            try:
                if self.client is None or not _up(self.client):
                    asked = False
                    ok = await loop.run_in_executor(self.pool, self._connect)
                    if not ok:
                        self.state = "disconnected"
                c = self.client
                if c is not None and _up(c):
                    needs = c.needs_authentication() and not c.is_authenticated()
                    self.state = "needs_pairing" if needs else "device"
                    if not self.model:
                        self.model = "Hisense VIDAA"
                    await self.publish()                  # show "connected" before the slow volume query
                    if not asked:
                        asked = True
                        await loop.run_in_executor(self.pool, self._ask_volume)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log("vidaa monitor:", e)
                self.state = "disconnected"
            await self.publish()
            await asyncio.sleep(4 if self.state == "device" else 6)

    async def start(self):
        self.pool = ThreadPoolExecutor(max_workers=1)    # stop() closes the old one; edits restart the driver
        self.client = None
        await super().start()

    async def stop(self):
        await super().stop()
        c, self.client = self.client, None
        if c:
            try:
                c.disconnect()
            except Exception:
                pass
        self.pool.shutdown(wait=False)

    def key(self, name):
        if name in ("MEDIA_PLAY_PAUSE", "MEDIA_PLAY", "MEDIA_PAUSE"):   # VIDAA has separate play and pause keys
            self.playing = (not self.playing) if name == "MEDIA_PLAY_PAUSE" else name == "MEDIA_PLAY"
            k = _vk.KEY_PLAY if self.playing else _vk.KEY_PAUSE
        else:
            k = VIDAA_KEYS.get(name)
        if k and self.client:
            self.pool.submit(self._send, k)

    async def set_volume(self, level):
        if not self.client:
            return
        await asyncio.get_running_loop().run_in_executor(self.pool, lambda: self.client.set_volume(int(level)))

    def _send(self, k):
        try:
            self.client.send_key(k)
        except Exception as e:
            log("vidaa key:", e)

    async def pair(self, pin=None):
        loop = asyncio.get_running_loop()
        if self.client is None or not _up(self.client):
            ok = await loop.run_in_executor(self.pool, self._connect)
            if not ok:
                if not self.cfg.get("mac"):
                    return False, ("La TV rechazó la conexión. Si es un modelo reciente, "
                                   "rellena la MAC de la TV en Ajustes, guarda y vuelve a probar.")
                return False, "No llego a la TV. Comprueba que está encendida, la IP y la casilla de cifrado."
        if not pin:
            ok = await loop.run_in_executor(self.pool, self.client.start_pairing)
            self.pair_msg = "Escribe el PIN que aparece en la TV" if ok else ""
            await self.publish(force=True)
            return ok, self.pair_msg or "No se pudo iniciar el emparejamiento."
        ok = await loop.run_in_executor(self.pool, lambda: self.client.authenticate(pin))
        self.pair_msg = "" if ok else "PIN incorrecto o caducado."
        await self.publish(force=True)
        return ok, "Emparejada." if ok else self.pair_msg


# --------------------------------------------------------------------------- LG webOS driver

try:                                    # optional
    import aiowebostv as _webos
    from aiowebostv import endpoints as _wep
    _webos.webos_client.RECEIVE_TIMEOUT = 35     # pairing: the user has to accept the prompt on the TV
except Exception as _e:                 # noqa: BLE001
    _webos = None
    log("aiowebostv not available:", _e)

WEBOS_BUTTONS = {
    "DPAD_UP": "UP", "DPAD_DOWN": "DOWN", "DPAD_LEFT": "LEFT", "DPAD_RIGHT": "RIGHT",
    "DPAD_CENTER": "ENTER", "ENTER": "ENTER", "BACK": "BACK", "HOME": "HOME", "MENU": "MENU",
    "CHANNEL_UP": "CHANNELUP", "CHANNEL_DOWN": "CHANNELDOWN",
}
WEBOS_COLORS = {"netflix": "#e50914", "youtube.leanback.v4": "#ff0033", "amazon": "#00a8e1",
                "com.disney.disneyplus-prod": "#113ccf", "com.apple.appletv": "#1d1d1f",
                "spotify-beehive": "#1db954", "hbomax": "#5b2bd4", "com.webos.app.hdmi1": "#3a3f4b"}
WOL_PORT = 9


def wake_on_lan(mac):
    import socket
    raw = bytes.fromhex(mac.replace(":", ""))
    pkt = b"\xff" * 6 + raw * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
        so.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        so.sendto(pkt, ("255.255.255.255", WOL_PORT))


class WebosDriver(Driver):
    type = "webos"
    caps = {"video": False, "apps": True, "text": True, "snapshot": False, "pair": True, "volume_set": True}

    def __init__(self, cfg):
        super().__init__(cfg)
        self.client = None
        self.q = None
        self.pair_msg = ""
        self.pairing = False
        self.playing = False

    # -- persisted pairing key (one per TV id)
    def _keys_path(self):
        return Path(DATA) / "webos-keys.json"

    def _load_key(self):
        try:
            with open(self._keys_path()) as f:
                return json.load(f).get(self.id)
        except (OSError, ValueError):
            return None

    def _save_key(self, key):
        try:
            try:
                with open(self._keys_path()) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                d = {}
            d[self.id] = key
            tmp = str(self._keys_path()) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, self._keys_path())
        except OSError as e:
            log("cannot save webOS key:", e)

    def extra(self):
        st = self.client.tv_state if self.client else None
        apps = (st.apps if st else {}) or {}
        cur = st.current_app_id if st else None
        return {"focus": cur or "", "app": (apps.get(cur) or {}).get("title", "") if cur else "",
                "volume": st.volume if st else None, "muted": bool(st.muted) if st else False,
                "on": bool(st.is_on) if st else None, "pair_msg": self.pair_msg}

    async def start(self):
        self.q = asyncio.Queue()
        self.client = None
        self.tasks = [asyncio.create_task(self.run()), asyncio.create_task(self.worker())]

    async def stop(self):
        await super().stop()
        c, self.client = self.client, None
        if c:
            try:
                await c.disconnect()
            except Exception:
                pass

    async def _drop(self):
        c, self.client = self.client, None
        if c:
            try:
                await c.disconnect()
            except Exception:
                pass

    async def _on_state(self, *_a):
        await self.publish()

    async def _open(self, key):
        c = _webos.WebOsClient(self.host, client_key=key)
        ok = await asyncio.wait_for(c.connect(), 45 if not key else 20)
        if not ok:
            raise ConnectionError("connect failed")
        await c.register_state_update_callback(self._on_state)
        return c

    async def run(self):
        fails = 0
        while True:
            try:
                key = self._load_key()
                if self.pairing:
                    pass
                elif not key:
                    self.state = "needs_pairing"
                elif self.client is None or not self.client.is_connected():
                    await self._drop()
                    self.client = await self._open(key)
                    fails = 0
                    info = self.client.tv_info
                    self.model = (info.system or {}).get("modelName") or "LG webOS"
                if self.client is not None and self.client.is_connected():
                    self.state = "device"
                elif key and not self.pairing:
                    self.state = "disconnected"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fails += 1
                self.state = "disconnected"
                if fails in (1, 5) or fails % 30 == 0:
                    log(f"webos {self.host}: {type(e).__name__}: {e}")
                await self._drop()
            await self.publish()
            await asyncio.sleep(3 if self.state == "device" else min(6 + fails, 20))

    # -- control
    def key(self, name):
        if self.q is not None and self.state == "device":
            self.q.put_nowait(("key", name))

    def text(self, s):
        if self.q is not None and self.state == "device" and s:
            self.q.put_nowait(("text", s))

    async def worker(self):
        while True:
            kind, arg = await self.q.get()
            c = self.client
            if c is None:
                continue
            try:
                if kind == "text":
                    await c.request(_wep.INSERT_TEXT, {"text": arg, "replace": 0})
                else:
                    await self._do_key(c, arg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"webos key {arg!r}: {type(e).__name__}: {e}")

    async def _do_key(self, c, name):
        if name == "VOLUME_UP":
            await c.volume_up()
        elif name == "VOLUME_DOWN":
            await c.volume_down()
        elif name == "VOLUME_MUTE":
            await c.set_mute(not bool(c.tv_state.muted))
        elif name in ("MEDIA_PLAY_PAUSE", "MEDIA_PLAY", "MEDIA_PAUSE"):
            self.playing = (not self.playing) if name == "MEDIA_PLAY_PAUSE" else name == "MEDIA_PLAY"
            await (c.play() if self.playing else c.pause())
        elif name == "MEDIA_REWIND":
            await c.rewind()
        elif name == "MEDIA_FAST_FORWARD":
            await c.fast_forward()
        elif name == "MEDIA_STOP":
            await c.stop()
        elif name == "DEL":
            await c.request(_wep.SEND_DELETE, {"count": 1})
        elif name == "POWER":
            await c.power_off()
        elif name in WEBOS_BUTTONS:
            await c.button(WEBOS_BUTTONS[name])
        elif re.match(r"^[0-9]$", name[-1:]) and name.startswith("NUMPAD_"):
            await c.button(name[-1])

    async def power(self, on):
        c = self.client
        if on:
            mac = self.cfg.get("mac")
            if mac:
                await asyncio.get_running_loop().run_in_executor(None, wake_on_lan, mac)
        elif c is not None and self.state == "device":
            await c.power_off()

    async def set_volume(self, level):
        if self.client is not None and self.state == "device":
            await self.client.set_volume(int(level))

    async def apps(self):
        c = self.client
        if self.state != "device" or c is None:
            return []
        res = []
        for aid, a in (c.tv_state.apps or {}).items():
            title = a.get("title") or aid
            res.append({"pkg": aid, "name": title, "color": WEBOS_COLORS.get(aid, "#3a3f4b"),
                        "known": aid in WEBOS_COLORS})
        res.sort(key=lambda x: (not x["known"], x["name"].lower()))
        return res

    async def launch(self, pkg):
        c = self.client
        if c is None or self.state != "device" or not PKG_RE.match(pkg):
            return
        try:
            await c.launch_app(pkg)
        except Exception as e:
            log(f"webos launch {pkg}: {e}")

    async def pair(self, pin=None):
        if self.pairing:
            return False, "Ya hay un emparejamiento en curso: acepta el aviso en la TV."
        self.pairing = True
        self.pair_msg = "Acepta el aviso que aparece en la TV (tienes 30 s)…"
        await self.publish(force=True)
        try:
            await self._drop()
            c = _webos.WebOsClient(self.host, client_key=None)
            ok = await asyncio.wait_for(c.connect(), 45)
            if not ok or not c.client_key:
                raise ConnectionError("not accepted")
            self._save_key(c.client_key)
            await c.register_state_update_callback(self._on_state)
            self.client = c
            self.model = (c.tv_info.system or {}).get("modelName") or "LG webOS"
            self.state = "device"
            self.pair_msg = ""
            await self.publish(force=True)
            return True, "Emparejada."
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"webos pair {self.host}: {type(e).__name__}: {e}")
            self.pair_msg = ""
            await self.publish(force=True)
            return False, ("No se aceptó el aviso a tiempo o no llego a la TV. Enciéndela, comprueba "
                           "que «LG Connect Apps» está activo en su red y vuelve a intentarlo.")
        finally:
            self.pairing = False


DRIVERS = {"android": AndroidDriver, "vidaa": VidaaDriver, "webos": WebosDriver}


# --------------------------------------------------------------------------- registry

def new_id():
    return uuid.uuid4().hex[:8]


def clean_cfg(d, old=None):
    """Validate user input into a TV config dict. Raises ValueError with a message for the UI."""
    old = old or {}
    typ = str(d.get("type", old.get("type", ""))).strip()
    if typ not in DRIVERS:
        raise ValueError("Tipo de TV no válido")
    if typ == "vidaa" and _vidaa is None:
        raise ValueError("Falta la librería vidaa-control en el servidor")
    if typ == "webos" and _webos is None:
        raise ValueError("Falta la librería aiowebostv en el servidor")
    host = str(d.get("host", old.get("host", ""))).strip()
    if not HOST_RE.match(host):
        raise ValueError("IP o nombre de host no válido")
    try:
        port = int(d.get("port") or old.get("port") or DEFAULT_PORTS[typ])
    except (TypeError, ValueError):
        raise ValueError("Puerto no válido")
    if not 1 <= port <= 65535:
        raise ValueError("Puerto no válido")
    mac = str(d.get("mac", old.get("mac", "")) or "").strip()
    if mac and not MAC_RE.match(mac):
        raise ValueError("MAC no válida (formato AA:BB:CC:DD:EE:FF)")
    name = str(d.get("name", old.get("name", "")) or "").strip()[:40] or host
    via = str(d.get("volume_via", old.get("volume_via", "")) or "")
    return {"id": old.get("id") or new_id(), "name": name, "type": typ, "host": host, "port": port,
            "mac": mac.upper().replace("-", ":") if mac else "",
            "tls": bool(d.get("tls", old.get("tls", True))), "volume_via": via}


async def load_registry():
    cfgs = []
    try:
        with open(TVS_PATH) as f:
            cfgs = json.load(f)
    except (OSError, ValueError):
        pass
    if not cfgs:                                   # first start: migrate v0.1 config or TV_HOST
        host = os.environ.get("TV_HOST", "").strip()
        try:
            with open(OLD_CFG_PATH) as f:
                host = json.load(f).get("host", host)
        except (OSError, ValueError):
            pass
        if host:
            cfgs = [{"id": new_id(), "name": "TV", "type": "android", "host": host,
                     "port": int(os.environ.get("TV_PORT", "5555")), "mac": "", "tls": True, "volume_via": ""}]
    for c in cfgs:
        if c.get("type") in DRIVERS and (c["type"] != "vidaa" or _vidaa) and (c["type"] != "webos" or _webos):
            hub.tvs[c["id"]] = DRIVERS[c["type"]](c)
    hub.save()
    for d in hub.tvs.values():
        await d.start()


def get_tv(request):
    tv = hub.tvs.get(request.match_info.get("id", "") or request.query.get("tv", ""))
    if not tv:
        raise web.HTTPNotFound(text="unknown TV")
    return tv


def route(tv, key):
    """Volume keys can be sent to another TV (e.g. a Fire Stick whose volume lives on the Hisense)."""
    via = tv.cfg.get("volume_via")
    if key in VOLUME_KEYS and via and via != tv.id and via in hub.tvs:
        return hub.tvs[via]
    return tv


# --------------------------------------------------------------------------- HTTP

def authorized(request):
    if not TOKEN:
        return True
    return request.query.get("token") == TOKEN or request.cookies.get("tvhub") == TOKEN


@web.middleware
async def auth_mw(request, handler):
    if not authorized(request):
        return web.Response(status=401, text="Missing or wrong token. Open /?token=... once.")
    resp = await handler(request)
    if TOKEN and request.query.get("token") == TOKEN and not isinstance(resp, web.WebSocketResponse):
        resp.set_cookie("tvhub", TOKEN, httponly=True, samesite="Strict")
    return resp


async def index(request):
    return web.FileResponse(os.path.join(STATIC, "index.html"))


async def broadcast_list():
    await hub.broadcast({"t": "list", "tvs": [x.status() for x in hub.tvs.values()]})
    if ha:
        await ha.sync()


_scan_lock = asyncio.Lock()


async def api_discover_suggest(request):
    import discover
    host = request.host.rsplit(":", 1)[0].strip("[]") if request.host else ""
    return web.json_response({"suggest": discover.suggest(host)})


async def api_discover(request):
    """Look for TVs on the given networks (see discover.py). One scan at a time."""
    import discover
    try:
        d = await request.json() if request.can_read_body else {}
        cidrs = d.get("cidrs") or []
        if isinstance(cidrs, str):
            cidrs = re.split(r"[\s,;]+", cidrs)
        if not isinstance(cidrs, list):
            raise ValueError("Redes no válidas")
        if _scan_lock.locked():
            return web.json_response({"error": "Ya hay una búsqueda en curso"}, status=409)
        async with _scan_lock:
            res = await discover.scan(cidrs[:8], {tv.cfg.get("host") for tv in hub.tvs.values()})
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(res)


async def api_ha(request):
    return web.json_response(ha.status())


async def api_ha_put(request):
    try:
        cfg = ha.clean(await request.json(), old_password=ha.password)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    ha.configure(cfg)
    return web.json_response(ha.status())


async def api_list(request):
    return web.json_response({"tvs": [d.status() for d in hub.tvs.values()],
                              "vidaa": _vidaa is not None, "webos": _webos is not None})


async def api_add(request):
    try:
        cfg = clean_cfg(await request.json())
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    d = DRIVERS[cfg["type"]](cfg)
    hub.tvs[cfg["id"]] = d
    hub.save()
    await d.start()
    await broadcast_list()
    return web.json_response(d.status())


async def api_edit(request):
    tv = get_tv(request)
    try:
        cfg = clean_cfg(await request.json(), old=tv.cfg)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    if cfg["type"] != tv.type:
        return web.json_response({"error": "No se puede cambiar el tipo; borra y vuelve a añadir"}, status=400)
    changed = any(cfg[k] != tv.cfg.get(k) for k in ("host", "port", "mac", "tls"))
    if changed:
        await tv.stop()
        tv.cfg.update(cfg)
        tv.state, tv.model = "disconnected", ""
        await tv.start()
    else:
        tv.cfg.update(cfg)
    hub.save()
    await tv.publish(force=True)
    await broadcast_list()
    return web.json_response(tv.status())


async def api_delete(request):
    tv = get_tv(request)
    await tv.stop()
    hub.tvs.pop(tv.id, None)
    for other in hub.tvs.values():            # nobody may keep routing volume to a TV that is gone
        if other.cfg.get("volume_via") == tv.id:
            other.cfg["volume_via"] = ""
    hub.save()
    await broadcast_list()
    return web.json_response({"ok": True})


async def api_pair(request):
    tv = get_tv(request)
    if not tv.caps.get("pair"):
        return web.json_response({"error": "Esta TV no necesita emparejamiento"}, status=400)
    d = await request.json() if request.can_read_body else {}
    pin = re.sub(r"\D", "", str(d.get("pin", "")))[:8] or None
    ok, msg = await tv.pair(pin)
    return web.json_response({"ok": ok, "message": msg})


async def api_apps(request):
    return web.json_response(await get_tv(request).apps())


async def api_launch(request):
    d = await request.json()
    await get_tv(request).launch(str(d.get("pkg", "")))
    return web.json_response({"ok": True})


def _body_keys(d):
    keys = d.get("keys") or [d.get("key")]
    return [k for k in keys[:10] if isinstance(k, str) and KEY_RE.match(k)]


async def api_key(request):
    tv = get_tv(request)
    for k in _body_keys(await request.json()):
        route(tv, k).key(k)
    return web.json_response({"ok": True})


async def api_text(request):
    tv = get_tv(request)
    if not tv.caps.get("text"):
        return web.json_response({"error": "Esta TV no admite texto"}, status=400)
    tv.text(str((await request.json()).get("text", ""))[:255])
    return web.json_response({"ok": True})


async def api_volume(request):
    tv = get_tv(request)
    target = hub.tvs.get(tv.cfg.get("volume_via") or "") or tv
    if not target.caps.get("volume_set"):
        return web.json_response({"error": "Esta TV no permite fijar el volumen"}, status=400)
    try:
        level = max(0, min(100, int((await request.json()).get("level"))))
    except (TypeError, ValueError):
        return web.json_response({"error": "Nivel no válido"}, status=400)
    await target.set_volume(level)
    return web.json_response({"ok": True, "level": level})


async def api_power(request):
    tv = get_tv(request)
    await tv.power(bool((await request.json()).get("on")))
    return web.json_response({"ok": True})


async def api_snapshot(request):
    tv = get_tv(request)
    if not tv.caps.get("snapshot"):
        return web.Response(status=400, text="This TV has no screen capture")
    out = await tv.snapshot()
    if not out:
        return web.Response(status=503, text="TV not connected")
    headers = {"Cache-Control": "no-store"}
    if request.query.get("download"):
        headers["Content-Disposition"] = f'attachment; filename="tv-{int(time.time())}.png"'
    return web.Response(body=out, content_type="image/png", headers=headers)


async def ws_video(request):
    tv = get_tv(request)
    if not tv.caps.get("video"):
        return web.Response(status=400, text="This TV has no video")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
    await ws.prepare(request)
    q, st = request.query, tv.streamer
    try:
        if q.get("size") and re.match(r"^\d{3,4}x\d{3,4}$", q["size"]):
            st.size = q["size"]
        if q.get("bitrate"):
            st.bitrate = max(500_000, min(16_000_000, int(q["bitrate"])))
    except ValueError:
        pass
    await st.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        await st.remove(ws)
    return ws


async def ws_control(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    hub.clients.add(ws)
    await ws.send_json({"t": "list", "tvs": [d.status() for d in hub.tvs.values()]})
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
                tv = hub.tvs.get(d.get("tv"))
                if not tv:
                    continue
                t = d.get("t")
                if t == "key":
                    keys = d.get("keys") or [d.get("k")]
                    for k in keys[:10]:
                        if isinstance(k, str) and KEY_RE.match(k):
                            route(tv, k).key(k)
                elif t == "text" and tv.caps.get("text"):
                    tv.text(str(d.get("v", "")))
                elif t == "app" and tv.caps.get("apps"):
                    asyncio.create_task(tv.launch(str(d.get("pkg", ""))))
            except Exception as e:
                log("control:", e)
    finally:
        hub.clients.discard(ws)
    return ws


async def on_start(app):
    global ha
    await load_registry()
    from ha import HABridge
    ha = HABridge(hub, log, DATA, route, KEY_RE)
    ha.start(asyncio.get_running_loop())


async def on_stop(app):
    if ha:
        ha.stop()
    for d in list(hub.tvs.values()):
        await d.stop()


def make_app():
    app = web.Application(middlewares=[auth_mw])
    app.add_routes([
        web.get("/", index),
        web.get("/api/tvs", api_list),
        web.get("/api/discover", api_discover_suggest),
        web.post("/api/discover", api_discover),
        web.get("/api/ha", api_ha),
        web.put("/api/ha", api_ha_put),
        web.post("/api/tvs", api_add),
        web.patch("/api/tvs/{id}", api_edit),
        web.delete("/api/tvs/{id}", api_delete),
        web.post("/api/tvs/{id}/pair", api_pair),
        web.get("/api/tvs/{id}/apps", api_apps),
        web.post("/api/tvs/{id}/launch", api_launch),
        web.get("/api/tvs/{id}/snapshot", api_snapshot),
        web.post("/api/tvs/{id}/key", api_key),
        web.post("/api/tvs/{id}/text", api_text),
        web.post("/api/tvs/{id}/volume", api_volume),
        web.post("/api/tvs/{id}/power", api_power),
        web.get("/ws/video", ws_video),
        web.get("/ws/control", ws_control),
    ])
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log(f"TVHub on :{port}  vidaa={'yes' if _vidaa else 'no'}  token={'on' if TOKEN else 'off'}")
    web.run_app(make_app(), host="0.0.0.0", port=port, print=None)
