#!/usr/bin/env python3
"""TVHub server: one web remote for several TVs.

Drivers
  android  Fire TV / Google TV / Android TV over ADB: remote keys, text, apps, snapshots and live
           H.264 video (`screenrecord`).
  vidaa    Hisense VIDAA over its built-in MQTT broker (via the `vidaa-control` library): remote
           keys, volume, PIN pairing. VIDAA exposes no screen, so no video.

HTTP / WebSocket
  /api/tvs ...        list, add, edit, delete, pair, apps, launch, snapshot
  /api/power-all      turn every TV on or off
  /api/discover       find TVs on the network (GET: suggested networks, POST: scan)
  /ws/control         commands in (each carries the target TV id), status of every TV out
  /ws/video?tv=<id>   live video of an android TV, one WebSocket message per frame
"""
import asyncio
import hmac
import json
import os
import re
import secrets
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import web, WSMsgType

import icons
import security as sec

DATA = os.environ.get("DATA_DIR", "/data")
ADB = os.environ.get("ADB_BIN", "adb")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TVS_PATH = os.path.join(DATA, "tvs.json")
OLD_CFG_PATH = os.path.join(DATA, "config.json")      # v0.1 single-TV config, migrated on first start
TOKEN = os.environ.get("TVHUB_TOKEN") or os.environ.get("TVDECK_TOKEN", "")      # TVDECK_TOKEN: name used before the rename

KEY_RE = re.compile(r"^[A-Z0-9_]{2,40}$")
PKG_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,100}$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
VOLUME_KEYS = {"VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE"}
DEFAULT_PORTS = {"android": 5555, "vidaa": 36669, "webos": 3001, "roku": 8060, "samsung": 8002, "sony": 80}

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
                "caps": {**self.caps, "inputs": self.has_inputs()}, "volume_via": self.cfg.get("volume_via") or "",
                "hdmi": int(self.cfg.get("hdmi") or 0), "tv": bool(self.cfg.get("tv")),
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

    # video inputs (HDMI…): drivers that can switch them override these
    def has_inputs(self):
        return False

    async def inputs(self):
        return []

    async def set_input(self, iid):
        raise NotImplementedError

    def hdmi_id(self, n):
        """Input id of this TV's HDMI port n (1-4), used by the "show this Stick on the TV" button."""
        return ""

    async def app_icon(self, pkg):
        """(bytes, content_type) of the icon the TV itself has for an app, if it offers one."""
        return None


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
        """Start an app. `monkey` is flaky on Fire OS, so resolve the app's launcher activity and start it
        explicitly (TV launcher first, then the phone one); monkey stays as the last resort."""
        if not PKG_RE.match(pkg):
            return
        last = ""
        for cat in ("LEANBACK_LAUNCHER", "LAUNCHER"):
            _, out = await self.sh(f"cmd package resolve-activity --brief -c android.intent.category.{cat} {pkg}")
            comp = next((l.strip() for l in reversed(out.splitlines()) if re.match(r"^[\w.]+/[\w.$]+$", l.strip())), "")
            if comp and comp.split("/")[0] == pkg:
                _, o = await self.sh(f"am start -a android.intent.action.MAIN -c android.intent.category.{cat} -n {comp}")
                last = o.strip()
                if "Error" not in o and "Exception" not in o:
                    return
        for cat in ("LEANBACK_LAUNCHER", "LAUNCHER"):
            _, o = await self.sh(f"monkey -p {pkg} -c android.intent.category.{cat} 1")
            last = o.strip() or last
            if "No activities found" not in o and "monkey aborted" not in o:
                return
        log(f"android {self.host}: could not launch {pkg}: {last[-200:]}")

    async def power(self, on):
        self.key("WAKEUP" if on else "SLEEP")

    # An Android *TV set* (Sony, Philips…) has HDMI inputs reachable with key events; a Fire / Google TV stick has none.
    def has_inputs(self):
        return bool(self.cfg.get("tv"))

    async def inputs(self):
        return [{"id": "TV_INPUT", "name": "Entradas"}] + [{"id": f"TV_INPUT_HDMI_{n}", "name": f"HDMI {n}"} for n in range(1, 5)]

    async def set_input(self, iid):
        if not KEY_RE.match(iid) or not iid.startswith("TV_INPUT"):
            raise ValueError("Entrada no válida")
        self.key(iid)

    def hdmi_id(self, n):
        return f"TV_INPUT_HDMI_{int(n)}"

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

    def has_inputs(self):
        return True

    async def inputs(self):
        return [{"id": "tv", "name": "TV"}] + [{"id": f"hdmi{n}", "name": f"HDMI {n}"} for n in range(1, 5)] + [{"id": "av", "name": "AV"}]

    async def set_input(self, iid):
        if self.client is None or self.state != "device":
            raise RuntimeError("La TV no está conectada")
        if iid not in {"tv", "av", "hdmi1", "hdmi2", "hdmi3", "hdmi4"}:
            raise ValueError("Entrada no válida")
        ok = await asyncio.get_running_loop().run_in_executor(self.pool, self.client.set_source, iid)
        if not ok:
            raise RuntimeError("La TV no aceptó el cambio de entrada")

    def hdmi_id(self, n):
        return f"hdmi{int(n)}"

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
            if not self.client.send_key(k):
                log(f"vidaa {self.host}: key {k} not sent (MQTT not connected or publish failed)")
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

    def has_inputs(self):
        return True

    async def inputs(self):
        c = self.client
        if c is None or self.state != "device":
            return []
        res = []
        for x in (c.tv_state.inputs or {}).values():
            if x.get("id"):
                res.append({"id": str(x["id"]), "name": str(x.get("label") or x["id"]), "connected": bool(x.get("connected", True))})
        return res

    async def set_input(self, iid):
        c = self.client
        if c is None or self.state != "device":
            raise RuntimeError("La TV no está conectada")
        if not re.match(r"^[A-Za-z0-9_\-]{1,40}$", iid):
            raise ValueError("Entrada no válida")
        await c.set_input(iid)

    def hdmi_id(self, n):
        return f"HDMI_{int(n)}"

    async def app_icon(self, pkg):
        c = self.client
        a = ((c.tv_state.apps or {}).get(pkg) or {}) if c else {}
        url = a.get("largeIcon") or a.get("icon")
        return await icons.fetch_remote(url, self.host) if url else None

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


# --------------------------------------------------------------------------- Roku / Samsung / Sony (plain HTTP / WebSocket, no extra libraries)

class AuthNeeded(Exception):
    """The TV wants a credential (pairing token / pre-shared key) that we do not have yet."""


def _secret_path(name):
    return Path(DATA) / f"{name}.json"


def secret_get(name, tid):
    return (sec._read_json(_secret_path(name), {}) or {}).get(tid)


