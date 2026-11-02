"""Home Assistant bridge for TVHub, over MQTT discovery.

Every TV in TVHub shows up in Home Assistant as a device with buttons (d-pad, volume, home...),
an app picker, a text box, and sensors (state, current app, volume). No custom component and no
HACS: Home Assistant only needs its MQTT integration (e.g. the Mosquitto add-on).

Enabled by setting MQTT_HOST. Everything else is optional:
  MQTT_PORT (1883)  MQTT_USER  MQTT_PASSWORD  MQTT_TLS (1 = TLS)  MQTT_TLS_INSECURE (1 = skip cert check)
  HA_DISCOVERY_PREFIX (homeassistant)  MQTT_PREFIX (tvhub)

Topics (prefix = tvhub):
  tvhub/bridge              online / offline (retained, last will)
  tvhub/<id>/state          JSON: state, app, volume, muted, model
  tvhub/<id>/avail          online when the TV answers, offline otherwise
  tvhub/<id>/app_state      name of the foreground app (select state)
  tvhub/<id>/key|text|app   commands (never retained)
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,100}$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

try:
    import paho.mqtt.client as mqtt
except Exception:                         # noqa: BLE001
    mqtt = None

# (object id, label, TVHub key name, mdi icon)
BUTTONS = [
    ("up", "Arriba", "DPAD_UP", "mdi:arrow-up-bold"),
    ("down", "Abajo", "DPAD_DOWN", "mdi:arrow-down-bold"),
    ("left", "Izquierda", "DPAD_LEFT", "mdi:arrow-left-bold"),
    ("right", "Derecha", "DPAD_RIGHT", "mdi:arrow-right-bold"),
    ("ok", "OK", "DPAD_CENTER", "mdi:checkbox-blank-circle-outline"),
    ("back", "Atrás", "BACK", "mdi:undo-variant"),
    ("home", "Inicio", "HOME", "mdi:home"),
    ("menu", "Menú", "MENU", "mdi:menu"),
    ("play_pause", "Play / Pausa", "MEDIA_PLAY_PAUSE", "mdi:play-pause"),
    ("rewind", "Retroceder", "MEDIA_REWIND", "mdi:rewind"),
    ("forward", "Avanzar", "MEDIA_FAST_FORWARD", "mdi:fast-forward"),
    ("vol_up", "Subir volumen", "VOLUME_UP", "mdi:volume-plus"),
    ("vol_down", "Bajar volumen", "VOLUME_DOWN", "mdi:volume-minus"),
    ("mute", "Silenciar", "VOLUME_MUTE", "mdi:volume-mute"),
    ("power", "Encender / apagar", "POWER", "mdi:power"),
]
MAKER = {"android": "Android TV / Fire TV", "vidaa": "Hisense", "webos": "LG"}
LEGACY = "tvdeck"            # old name of this project: its retained MQTT topics are withdrawn once
STATE_TPL = ("{{ {'device': 'Conectada', 'disconnected': 'Sin conexión', 'needs_pairing': 'Falta emparejar', "
             "'unauthorized': 'Falta autorizar'}.get(value_json.state, value_json.state) }}")
APPS_TTL = 600           # seconds between app-list refreshes
MAX_APPS = 80


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class HABridge:
    def __init__(self, hub, log, data_dir, route, key_re):
        self.hub, self.log, self.route, self.key_re = hub, log, route, key_re
        self.cfg_path = Path(data_dir) / "ha.json"
        self.error = ""
        self._apply(self._load_cfg())
        if self.host and mqtt is None:
            log("MQTT is configured but paho-mqtt is not installed: Home Assistant bridge disabled")
        self.connected = False
        self.client = None
        self.loop = None
        self.last = {}                    # tv id -> last state JSON string
        self.apps = {}                    # tv id -> {"opts": [...], "map": {name: pkg}, "t": ts}
        self.refreshing = set()
        self.known_path = Path(data_dir) / "ha-known.json"
        self.known = self._load_known()

    # ------------------------------------------------------------------ configuration
    # Saved settings (web UI) win over the MQTT_* environment variables, which only seed the defaults.
    @staticmethod
    def _env_cfg():
        return {"host": os.environ.get("MQTT_HOST", "").strip(),
                "port": os.environ.get("MQTT_PORT", "1883") or 1883,
                "user": os.environ.get("MQTT_USER", ""), "password": os.environ.get("MQTT_PASSWORD", ""),
                "tls": _flag("MQTT_TLS"), "insecure": _flag("MQTT_TLS_INSECURE"),
                "prefix": os.environ.get("MQTT_PREFIX", "tvhub"),
                "discovery": os.environ.get("HA_DISCOVERY_PREFIX", "homeassistant")}

    def _load_cfg(self):
        cfg = self._env_cfg()
        try:
            with open(self.cfg_path) as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        if cfg.get("prefix") == LEGACY:                # saved before the project was renamed from TVDeck to TVHub
            cfg["prefix"] = "tvhub"
        return cfg

    def _apply(self, cfg):
        self.host = str(cfg.get("host") or "").strip()
        try:
            self.port = int(cfg.get("port") or 1883)
        except (TypeError, ValueError):
            self.port = 1883
        self.user = str(cfg.get("user") or "")
        self.password = str(cfg.get("password") or "")
        self.tls, self.insecure = bool(cfg.get("tls")), bool(cfg.get("insecure"))
        self.prefix = str(cfg.get("prefix") or "tvhub").strip("/") or "tvhub"
        self.dp = str(cfg.get("discovery") or "homeassistant").strip("/") or "homeassistant"

    @property
    def enabled(self):
        return bool(self.host) and mqtt is not None

    @staticmethod
    def clean(d, old_password=""):
        """Validate settings coming from the web UI. Raises ValueError with a message for the user."""
        host = str(d.get("host") or "").strip()
        if host and not HOST_RE.match(host):
            raise ValueError("IP o nombre del broker no válido")
        try:
            port = int(d.get("port") or 1883)
        except (TypeError, ValueError):
            raise ValueError("Puerto no válido")
        if not 1 <= port <= 65535:
            raise ValueError("Puerto no válido")
        prefix = str(d.get("prefix") or "tvhub").strip("/")
        disc = str(d.get("discovery") or "homeassistant").strip("/")
        for v in (prefix, disc):
            if not TOPIC_RE.match(v):
                raise ValueError("Los prefijos solo admiten letras, números, - y _")
        pw = d.get("password")
        if pw is None or (pw == "" and d.get("keep_password")):
            pw = old_password
        return {"host": host, "port": port, "user": str(d.get("user") or "").strip()[:100],
                "password": str(pw or "")[:200], "tls": bool(d.get("tls")), "insecure": bool(d.get("insecure")),
                "prefix": prefix, "discovery": disc}

    def configure(self, cfg):
        """Save new settings and reconnect with them (called from the web UI)."""
        if self.connected and (cfg["prefix"], cfg["discovery"]) != (self.prefix, self.dp):
            for tv_id in set(self.known):             # withdraw the entities published under the old prefixes
                self._remove(tv_id)
            self.known = set()
            self._save_known()
        self.stop()
        try:
            tmp = str(self.cfg_path) + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f)
            os.replace(tmp, self.cfg_path)
        except OSError as e:
            self.log("cannot save Home Assistant settings:", e)
        self._apply(cfg)
        self.error = ""
        self.last.clear()
        self.apps.clear()
        if self.loop:
            self.start(self.loop)

    # ------------------------------------------------------------------ lifecycle
    def status(self):
        return {"enabled": self.enabled, "connected": self.connected, "error": self.error,
                "host": self.host, "port": self.port, "user": self.user, "has_password": bool(self.password),
                "tls": self.tls, "insecure": self.insecure, "prefix": self.prefix, "discovery": self.dp,
                "available": mqtt is not None}

    def start(self, loop):
        self.loop = loop
        if not self.enabled:
            return
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"tvhub-{os.getpid()}",
                            protocol=mqtt.MQTTv311)
        except AttributeError:
            self.log("paho-mqtt 2.x is required for the Home Assistant bridge")
            self.error = "Falta paho-mqtt 2.x en el servidor"
            return
        if self.user:
            c.username_pw_set(self.user, self.password)
        if self.tls:
            c.tls_set()
            if self.insecure:
                c.tls_insecure_set(True)
        c.on_connect_fail = self._on_connect_fail
        c.will_set(f"{self.prefix}/bridge", "offline", qos=1, retain=True)
        c.on_connect, c.on_disconnect, c.on_message = self._on_connect, self._on_disconnect, self._on_message
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client = c
        try:
            c.connect_async(self.host, self.port, keepalive=30)
            c.loop_start()
            self.log(f"Home Assistant bridge: connecting to MQTT {self.host}:{self.port}")
        except Exception as e:                # noqa: BLE001
            self.log("Home Assistant bridge:", e)

    def stop(self):
        c, self.client = self.client, None
        if c:
            try:
                if self.connected:
                    c.publish(f"{self.prefix}/bridge", "offline", qos=1, retain=True).wait_for_publish(2)
                c.loop_stop()
                c.disconnect()
            except Exception:                 # noqa: BLE001
                pass
        self.connected = False

    # ------------------------------------------------------------------ paho callbacks (their thread)
    def _on_connect_fail(self, _client, _ud):
        self.error = f"No llego al broker en {self.host}:{self.port}. Comprueba la IP, el puerto y el cifrado."

    def _on_connect(self, client, _ud, _flags, rc, _props):
        if getattr(rc, "is_failure", False) or (isinstance(rc, int) and rc != 0):
            self.error = "El broker rechazó el usuario o la contraseña." if "uth" in str(rc) or "assword" in str(rc) \
                else f"El broker rechazó la conexión ({rc})."
            self.log(f"Home Assistant bridge: MQTT refused the connection ({rc})")
            return
        self.error = ""
        self.connected = True
        self.log("Home Assistant bridge: connected")
        client.publish(f"{self.prefix}/bridge", "online", qos=1, retain=True)
        for kind in ("key", "text", "app"):
            client.subscribe(f"{self.prefix}/+/{kind}", qos=1)
        self.last.clear()
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self.sync()))

    def _on_disconnect(self, _client, _ud, _flags, rc, _props):
        if self.connected:
            self.log(f"Home Assistant bridge: disconnected ({rc})")
        self.connected = False

    def _on_message(self, _client, _ud, msg):
        if msg.retain:                        # never replay stale commands after a restart
            return
        m = re.match(rf"^{re.escape(self.prefix)}/([^/]+)/(key|text|app)$", msg.topic)
        if not m or not self.loop:
            return
        try:
            payload = msg.payload.decode("utf-8", "replace")
        except Exception:                     # noqa: BLE001
            return
        self.loop.call_soon_threadsafe(self._command, m.group(1), m.group(2), payload)

    # ------------------------------------------------------------------ commands (event loop)
    def _command(self, tv_id, kind, payload):
        tv = self.hub.tvs.get(tv_id)
        if not tv:
            return
        try:
            if kind == "key":
                k = payload.strip()
                if self.key_re.match(k):
                    self.route(tv, k).key(k)
            elif kind == "text" and tv.caps.get("text"):
                tv.text(payload[:255])
            elif kind == "app" and tv.caps.get("apps"):
                pkg = (self.apps.get(tv_id) or {}).get("map", {}).get(payload.strip())
                if pkg:
                    self.loop.create_task(tv.launch(pkg))
        except Exception as e:                # noqa: BLE001
            self.log("Home Assistant command:", e)

    # ------------------------------------------------------------------ publishing
    def _pub(self, topic, payload, retain=True):
        c = self.client
        if c is not None and self.connected:
            c.publish(topic, payload, qos=1, retain=retain)

    def _cfg_topic(self, comp, tv_id, obj, legacy=False):
        return f"{self.dp}/{comp}/{LEGACY if legacy else 'tvhub'}_{tv_id}/{obj}/config"

    def _load_known(self):
        try:
            with open(self.known_path) as f:
                return set(json.load(f))
        except (OSError, ValueError):
            return set()

    def _save_known(self):
        try:
            with open(self.known_path, "w") as f:
                json.dump(sorted(self.known), f)
        except OSError:
            pass

    async def sync(self):
        """(Re)publish discovery for every TV and withdraw the ones that no longer exist."""
        if not self.enabled or not self.connected:
            return
        ids = set(self.hub.tvs)
        self._drop_legacy(ids | self.known)
        for gone in self.known - ids:
            self._remove(gone)
        for tv in self.hub.tvs.values():
            self._discovery(tv)
            self.update(tv.status(), force=True)
        self.known = ids
        self._save_known()

    def _drop_legacy(self, ids):
        """One-time: delete the entities this bridge published under its old name (TVDeck)."""
        marker = self.known_path.with_name("ha-legacy-done")
        if marker.exists():
            return
        for tv_id in ids:
            self._remove(tv_id, legacy=True)
        self._pub(f"{LEGACY}/bridge", "")
        try:
            marker.write_text("ok")
        except OSError:
            pass

    def _remove(self, tv_id, legacy=False):
        pre = LEGACY if legacy else self.prefix
        objs = [("sensor", "state"), ("sensor", "app"), ("sensor", "volume"), ("binary_sensor", "connected"),
                ("select", "app"), ("text", "text")] + [("button", b[0]) for b in BUTTONS]
        for comp, obj in objs:
            self._pub(self._cfg_topic(comp, tv_id, obj, legacy), "")
        for t in ("state", "avail", "app_state"):
            self._pub(f"{pre}/{tv_id}/{t}", "")
        if not legacy:
            self.last.pop(tv_id, None)
            self.apps.pop(tv_id, None)

    def _discovery(self, tv):
        i, p = tv.id, self.prefix
        snap = tv.status()
        device = {"identifiers": [f"tvhub_{i}"], "name": snap["name"], "manufacturer": MAKER.get(tv.type, "TV"),
                  "model": snap.get("model") or tv.type, "configuration_url": os.environ.get("TVHUB_URL", "") or None}
        device = {k: v for k, v in device.items() if v}
        origin = {"name": "TVHub"}
        bridge = {"topic": f"{p}/bridge"}
        both = [bridge, {"topic": f"{p}/{i}/avail"}]

        def put(comp, obj, cfg, needs_tv=True):
            cfg.update(unique_id=f"tvhub_{i}_{obj}", device=device, origin=origin, has_entity_name=True,
                       availability=both if needs_tv else [bridge], availability_mode="all")
            self._pub(self._cfg_topic(comp, i, obj), json.dumps(cfg))

        st = f"{p}/{i}/state"
        put("sensor", "state", {"name": "Estado", "state_topic": st, "icon": "mdi:television",
                                "value_template": STATE_TPL}, needs_tv=False)
        put("binary_sensor", "connected", {"name": "Conectada", "state_topic": st, "device_class": "connectivity",
                                           "value_template": "{{ 'ON' if value_json.state == 'device' else 'OFF' }}"},
            needs_tv=False)
        if tv.caps.get("apps"):
            put("sensor", "app", {"name": "App actual", "state_topic": st, "icon": "mdi:apps",
                                  "value_template": "{{ value_json.app or 'ninguna' }}"})
            opts = (self.apps.get(i) or {}).get("opts") or []
            put("select", "app", {"name": "Abrir app", "icon": "mdi:application-import",
                                  "command_topic": f"{p}/{i}/app", "state_topic": f"{p}/{i}/app_state",
                                  "options": opts or ["—"]})
        if tv.type in ("vidaa", "webos"):
            put("sensor", "volume", {"name": "Volumen", "state_topic": st, "icon": "mdi:volume-high",
                                     "state_class": "measurement",
                                     "value_template": "{{ value_json.volume if value_json.volume is not none else 'unknown' }}"})
        if tv.caps.get("text"):
            put("text", "text", {"name": "Escribir texto", "icon": "mdi:keyboard", "command_topic": f"{p}/{i}/text",
                                 "max": 255})
        for obj, label, key, icon in BUTTONS:
            put("button", obj, {"name": label, "icon": icon, "command_topic": f"{p}/{i}/key", "payload_press": key})

    def update(self, snap, force=False):
        """Publish a TV's state if it changed. Called on every status change of any driver."""
        if not self.enabled or not self.connected:
            return
        i = snap["id"]
        doc = {"state": snap.get("state"), "app": snap.get("app") or "", "volume": snap.get("volume"),
               "muted": snap.get("muted"), "model": snap.get("model") or ""}
        s = json.dumps(doc, sort_keys=True)
        if not force and self.last.get(i) == s:
            return
        self.last[i] = s
        self._pub(f"{self.prefix}/{i}/state", s)
        self._pub(f"{self.prefix}/{i}/avail", "online" if snap.get("state") == "device" else "offline")
        opts = (self.apps.get(i) or {}).get("opts") or []
        self._pub(f"{self.prefix}/{i}/app_state", doc["app"] if doc["app"] in opts else "None")
        tv = self.hub.tvs.get(i)
        if tv and snap.get("state") == "device" and tv.caps.get("apps"):
            a = self.apps.get(i)
            if (not a or time.time() - a["t"] > APPS_TTL) and i not in self.refreshing and self.loop:
                self.refreshing.add(i)
                self.loop.create_task(self._refresh_apps(tv))

    async def _refresh_apps(self, tv):
        try:
            res = await tv.apps()
            names, mp = [], {}
            for a in res[:MAX_APPS]:
                n = a["name"]
                if n in mp:
                    n = f"{n} ({a['pkg']})"
                mp[n] = a["pkg"]
                names.append(n)
            old = self.apps.get(tv.id)
            self.apps[tv.id] = {"opts": names, "map": mp, "t": time.time()}
            if names and (not old or old["opts"] != names):
                self._discovery(tv)
                self.update(tv.status(), force=True)
        except Exception as e:                # noqa: BLE001
            self.log("Home Assistant apps:", e)
        finally:
            self.refreshing.discard(tv.id)
