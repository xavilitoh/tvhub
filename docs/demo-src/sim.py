"""Simulated TVs for the README demo: real TVHub server + UI, fake drivers (test-pattern screen, mocked search).

Usage:  DEMO_DATA=/tmp/tvhub-demo python docs/demo-src/sim.py   (serves http://127.0.0.1:8099, login demo / demo-password-123)
Then:   CHROMIUM=/path/to/chromium python docs/demo-src/rec.py  -> writes demo.gif (needs: playwright, pillow)."""
import asyncio, io, math, os, sys, time, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app"))
DATA = os.environ["DEMO_DATA"]; os.environ.update(DATA_DIR=DATA, TVHUB_DEV="")
import server
from PIL import Image, ImageDraw, ImageFont

PAL = {"Netflix": ((229, 9, 20), (30, 4, 6)), "YouTube": ((255, 40, 40), (40, 8, 8)), "Prime Video": ((0, 168, 225), (4, 30, 46)),
       "Disney+": ((17, 60, 207), (6, 12, 44)), "Inicio": ((90, 110, 160), (14, 18, 30))}

def font(n):
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf"):
        try: return ImageFont.truetype(p, n)
        except Exception: pass
    return ImageFont.load_default()

def frame(label, app):
    a, b = PAL.get(app, PAL["Inicio"]); W, H = 960, 540
    im = Image.new("RGB", (W, H)); px = im.load(); t = time.time()
    for y in range(H):
        for x in range(0, W, 4):
            k = min(1, max(0, x / W * .6 + y / H * .4 + .12 * math.sin(t * .8 + x / 140)))
            c = tuple(int(b[i] + (a[i] - b[i]) * k) for i in range(3))
            for dx in range(4): px[x + dx, y] = c
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((60, 340, 60 + 24 * len(app) + 40, 400), 14, fill=(0, 0, 0))
    d.text((80, 350), app or "Inicio", fill="white", font=font(38))
    d.text((60, 70), label, fill=(255, 255, 255), font=font(56))
    bar = int(((t * 0.03) % 1) * (W - 120)); d.rounded_rectangle((60, 470, W - 60, 478), 4, fill=(255, 255, 255, 80)); d.rounded_rectangle((60, 470, 60 + bar, 478), 4, fill="white")
    out = io.BytesIO(); im.save(out, "PNG"); return out.getvalue()

class Fake(server.Driver):
    apps_list = []; vol = None
    def __init__(s, cfg):
        super().__init__(cfg); s.state = "device"; s.model = cfg.get("_model", ""); s.focus = cfg.get("_app", ""); s.on = True
        s.volume = cfg.get("_vol"); s.muted = False
    async def run(s):
        while True: await asyncio.sleep(3600)
    def extra(s):
        e = {"focus": s.focus, "app": s.focus, "on": s.on}
        if s.volume is not None: e.update(volume=s.volume, muted=s.muted)
        return e
    def key(s, name):
        if name == "VOLUME_UP" and s.volume is not None: s.volume = min(100, s.volume + 2)
        elif name == "VOLUME_DOWN" and s.volume is not None: s.volume = max(0, s.volume - 2)
        elif name == "MUTE": s.muted = not s.muted
        elif name == "HOME": s.focus = "Inicio"
        asyncio.get_event_loop().create_task(s.publish())
    async def apps(s):
        return [{"pkg": n, "name": n, "color": c, "known": True} for n, c in
                (("Netflix", "#e50914"), ("YouTube", "#ff0000"), ("Prime Video", "#00a8e1"), ("Disney+", "#113ccf"))]
    async def launch(s, pkg):
        s.focus = pkg; await s.publish(True)
    async def snapshot(s): return frame(s.cfg["name"], s.focus)
    async def power(s, on):
        s.on = bool(on); await s.publish(True)
    async def set_volume(s, level): s.volume = level; await s.publish()

class FakeAndroid(Fake):
    type = "android"; caps = {"video": True, "apps": True, "text": True, "snapshot": True, "pair": False}
class FakeWebos(Fake):
    type = "webos"; caps = {"video": False, "apps": True, "text": True, "snapshot": False, "pair": True, "volume_set": True}
class FakeRoku(Fake):
    type = "roku"; caps = {"video": False, "apps": True, "text": True, "snapshot": False, "pair": False}

server.DRIVERS.update(android=FakeAndroid, webos=FakeWebos, roku=FakeRoku)
server._webos = object()

async def fake_scan(cidrs, known=()):
    await asyncio.sleep(1.8)
    f = lambda ip, kind, label, name, model, typ, port, ports, note="": dict(host=ip, kind=kind, label=label, name=name, model=model, type=typ, port=port,
        addable=bool(typ), note=note, added=ip in known, ports=ports)
    return {"found": [f("192.168.1.20", "android", "Android TV / Fire TV", "Salón", "Google TV Streamer", "android", 5555, [5555]),
                      f("192.168.1.31", "webos", "LG webOS", "Dormitorio", "LG OLED C3", "webos", 3001, [3000, 3001]),
                      f("192.168.1.52", "sony", "Sony Bravia", "BRAVIA 55 X80K", "KD-55X80K", "sony", 80, [80]),
                      f("192.168.1.61", "samsung", "Samsung Tizen", "Samsung 7 Series", "UE50AU7105", "samsung", 8002, [8001, 8002])],
            "scanned": 254, "networks": ["192.168.1.0/24"], "ssdp": 4, "seconds": 3.4}
import types
sys.modules["discover"] = types.SimpleNamespace(scan=fake_scan, suggest=lambda: ["192.168.1.0/24"])

TVS = [dict(id="salon01", name="Salón", type="android", host="192.168.1.20", port=5555, _model="Google TV Streamer", _app="Netflix"),
       dict(id="dorm001", name="Dormitorio", type="webos", host="192.168.1.31", port=3001, _model="LG OLED C3", _app="YouTube", _vol=18),
       dict(id="cocina01", name="Cocina", type="roku", host="192.168.1.44", port=8060, _model="Roku Express", _app="Prime Video"),
       dict(id="despa001", name="Despacho", type="android", host="192.168.1.25", port=5555, _model="Fire TV Stick 4K", _app="Disney+")]
os.makedirs(DATA, exist_ok=True)
json.dump(TVS, open(os.path.join(DATA, "tvs.json"), "w"))

async def boot(app):
    # first administrator without the setup-code dance
    await server.SEC.add_user("demo", "demo-password-123", "admin")
app = server.make_app(); app.on_startup.append(boot)
if __name__ == "__main__":
    server.web.run_app(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8099")), print=None)