def secret_set(name, tid, value):
    d = sec._read_json(_secret_path(name), {}) or {}
    if value is None:
        d.pop(tid, None)
    else:
        d[tid] = value
    sec._write_json(_secret_path(name), d)


def _unesc(s):
    import html
    return html.unescape(s or "").strip()


class NetDriver(Driver):
    """Base for TVs controlled over HTTP: one poll loop (state, reconnection) and one ordered command queue."""
    caps = {"video": False, "apps": True, "text": False, "snapshot": False, "pair": False}

    def __init__(self, cfg):
        super().__init__(cfg)
        self.q = None
        self.http = None
        self.on = None                 # True / False when the TV reports its power state, None when unknown
        self.app_name = ""
        self.app_focus = ""
        self.volume = None
        self.muted = False
        self.pair_msg = ""
        self._apps = []

    def extra(self):
        return {"on": self.on, "app": self.app_name, "focus": self.app_focus, "volume": self.volume,
                "muted": self.muted, "pair_msg": self.pair_msg}

    async def start(self):
        import aiohttp
        self.q = asyncio.Queue()
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6),
                                          connector=aiohttp.TCPConnector(ssl=False))
        self.tasks = [asyncio.create_task(self.run()), asyncio.create_task(self.worker())]

    async def stop(self):
        await super().stop()
        h, self.http = self.http, None
        if h:
            await h.close()

    async def poll(self):
        raise NotImplementedError

    async def run(self):
        fails = 0
        while True:
            try:
                await self.poll()
                self.state = "device"
                fails = 0
            except asyncio.CancelledError:
                raise
            except AuthNeeded:
                self.state, self.on = "needs_pairing", None
            except Exception as e:                          # noqa: BLE001 — any network error means "not reachable now"
                fails += 1
                self.state, self.on, self.app_name, self.app_focus = "disconnected", None, "", ""
                if fails in (1, 5) or fails % 30 == 0:
                    log(f"{self.type} {self.host}: {type(e).__name__}: {e}")
            await self.publish()
            await asyncio.sleep(3 if self.state == "device" else min(6 + fails, 20) if self.state == "disconnected" else 5)

    def key(self, name):
        if self.q is not None and self.state == "device":
            self.q.put_nowait(("key", name))

    def text(self, s):
        if self.q is not None and self.state == "device" and s:
            self.q.put_nowait(("text", s))

    async def worker(self):
        while True:
            kind, arg = await self.q.get()
            try:
                await (self.do_text(arg) if kind == "text" else self.do_key(arg))
            except asyncio.CancelledError:
                raise
            except Exception as e:                          # noqa: BLE001
                log(f"{self.type} {kind} {arg!r}: {type(e).__name__}: {e}")

    async def do_key(self, name):
        raise NotImplementedError

    async def do_text(self, s):
        pass

    def _num_key(self, name):
        return name[-1] if name.startswith("NUMPAD_") and name[-1:].isdigit() else None

    async def power(self, on):
        mac = self.cfg.get("mac")
        if on and mac:
            await asyncio.get_running_loop().run_in_executor(None, wake_on_lan, mac)
        if on and self.state == "device":
            await self.power_on_net()
        elif not on and self.state == "device":
            await self.power_off_net()

    async def power_on_net(self):
        pass

    async def power_off_net(self):
        pass


# ---- Roku: External Control Protocol (HTTP :8060, no pairing)

ROKU_KEYS = {"DPAD_UP": "Up", "DPAD_DOWN": "Down", "DPAD_LEFT": "Left", "DPAD_RIGHT": "Right", "DPAD_CENTER": "Select",
             "ENTER": "Select", "BACK": "Back", "HOME": "Home", "MENU": "Info", "MEDIA_PLAY_PAUSE": "Play",
             "MEDIA_PLAY": "Play", "MEDIA_PAUSE": "Play", "MEDIA_REWIND": "Rev", "MEDIA_FAST_FORWARD": "Fwd",
             "VOLUME_UP": "VolumeUp", "VOLUME_DOWN": "VolumeDown", "VOLUME_MUTE": "VolumeMute",
             "CHANNEL_UP": "ChannelUp", "CHANNEL_DOWN": "ChannelDown", "DEL": "Backspace", "POWER": "Power"}
ROKU_COLORS = {"12": "#e50914", "837": "#ff0033", "13": "#00a8e1", "291097": "#113ccf", "61322": "#5b2bd4",
               "22297": "#1db954", "551012": "#1d1d1f", "13535": "#e5a00d"}


class RokuDriver(NetDriver):
    type = "roku"
    caps = {"video": False, "apps": True, "text": True, "snapshot": False, "pair": False}

    def __init__(self, cfg):
        super().__init__(cfg)
        self.is_tv = False
        self._inputs = []

    @property
    def base(self):
        return f"http://{self.host}:{self.port}"

    async def _get(self, path):
        async with self.http.get(self.base + path) as r:
            if r.status == 403:
                raise ConnectionError("403: en la Roku activa Ajustes → Sistema → Configuración avanzada → Control por aplicaciones móviles → Permisivo")
            if r.status != 200:
                raise ConnectionError(f"HTTP {r.status}")
            return await r.read()

    async def _post(self, path):
        async with self.http.post(self.base + path) as r:
            if r.status >= 400:
                raise ConnectionError(f"HTTP {r.status}")

    async def poll(self):
        info = (await self._get("/query/device-info")).decode("utf-8", "replace")
        tag = lambda t: _unesc((re.search(rf"<{t}>([^<]{{0,80}})</{t}>", info) or [None, ""])[1])
        self.model = tag("model-name") or "Roku"
        self.is_tv = tag("is-tv") == "true"
        mode = tag("power-mode")
        self.on = (mode == "PowerOn") if mode else None
        if not self._apps and self.on is not False:
            await self._load_apps()
        self.app_name = self.app_focus = ""
        if self.on is not False:
            act = (await self._get("/query/active-app")).decode("utf-8", "replace")
            m = re.search(r'<app\b([^>]*)>([^<]*)</app>', act)
            if m:
                idm = re.search(r'id="([^"]+)"', m.group(1))
                self.app_name, self.app_focus = _unesc(m.group(2)), idm.group(1) if idm else ""
                nm = next((i["name"] for i in self._inputs if i["pkg"] == self.app_focus), "")
                if nm:                                      # an HDMI input in front: show "HDMI 2", not "tvinput.hdmi2"
                    self.app_name = nm

    async def _load_apps(self):
        xml = (await self._get("/query/apps")).decode("utf-8", "replace")
        apps, ins = [], []
        for attrs, name in re.findall(r"<app\b([^>]*)>([^<]*)</app>", xml):
            i = re.search(r'id="([^"]+)"', attrs)
            t = re.search(r'type="([^"]+)"', attrs)
            if not i or not re.match(r"^[A-Za-z0-9_.\-]{1,120}$", i.group(1)):
                continue
            rec = {"pkg": i.group(1), "name": _unesc(name) or i.group(1)}
            (ins if (t and t.group(1) == "tvin") or i.group(1).startswith("tvinput.") else apps).append(rec)
        self._apps, self._inputs = apps, ins

    async def do_key(self, name):
        k = ROKU_KEYS.get(name) or (("Lit_" + self._num_key(name)) if self._num_key(name) else None)
        if k:
            await self._post(f"/keypress/{k}")

    async def do_text(self, s):
        from urllib.parse import quote
        for ch in s[:200]:
            await self._post("/keypress/Lit_" + quote(ch, safe=""))

    async def power_on_net(self):
        await self._post("/keypress/PowerOn")

    async def power_off_net(self):
        await self._post("/keypress/PowerOff")

    async def apps(self):
        if self.state != "device":
            return []
        res = [{"pkg": a["pkg"], "name": a["name"], "color": ROKU_COLORS.get(a["pkg"], "#3a3f4b"), "known": a["pkg"] in ROKU_COLORS}
               for a in self._apps]
        res.sort(key=lambda x: (not x["known"], x["name"].lower()))
        return res

    async def launch(self, pkg):
        if self.state == "device" and re.match(r"^[A-Za-z0-9_.\-]{1,120}$", pkg):
            try:
                await self._post(f"/launch/{pkg}")
            except Exception as e:                          # noqa: BLE001
                log(f"roku launch {pkg}: {e}")

    def has_inputs(self):
        return bool(self._inputs)

    async def inputs(self):
        return [{"id": i["pkg"], "name": i["name"], "connected": True} for i in self._inputs] if self.state == "device" else []

    async def set_input(self, iid):
        if self.state != "device":
            raise RuntimeError("La TV no está conectada")
        if iid not in {i["pkg"] for i in self._inputs}:
            raise ValueError("Entrada no válida")
        await self._post(f"/launch/{iid}")

    def hdmi_id(self, n):
        return f"tvinput.hdmi{int(n)}"

    async def app_icon(self, pkg):
        if self.state != "device" or not re.match(r"^[A-Za-z0-9_.\-]{1,120}$", pkg) or pkg.startswith("tvinput."):
            return None
        try:
            async with self.http.get(f"{self.base}/query/icon/{pkg}") as r:
                ct = r.headers.get("Content-Type", "")
                if r.status == 200 and ct.startswith("image/"):
                    return await r.read(), ct.split(";")[0]
        except Exception:                                   # noqa: BLE001
            pass
        return None


# ---- Samsung Tizen: WebSocket remote channel (wss :8002, one-time token approved on the TV)

SAMSUNG_KEYS = {"DPAD_UP": "KEY_UP", "DPAD_DOWN": "KEY_DOWN", "DPAD_LEFT": "KEY_LEFT", "DPAD_RIGHT": "KEY_RIGHT",
                "DPAD_CENTER": "KEY_ENTER", "ENTER": "KEY_ENTER", "BACK": "KEY_RETURN", "HOME": "KEY_HOME", "MENU": "KEY_MENU",
                "VOLUME_UP": "KEY_VOLUP", "VOLUME_DOWN": "KEY_VOLDOWN", "VOLUME_MUTE": "KEY_MUTE",
                "CHANNEL_UP": "KEY_CHUP", "CHANNEL_DOWN": "KEY_CHDOWN", "MEDIA_PLAY": "KEY_PLAY", "MEDIA_PAUSE": "KEY_PAUSE",
                "MEDIA_STOP": "KEY_STOP", "MEDIA_REWIND": "KEY_REWIND", "MEDIA_FAST_FORWARD": "KEY_FF", "POWER": "KEY_POWER"}
SAMSUNG_APPS = [("11101200001", "Netflix", "#e50914"), ("111299001912", "YouTube", "#ff0033"),
                ("3201512006785", "Prime Video", "#00a8e1"), ("3201901017640", "Disney+", "#113ccf"),
                ("3201606009684", "Spotify", "#1db954")]


class SamsungDriver(NetDriver):
    type = "samsung"
    caps = {"video": False, "apps": True, "text": False, "snapshot": False, "pair": True}

    def __init__(self, cfg):
        super().__init__(cfg)
        self.ws = None
        self.reader = None
        self.ready = None
        self.playing = False
        self._installed = None

    async def stop(self):
        await self._close_ws()
        await super().stop()

    async def _close_ws(self):
        r, self.reader = self.reader, None
        w, self.ws = self.ws, None
        if r:
            r.cancel()
        if w is not None and not w.closed:
            try:
                await w.close()
            except Exception:                               # noqa: BLE001
                pass

    async def poll(self):
        async with self.http.get(f"http://{self.host}:8001/api/v2/") as r:
            if r.status != 200:
                raise ConnectionError(f"HTTP {r.status}")
            j = await r.json(content_type=None)
        dev = (j or {}).get("device") or {}
        self.model = str(dev.get("modelName") or "Samsung")[:40]
        ps = str(dev.get("PowerState") or "").lower()
        self.on = (ps == "on") if ps else None
        if not secret_get("samsung-tokens", self.id):
            raise AuthNeeded()
        if self.ws is not None and self.ws.closed:
            await self._close_ws()

    def _url(self, token):
        import base64
        from urllib.parse import quote
        name = base64.b64encode(b"TVHub").decode()
        return f"wss://{self.host}:8002/api/v2/channels/samsung.remote.control?name={quote(name)}" + (f"&token={quote(token)}" if token else "")

    async def _read(self, ws, ready, token_box):
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(m.data)
            except ValueError:
                continue
            ev = d.get("event")
            if ev == "ms.channel.connect":
                tok = (d.get("data") or {}).get("token")
                if tok:
                    token_box.append(str(tok))
                if not ready.done():
                    ready.set_result(True)
            elif ev == "ed.installedApp.get":
                lst = ((d.get("data") or {}).get("data")) or []
                self._installed = [{"pkg": str(a.get("appId")), "name": str(a.get("name") or a.get("appId"))} for a in lst if a.get("appId")]
            elif ev in ("ms.channel.timeOut", "ms.error") and not ready.done():
                ready.set_exception(ConnectionError(str(ev)))

    async def _ensure_ws(self, timeout=8):
        if self.ws is not None and not self.ws.closed:
            return self.ws
        await self._close_ws()
        token = secret_get("samsung-tokens", self.id)
        if not token:
            raise AuthNeeded()
        ready = asyncio.get_running_loop().create_future()
        box = []
        ws = await self.http.ws_connect(self._url(token), heartbeat=20)
        self.ws = ws
        self.reader = asyncio.create_task(self._read(ws, ready, box))
        await asyncio.wait_for(ready, timeout)
        if box and box[0] != token:
            secret_set("samsung-tokens", self.id, box[0])
        return ws

    async def _send(self, payload):
        ws = await self._ensure_ws()
        await ws.send_json(payload)

    async def do_key(self, name):
        if name == "MEDIA_PLAY_PAUSE":
            self.playing = not self.playing
            k = "KEY_PLAY" if self.playing else "KEY_PAUSE"
        else:
            k = SAMSUNG_KEYS.get(name) or (("KEY_" + self._num_key(name)) if self._num_key(name) else None)
        if k:
            await self._send({"method": "ms.remote.control", "params": {"Cmd": "Click", "DataOfCmd": k, "Option": "false", "TypeOfRemote": "SendRemoteKey"}})

    async def power_on_net(self):
        await self.do_key("POWER")

    async def power_off_net(self):
        await self.do_key("POWER")

    async def power(self, on):
        mac = self.cfg.get("mac")
        if on and mac:
            await asyncio.get_running_loop().run_in_executor(None, wake_on_lan, mac)
        if on and self.state == "device" and self.on is False and not mac:
            await self.do_key("POWER")
        elif not on and self.state == "device" and self.on is not False:
            await self.do_key("POWER")

    async def apps(self):
        if self.state != "device":
            return []
        try:
            await self._send({"method": "ms.channel.emit", "params": {"event": "ed.installedApp.get", "to": "host"}})
            for _ in range(10):
                if self._installed is not None:
                    break
                await asyncio.sleep(0.2)
        except Exception as e:                              # noqa: BLE001
            log(f"samsung apps {self.host}: {type(e).__name__}: {e}")
        colors = {a[0]: a[2] for a in SAMSUNG_APPS}
        src = self._installed or [{"pkg": a[0], "name": a[1]} for a in SAMSUNG_APPS]
        res = [{"pkg": a["pkg"], "name": a["name"], "color": colors.get(a["pkg"], "#3a3f4b"), "known": a["pkg"] in colors} for a in src
               if PKG_RE.match(a["pkg"])]
        res.sort(key=lambda x: (not x["known"], x["name"].lower()))
        return res

    async def launch(self, pkg):
        if self.state != "device" or not PKG_RE.match(pkg):
            return
        try:
            async with self.http.post(f"http://{self.host}:8001/api/v2/applications/{pkg}") as r:
                if r.status == 200:
                    return
            await self._send({"method": "ms.channel.emit", "params": {"event": "ed.apps.launch", "to": "host",
                              "data": {"action_type": "DEEP_LINK", "appId": pkg}}})
        except Exception as e:                              # noqa: BLE001
            log(f"samsung launch {pkg}: {e}")

    async def pair(self, pin=None):
        if self.pair_msg:
            return False, "Ya hay un emparejamiento en curso: acepta el aviso en la TV."
        self.pair_msg = "Acepta el aviso que aparece en la TV (tienes 30 s)…"
        await self.publish(force=True)
        ready = asyncio.get_running_loop().create_future()
        box = []
        ws = None
        try:
            await self._close_ws()
            ws = await self.http.ws_connect(self._url(""), heartbeat=20)
            t = asyncio.create_task(self._read(ws, ready, box))
            try:
                await asyncio.wait_for(ready, 30)
            finally:
                t.cancel()
            if not box:
                raise ConnectionError("TV did not return a token")
            secret_set("samsung-tokens", self.id, box[0])
            self.state = "device"
            return True, "Emparejada."
        except asyncio.CancelledError:
            raise
        except Exception as e:                              # noqa: BLE001
            log(f"samsung pair {self.host}: {type(e).__name__}: {e}")
            return False, ("No se aceptó el aviso a tiempo o no llegó a la TV. Enciéndela, activa en ella "
                           "Ajustes → General → Administrador de dispositivos externos → «Notificación de acceso» y vuelve a intentarlo.")
        finally:
            if ws is not None and not ws.closed:
                await ws.close()
            self.pair_msg = ""
            await self.publish(force=True)


# ---- Sony Bravia: REST (JSON-RPC) + IRCC remote codes, authenticated with a pre-shared key set on the TV

SONY_KEYS = {"DPAD_UP": "Up", "DPAD_DOWN": "Down", "DPAD_LEFT": "Left", "DPAD_RIGHT": "Right", "DPAD_CENTER": "Confirm",
             "ENTER": "Confirm", "BACK": "Return", "HOME": "Home", "MENU": "Options", "VOLUME_UP": "VolumeUp",
             "VOLUME_DOWN": "VolumeDown", "VOLUME_MUTE": "Mute", "CHANNEL_UP": "ChannelUp", "CHANNEL_DOWN": "ChannelDown",
             "MEDIA_PLAY": "Play", "MEDIA_PAUSE": "Pause", "MEDIA_STOP": "Stop", "MEDIA_REWIND": "Rewind",
             "MEDIA_FAST_FORWARD": "Forward"}


class SonyDriver(NetDriver):
    type = "sony"
    caps = {"video": False, "apps": True, "text": True, "snapshot": False, "pair": True, "volume_set": True}

    def __init__(self, cfg):
        super().__init__(cfg)
        self._codes = {}
        self._app_uri = {}           # short id -> uri (the UI never gets to name an arbitrary uri)
        self._app_name = {}
        self._app_icon_url = {}
        self._inp = []
        self.playing = False

    @property
    def base(self):
        return f"http://{self.host}:{self.port}"

    @staticmethod
    def _lst(res):
        """Sony wraps list results one level deep: {"result": [[{...}, {...}]]}."""
        return res[0] if res and isinstance(res[0], list) else res

    async def _rpc(self, service, method, params=None, version="1.0"):
        psk = secret_get("sony-psk", self.id)
        if not psk:
            raise AuthNeeded()
        body = {"method": method, "id": 1, "params": params if params is not None else [], "version": version}
        async with self.http.post(f"{self.base}/sony/{service}", json=body, headers={"X-Auth-PSK": psk}) as r:
            if r.status in (401, 403):
                raise AuthNeeded()
            if r.status != 200:
                raise ConnectionError(f"HTTP {r.status}")
            j = await r.json(content_type=None)
        if "error" in j:
            raise RuntimeError(f"{method}: {j['error']}")
        return j.get("result") or []

    async def poll(self):
        st = (await self._rpc("system", "getPowerStatus"))
        self.on = bool(st and st[0].get("status") == "active")
        if not self.model:
            info = await self._rpc("system", "getSystemInformation")
            self.model = str((info[0] if info else {}).get("model") or "Sony Bravia")[:40]
        if not self._codes:
            r = await self._rpc("system", "getRemoteControllerInfo")
            lst = r[1] if len(r) > 1 and isinstance(r[1], list) else []
            self._codes = {c["name"]: c["value"] for c in lst if isinstance(c, dict) and "name" in c and "value" in c}
        self.volume, self.muted, self.app_name, self.app_focus = None, False, "", ""
        if not self.on:
            return
        try:
            for v in self._lst(await self._rpc("audio", "getVolumeInformation")):
                if v.get("target") == "speaker":
                    self.volume, self.muted = int(v.get("volume", 0)), bool(v.get("mute"))
        except RuntimeError:
            pass
        if not self._app_uri:
            await self._load_apps()
        try:
            pc = await self._rpc("avContent", "getPlayingContentInfo")
            uri = (pc[0] if pc else {}).get("uri", "")
            self.app_focus = uri
            title = (pc[0] if pc else {}).get("title") or ""
            hit = next((a for a, u in self._app_uri.items() if u and uri and u.split("?")[0] == uri.split("?")[0]), "")
            self.app_name = self._app_name.get(hit, "") if hit else str(title)[:40]
        except RuntimeError:                                # error 7 = "illegal state": an app is in front, not a source
            pass

    async def _load_apps(self):
        import hashlib
        res = await self._rpc("appControl", "getApplicationList")
        for a in (res[0] if res and isinstance(res[0], list) else []):
            uri = a.get("uri") or ""
            if not uri:
                continue
            k = "app" + hashlib.sha1(uri.encode()).hexdigest()[:12]
            self._app_uri[k] = uri
            self._app_name[k] = str(a.get("title") or k)[:40]
            self._app_icon_url[k] = a.get("icon") or ""

    async def _ircc(self, name):
        code = self._codes.get(name)
        if not code:
            return
        body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                '<u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1"><IRCCCode>' + code +
                '</IRCCCode></u:X_SendIRCC></s:Body></s:Envelope>')
        h = {"X-Auth-PSK": secret_get("sony-psk", self.id) or "", "Content-Type": "text/xml; charset=UTF-8",
             "SOAPACTION": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"'}
        async with self.http.post(f"{self.base}/sony/IRCC", data=body.encode(), headers=h) as r:
            if r.status >= 400:
                raise ConnectionError(f"IRCC HTTP {r.status}")

    async def do_key(self, name):
        if name == "MEDIA_PLAY_PAUSE":
            self.playing = not self.playing
            n = "Play" if self.playing else "Pause"
        elif name == "DEL":
            n = "Return"
        elif name == "POWER":
            n = "PowerOff" if self.on else "WakeUp"
        else:
            n = SONY_KEYS.get(name) or (("Num" + self._num_key(name)) if self._num_key(name) else None)
        if n:
            await self._ircc(n)

    async def do_text(self, s):
        await self._rpc("appControl", "setTextForm", [s[:200]])

    async def power_on_net(self):
        await self._rpc("system", "setPowerStatus", [{"status": True}], "1.1")

    async def power_off_net(self):
        await self._rpc("system", "setPowerStatus", [{"status": False}], "1.1")

    async def set_volume(self, level):
        if self.state == "device":
            await self._rpc("audio", "setAudioVolume", [{"volume": str(max(0, min(100, int(level)))), "target": "speaker"}], "1.2")

    async def apps(self):
        if self.state != "device":
            return []
        if not self._app_uri and self.on:
            try:
                await self._load_apps()
            except Exception as e:                          # noqa: BLE001
                log(f"sony apps {self.host}: {e}")
        colors = {"netflix": "#e50914", "youtube": "#ff0033", "prime": "#00a8e1", "disney": "#113ccf"}
        res = []
        for k, uri in self._app_uri.items():
            n = self._app_name.get(k, k)
            col = next((c for w, c in colors.items() if w in n.lower()), "#3a3f4b")
            res.append({"pkg": k, "name": n, "color": col, "known": col != "#3a3f4b"})
        res.sort(key=lambda x: (not x["known"], x["name"].lower()))
        return res

    async def launch(self, pkg):
        uri = self._app_uri.get(pkg)
        if uri and self.state == "device":
            try:
                await self._rpc("appControl", "setActiveApp", [{"uri": uri}])
            except Exception as e:                          # noqa: BLE001
                log(f"sony launch {pkg}: {e}")

    def has_inputs(self):
        return True

    async def inputs(self):
        if self.state != "device" or not self.on:
            return []
        res = self._lst(await self._rpc("avContent", "getCurrentExternalInputsStatus", [], "1.1"))
        self._inp = [{"id": x["uri"], "name": str(x.get("label") or x.get("title") or x["uri"])[:30], "connected": bool(x.get("connection", True))}
                     for x in res if isinstance(x, dict) and x.get("uri")]
        return self._inp

    async def set_input(self, iid):
        if self.state != "device":
            raise RuntimeError("La TV no está conectada")
        if not self._inp:
            await self.inputs()
        if iid not in {i["id"] for i in self._inp}:
            raise ValueError("Entrada no válida")
        await self._rpc("avContent", "setPlayContent", [{"uri": iid}])

    def hdmi_id(self, n):
        return f"extInput:hdmi?port={int(n)}"

    async def app_icon(self, pkg):
        url = self._app_icon_url.get(pkg)
        return await icons.fetch_remote(url, self.host) if url else None

    async def pair(self, psk=None):
        psk = (psk or "").strip()
        if not re.match(r"^[\x21-\x7e]{1,40}$", psk):
            return False, "Escribe la clave precompartida (PSK) que pusiste en la TV (1-40 caracteres, sin espacios)."
        old = secret_get("sony-psk", self.id)
        secret_set("sony-psk", self.id, psk)
        try:
            r = await self._rpc("system", "getPowerStatus")
        except AuthNeeded:
            secret_set("sony-psk", self.id, old)
            return False, "La TV rechazó la clave. Revisa Ajustes → Red → Configuración de red doméstica → Control IP → Clave precompartida."
        except Exception as e:                              # noqa: BLE001
            secret_set("sony-psk", self.id, old)
            return False, f"No se pudo contactar con la TV: {e}. ¿Está «Control IP» y «Control remoto simple IP» activados?"
        self._codes, self._app_uri = {}, {}
        self.model = ""
        await self.publish(force=True)
        return True, "Clave guardada."


DRIVERS = {"android": AndroidDriver, "vidaa": VidaaDriver, "webos": WebosDriver, "roku": RokuDriver,
           "samsung": SamsungDriver, "sony": SonyDriver}


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
    try:
        hdmi = int(d.get("hdmi", old.get("hdmi", 0)) or 0)
    except (TypeError, ValueError):
        hdmi = 0
    if not 0 <= hdmi <= 4:
        raise ValueError("Entrada HDMI no válida")
    return {"id": old.get("id") or new_id(), "name": name, "type": typ, "host": host, "port": port,
            "hdmi": hdmi, "tv": bool(d.get("tv", old.get("tv", False))),
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

SEC = None                      # sec.Security, created at startup
_WS = {}                        # user id -> {(websocket, session hash)}: closed when the session / user goes away
SAFE = ("GET", "HEAD", "OPTIONS")
PUBLIC = {("GET", "/"), ("GET", "/api/auth/status"), ("POST", "/api/auth/login"), ("POST", "/api/auth/setup")}
ADMIN_ONLY = {("POST", "/api/tvs"), ("PATCH", "/api/tvs/{id}"), ("DELETE", "/api/tvs/{id}"), ("POST", "/api/tvs/{id}/pair"),
              ("GET", "/api/discover"), ("POST", "/api/discover"), ("GET", "/api/ha"), ("PUT", "/api/ha")}
JSON_NOSTORE = {"Cache-Control": "no-store"}


def jerr(msg, status=400, **kw):
    return web.json_response({"error": msg, **kw}, status=status, headers=JSON_NOSTORE)


def kick(uid=None, sid=None, keep=None):
    """Close live WebSockets of a user (or of one session) whose access was revoked."""
    for u in ([uid] if uid else list(_WS)):
        for item in list(_WS.get(u, ())):
            if (sid is None or item[1] == sid) and item[1] != keep:
                _WS[u].discard(item)
                asyncio.ensure_future(item[0].close(code=4401, message=b"session ended"))


def authenticate(request):
    """-> principal dict or None. Sessions (browser) or API tokens (integrations, `Authorization: Bearer ...`)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        raw = auth[7:].strip()
        t = SEC.api_token(raw)
        if t:
            return {"kind": "api", "name": "token:" + t["name"], "role": "user", "uid": ""}
        if TOKEN and hmac.compare_digest(raw, TOKEN):
            return {"kind": "api", "name": "token:env", "role": "user", "uid": ""}
        return None
    if TOKEN and request.query.get("token") and hmac.compare_digest(request.query["token"], TOKEN):
        return {"kind": "api", "name": "token:env", "role": "user", "uid": ""}     # legacy ?token=, kept for old integrations
    h, sess = SEC.session_for(request.cookies.get(SEC.cookie_name(request), ""))
    if not sess:
        return None
    u = SEC.users.get(sess["uid"])
    return {"kind": "session", "name": u["username"], "role": u["role"], "uid": u["id"], "sid": h, "csrf": sess["csrf"]}


@web.middleware
async def headers_mw(request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    if not isinstance(resp, web.WebSocketResponse):
        for k, v in sec.security_headers(SEC.is_secure(request) if SEC else False).items():
            resp.headers.setdefault(k, v)
        if request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


@web.middleware
async def auth_mw(request, handler):
    route = request.match_info.route
    canon = getattr(getattr(route, "resource", None), "canonical", request.path)
    key = (request.method, canon)
    if key in PUBLIC:
        request["pr"] = None
        return await handler(request)
    if not SEC.has_users():
        return jerr("setup_required", 403)
    pr = authenticate(request)
    if pr is None:
        return jerr("auth_required", 401)
    if request.path.startswith("/ws/") or request.method not in SAFE:
        if not SEC.origin_ok(request):
            SEC.audit("origin_rejected", request, user=pr["name"], origin=request.headers.get("Origin"))
            return jerr("Origen no permitido", 403)
        if pr["kind"] == "session" and request.method not in SAFE and \
                not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), pr["csrf"]):
            SEC.audit("csrf_rejected", request, user=pr["name"])
            return jerr("Sesión no válida; recarga la página", 403)
    if (key in ADMIN_ONLY or canon.startswith("/api/users") or canon.startswith("/api/tokens")) and pr["role"] != "admin":
        SEC.audit("forbidden", request, user=pr["name"], path=request.path)
        return jerr("Solo un administrador puede hacer esto", 403)
    request["pr"] = pr
    return await handler(request)


INDEX_HTML = None


async def index(request):
    global INDEX_HTML
    if INDEX_HTML is None or os.environ.get("TVHUB_DEV"):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            INDEX_HTML = f.read()
    nonce = secrets.token_urlsafe(16)
    html = INDEX_HTML.replace("<script>", f'<script nonce="{nonce}">')
    resp = web.Response(text=html, content_type="text/html", charset="utf-8")
    for k, v in sec.security_headers(SEC.is_secure(request), request.host, nonce).items():
        resp.headers[k] = v
    resp.headers["Cache-Control"] = "no-store"
    return resp


def set_session_cookie(request, resp, token, remember=False):
    resp.set_cookie(SEC.cookie_name(request), token, httponly=True, samesite="Strict", secure=SEC.is_secure(request),
                    path="/", max_age=(30 * 86400 if remember else None))


def clear_session_cookie(request, resp):
    resp.del_cookie(SEC.cookie_name(request), path="/")


async def read_json(request):
    try:
        d = await request.json()
    except Exception:                                   # noqa: BLE001 - any malformed body is a 400
        raise web.HTTPBadRequest(text=json.dumps({"error": "Petición no válida"}), content_type="application/json")
    if not isinstance(d, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "Petición no válida"}), content_type="application/json")
    return d


def me_payload(pr, extra=None):
    return {"authenticated": True, "user": {"username": pr["name"], "role": pr["role"]}, "csrf": pr.get("csrf", ""), **(extra or {})}


async def api_auth_status(request):
    if not SEC.has_users():
        return web.json_response({"setup_required": True, "authenticated": False}, headers=JSON_NOSTORE)
    pr = authenticate(request)
    if pr and pr["kind"] == "session":
        return web.json_response(me_payload(pr, {"setup_required": False}), headers=JSON_NOSTORE)
    return web.json_response({"setup_required": False, "authenticated": False}, headers=JSON_NOSTORE)


async def _start_session(request, u, remember=False):
    token, s = SEC.create_session(u, request, remember)
    resp = web.json_response({"authenticated": True, "user": {"username": u["username"], "role": u["role"]}, "csrf": s["csrf"],
                              "setup_required": False}, headers=JSON_NOSTORE)
    set_session_cookie(request, resp, token, remember)
    return resp


async def api_setup(request):
    """Create the first administrator. Only possible while no user exists and with the code printed in the log."""
    ip = SEC.client_ip(request)
    d = await read_json(request)
    wait = SEC.limiter.wait(ip, "setup")
    if wait:
        return jerr(f"Demasiados intentos. Espera {wait} s", 429, retry_after=wait)
    async with SEC.setup_lock:
        if SEC.has_users():
            return jerr("TVHub ya está configurado", 409)
        norm = lambda x: re.sub(r"[^A-Z0-9]", "", str(x or "").upper())          # noqa: E731
        if not hmac.compare_digest(norm(d.get("code")).encode(), norm(SEC.setup_code).encode()):
            SEC.limiter.fail(ip, "setup")
            SEC.audit("setup_bad_code", request)
            return jerr("Código de configuración incorrecto", 403)
        username = sec.norm_username(d.get("username"))
        pw = d.get("password")
        err = sec.check_username(username) or sec.check_password(pw, username)
        if err:
            return jerr(err)
        u = await SEC.add_user(username, pw, "admin")
        SEC.setup_code = ""
    SEC.audit("setup_admin_created", request, user=username)
    return await _start_session(request, u)


async def api_login(request):
    ip = SEC.client_ip(request)
    d = await read_json(request)
    username = sec.norm_username(d.get("username"))[:64]
    pw = d.get("password")
    if not isinstance(pw, str) or not pw or len(pw) > 1024:
        return jerr("Usuario o contraseña incorrectos", 401)
    wait = SEC.limiter.wait(ip, username)
    if wait:
        SEC.audit("login_blocked", request, user=username)
        return web.json_response({"error": f"Demasiados intentos. Espera {wait} s", "retry_after": wait}, status=429,
                                 headers={**JSON_NOSTORE, "Retry-After": str(wait)})
    u = await SEC.verify_login(username, pw)
    if u is None:
        SEC.limiter.fail(ip, username)
        SEC.audit("login_failed", request, user=username)
        return jerr("Usuario o contraseña incorrectos", 401)
    SEC.limiter.success(ip, username)
    SEC.audit("login", request, user=username)
    return await _start_session(request, u, bool(d.get("remember")))


async def api_logout(request):
    pr = request["pr"]
    if pr["kind"] == "session":
        SEC.revoke(pr["sid"])
        kick(sid=pr["sid"])
    SEC.audit("logout", request, user=pr["name"])
    resp = web.json_response({"ok": True}, headers=JSON_NOSTORE)
    clear_session_cookie(request, resp)
    return resp


async def api_password(request):
    pr = request["pr"]
    if pr["kind"] != "session":
        return jerr("Solo desde una sesión de usuario", 403)
    d = await read_json(request)
    ip = SEC.client_ip(request)
    u = SEC.users[pr["uid"]]
    wait = SEC.limiter.wait(ip, u["username"])
    if wait:
        return jerr(f"Demasiados intentos. Espera {wait} s", 429, retry_after=wait)
    cur, new = d.get("current"), d.get("password")
    if not isinstance(cur, str) or await SEC.verify_login(u["username"], cur) is None:
        SEC.limiter.fail(ip, u["username"])
        SEC.audit("password_change_failed", request, user=u["username"])
        return jerr("La contraseña actual no es correcta", 403)
    err = sec.check_password(new, u["username"])
    if err:
        return jerr(err)
    await SEC.set_password(u, new)
    kick(uid=u["id"])
    SEC.audit("password_changed", request, user=u["username"])
    return await _start_session(request, u)             # all older sessions are void; this browser gets a fresh one


async def api_users(request):
    return web.json_response({"users": [SEC.public(u) for u in SEC.users.values()],
                              "tokens": [{"id": t["id"], "name": t["name"], "created": t["created"], "last_used": t.get("last_used", 0)}
                                         for t in SEC.tokens.values()]}, headers=JSON_NOSTORE)


async def api_user_add(request):
    d = await read_json(request)
    username = sec.norm_username(d.get("username"))
    role = d.get("role", "user")
    err = sec.check_username(username) or (None if role in sec.ROLES else "Rol no válido") or sec.check_password(d.get("password"), username)
    if err:
        return jerr(err)
    if SEC.find(username):
        return jerr("Ya existe un usuario con ese nombre", 409)
    u = await SEC.add_user(username, d["password"], role)
    SEC.audit("user_created", request, user=request["pr"]["name"], target=username, role=role)
    return web.json_response(SEC.public(u), headers=JSON_NOSTORE)


async def api_user_edit(request):
    u = SEC.users.get(request.match_info["id"])
    if not u:
        return jerr("Usuario no encontrado", 404)
    d = await read_json(request)
    me = request["pr"]
    if "role" in d:
        if d["role"] not in sec.ROLES:
            return jerr("Rol no válido")
        if u["role"] == "admin" and d["role"] != "admin" and len(SEC.admins()) <= 1:
            return jerr("No puedes quitar al último administrador")
        if d["role"] != u["role"]:
            u["role"] = d["role"]
            SEC.save_users()
            kick(uid=u["id"])
            SEC.audit("role_changed", request, user=me["name"], target=u["username"], role=d["role"])
    if "disabled" in d:
        dis = bool(d["disabled"])
        if dis and u["role"] == "admin" and len(SEC.admins()) <= 1:
            return jerr("No puedes desactivar al último administrador")
        u["disabled"] = dis
        SEC.save_users()
        if dis:
            SEC.revoke_user(u["id"])
            kick(uid=u["id"])
        SEC.audit("user_disabled" if dis else "user_enabled", request, user=me["name"], target=u["username"])
    if d.get("password"):
        err = sec.check_password(d["password"], u["username"])
        if err:
            return jerr(err)
        await SEC.set_password(u, d["password"])
        kick(uid=u["id"])
        SEC.audit("password_reset", request, user=me["name"], target=u["username"])
    return web.json_response(SEC.public(u), headers=JSON_NOSTORE)


async def api_user_delete(request):
    u = SEC.users.get(request.match_info["id"])
    if not u:
        return jerr("Usuario no encontrado", 404)
    if u["role"] == "admin" and len(SEC.admins()) <= 1:
        return jerr("No puedes quitar al último administrador")
    SEC.delete_user(u["id"])
    kick(uid=u["id"])
    SEC.audit("user_deleted", request, user=request["pr"]["name"], target=u["username"])
    return web.json_response({"ok": True}, headers=JSON_NOSTORE)


async def api_token_add(request):
    d = await read_json(request)
    name = re.sub(r"[^\w .@-]", "", str(d.get("name", "")))[:40].strip()
    if not name:
        return jerr("Ponle un nombre al token")
    raw, rec = SEC.create_api_token(name)
    SEC.audit("api_token_created", request, user=request["pr"]["name"], target=name)
    return web.json_response({"token": raw, "id": rec["id"], "name": rec["name"]}, headers=JSON_NOSTORE)


async def api_token_delete(request):
    ok = SEC.delete_api_token(request.match_info["id"])
    SEC.audit("api_token_revoked", request, user=request["pr"]["name"], target=request.match_info["id"])
    return web.json_response({"ok": ok}, headers=JSON_NOSTORE)


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
    SEC.audit("ha_configured", request, user=request["pr"]["name"])
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
    SEC.audit("tv_added", request, user=request["pr"]["name"], target=cfg["name"], host=cfg["host"])
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
    SEC.audit("tv_deleted", request, user=request["pr"]["name"], target=tv.cfg.get("name"), host=tv.cfg.get("host"))
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
    if tv.type == "sony":                       # a pre-shared key, not a numeric PIN
        pin = str(d.get("pin", ""))[:40]
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


async def power_one(tv, on):
    """Power one TV for the "all TVs" button. Returns (status, detail): status is ok / skipped / fail."""
    mac = tv.cfg.get("mac")
    if tv.type == "android":
        if tv.state != "device":
            return "fail", "ADB no responde (¿en reposo profundo o sin red?)"
        await tv.power(on)
        return "ok", ""
    is_on = tv.state == "device" and tv.extra().get("on") is not False       # None (unknown) counts as on
    if on:
        if is_on:
            return "skipped", "ya estaba encendida"
        if tv.state == "device":                  # reachable but in standby (Roku, Sony, Samsung…): ask it directly
            await tv.power(True)
            return "ok", "orden de encendido enviada"
        if not mac:
            return "fail", "no se puede encender sin su MAC (Wake-on-LAN); añádela en Ajustes"
        await asyncio.get_running_loop().run_in_executor(None, wake_on_lan, mac)
        return "ok", "Wake-on-LAN enviado; tarda unos segundos"
    if not is_on:
        return "skipped", "ya estaba apagada o sin conexión"
    await tv.power(False)
    return "ok", ""


async def api_power_all(request):
    d = await request.json()
    on = bool(d.get("on"))

    async def one(tv):
        try:
            st, detail = await asyncio.wait_for(power_one(tv, on), 15)
        except asyncio.TimeoutError:
            st, detail = "fail", "la TV no respondió a tiempo"
        except Exception as e:            # noqa: BLE001 — one TV failing must not stop the others
            st, detail = "fail", str(e) or e.__class__.__name__
        return {"id": tv.id, "name": tv.cfg.get("name", ""), "type": tv.type, "status": st, "detail": detail}

    results = await asyncio.gather(*(one(tv) for tv in list(hub.tvs.values())))
    return web.json_response({"on": on, "results": results})


async def api_power(request):
    tv = get_tv(request)
    on = bool((await request.json()).get("on"))
    try:
        status, detail = await asyncio.wait_for(power_one(tv, on), 15)
    except asyncio.TimeoutError:
        status, detail = "fail", "la TV no respondió a tiempo"
    return web.json_response({"ok": status != "fail", "status": status, "detail": detail})


async def api_inputs(request):
    tv = get_tv(request)
    return web.json_response(await tv.inputs() if tv.has_inputs() else [])


async def api_set_input(request):
    tv = get_tv(request)
    iid = str((await request.json()).get("id", ""))
    if not tv.has_inputs():
        return web.json_response({"error": "Esta TV no permite cambiar de entrada"}, status=400)
    try:
        await tv.set_input(iid)
    except (ValueError, RuntimeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True})


async def api_show(request):
    """Switch the TV that a Stick's volume is routed to onto the HDMI port the Stick is plugged into."""
    stick = get_tv(request)
    target = hub.tvs.get(stick.cfg.get("volume_via") or "")
    n = int(stick.cfg.get("hdmi") or 0)
    if target is None or not n:
        return web.json_response({"error": "Indica en Ajustes la TV y el HDMI donde está conectado este dispositivo"}, status=400)
    if not target.has_inputs():
        return web.json_response({"error": f"«{target.cfg.get('name')}» no permite cambiar de entrada"}, status=400)
    try:
        await target.set_input(target.hdmi_id(n))
    except (ValueError, RuntimeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "tv": target.cfg.get("name"), "hdmi": n})


async def api_appicon(request):
    tv = get_tv(request)
    pkg = request.match_info["pkg"]
    headers = {"Cache-Control": "public, max-age=86400"}
    miss = web.Response(status=404, headers={"Cache-Control": "public, max-age=3600"})
    if not PKG_RE.match(pkg):
        return miss
    m = icons.HDMI_RE.search(pkg.lower())
    if m:
        return web.Response(body=icons.hdmi_tile(m.group(1)), content_type="image/svg+xml", headers=headers)
    own = await tv.app_icon(pkg)                      # the TV's own icon beats a generic brand logo
    if own:
        return web.Response(body=own[0], content_type=own[1], headers=headers)
    name = ""
    try:
        name = next((a["name"] for a in (await tv.apps()) if a["pkg"] == pkg), "")
    except Exception:                                 # noqa: BLE001
        pass
    b = icons.brand_for(pkg, name)
    if b:
        svg = await icons.brand_tile(os.path.join(DATA, "icons"), *b)
        if svg:
            return web.Response(body=svg, content_type="image/svg+xml", headers=headers)
    return miss


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
    pr = request["pr"]
    track = (ws, pr.get("sid"))
    if pr.get("uid"):
        _WS.setdefault(pr["uid"], set()).add(track)
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
        _WS.get(pr.get("uid"), set()).discard(track)
        await st.remove(ws)
    return ws


async def ws_control(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    pr = request["pr"]
    track = (ws, pr.get("sid"))
    if pr.get("uid"):
        _WS.setdefault(pr["uid"], set()).add(track)
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
        _WS.get(pr.get("uid"), set()).discard(track)
        hub.clients.discard(ws)
    return ws


async def on_start(app):
    global ha, SEC
    SEC = sec.Security(DATA)
    app["flusher"] = asyncio.ensure_future(SEC.flusher())
    if not SEC.has_users():
        log("=" * 64)
        log("FIRST RUN: no user exists yet. Open TVHub in your browser and create the administrator.")
        log(f"Setup code (needed once): {SEC.setup_code}")
        log("=" * 64)
    await load_registry()
    from ha import HABridge
    ha = HABridge(hub, log, DATA, route, KEY_RE)
    ha.start(asyncio.get_running_loop())


async def on_stop(app):
    app["flusher"].cancel()
    if ha:
        ha.stop()
    for d in list(hub.tvs.values()):
        await d.stop()


def make_app():
    app = web.Application(middlewares=[headers_mw, auth_mw], client_max_size=256 * 1024)
    app.add_routes([
        web.get("/", index),
        web.get("/api/auth/status", api_auth_status),
        web.post("/api/auth/setup", api_setup),
        web.post("/api/auth/login", api_login),
        web.post("/api/auth/logout", api_logout),
        web.post("/api/auth/password", api_password),
        web.get("/api/users", api_users),
        web.post("/api/users", api_user_add),
        web.patch("/api/users/{id}", api_user_edit),
        web.delete("/api/users/{id}", api_user_delete),
        web.post("/api/tokens", api_token_add),
        web.delete("/api/tokens/{id}", api_token_delete),
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
        web.post("/api/power-all", api_power_all),
        web.get("/api/tvs/{id}/inputs", api_inputs),
        web.post("/api/tvs/{id}/input", api_set_input),
        web.post("/api/tvs/{id}/show", api_show),
        web.get("/api/tvs/{id}/appicon/{pkg}", api_appicon),
        web.get("/ws/video", ws_video),
        web.get("/ws/control", ws_control),
    ])
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log(f"TVHub on :{port}  vidaa={'yes' if _vidaa else 'no'}  legacy-token={'on' if TOKEN else 'off'}")
    web.run_app(make_app(), host="0.0.0.0", port=port, print=None)
