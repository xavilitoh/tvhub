"""Find TVs on the local network without typing their IPs.

Two sources, merged:
  * SSDP (UDP multicast): works when the container shares the host network (Linux: `network_mode: host`).
    Docker Desktop on macOS / Windows does not forward multicast, so there it simply finds nothing.
  * A TCP sweep of the networks you give it: plain unicast, works everywhere, including Docker Desktop.

Every host that answers on a TV port is classified by the ports it opens and, where the TV offers it,
asked for its name over HTTP. Only private / loopback ranges are ever scanned.
"""
import asyncio
import ipaddress
import re
import socket
import time
from xml.etree import ElementTree

import aiohttp

MAX_HOSTS = 1024                        # one /22 per scan
CONNECT_TIMEOUT = 0.7
CONCURRENCY = 300
SSDP_ADDR = ("239.255.255.250", 1900)
PORTS = (5555, 3000, 3001, 36669, 8060, 8001, 8002, 8008, 8009, 6466, 6467)

# kind -> (label, driver type or None, default port, note)
KINDS = {
    "vidaa": ("Hisense VIDAA", "vidaa", 36669, ""),
    "webos": ("LG webOS", "webos", 3001, "Después de añadirla: Ajustes → Emparejar y acepta el aviso en la TV."),
    "android": ("Android TV / Fire TV", "android", 5555, "La primera vez la TV pregunta «¿Permitir depuración USB?»: acepta."),
    "androidtv-noadb": ("Android TV / Google TV (ADB apagado)", None, 0,
                        "Activa Opciones de desarrollador → Depuración ADB (o por red) y vuelve a buscar."),
    "roku": ("Roku", None, 0, "Roku todavía no está soportado por TVHub."),
    "samsung": ("Samsung", None, 0, "Samsung todavía no está soportado por TVHub."),
    "cast": ("Chromecast / Google Cast", None, 0, "Sin ADB no se puede controlar. Si es un Chromecast con Google TV, activa la depuración ADB."),
}


def parse_cidrs(items):
    """Validate the networks to scan. Returns (networks, hosts). Raises ValueError with a message for the UI."""
    nets = []
    for raw in items or []:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            net = ipaddress.ip_network(raw if "/" in raw else raw + "/24", strict=False)
        except ValueError:
            raise ValueError(f"Red no válida: {raw}")
        if net.version != 4:
            raise ValueError("Solo se admiten redes IPv4")
        if not (net.is_private or net.is_loopback):
            raise ValueError(f"{raw}: solo se exploran redes privadas (192.168.x.x, 10.x.x.x, 172.16-31.x.x)")
        if net.num_addresses > MAX_HOSTS:
            raise ValueError(f"{raw}: demasiado grande (máximo {MAX_HOSTS} direcciones, es decir una /22)")
        nets.append(net)
    if not nets:
        raise ValueError("Indica al menos una red, por ejemplo 192.168.1.0/24")
    hosts, seen = [], set()
    for net in nets:
        for ip in (net.hosts() if net.num_addresses > 2 else net):
            s = str(ip)
            if s not in seen:
                seen.add(s)
                hosts.append(s)
    if len(hosts) > 2 * MAX_HOSTS:
        raise ValueError("Demasiadas direcciones en total; explora menos redes a la vez")
    return nets, hosts


def suggest(request_host=""):
    """Networks worth proposing: the one the browser used to reach TVHub, plus this machine's own."""
    out = []

    def add(ip):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return
        if a.version == 4 and a.is_private and not a.is_loopback:
            n = str(ipaddress.ip_network(f"{ip}/24", strict=False))
            if n not in out:
                out.append(n)

    add(request_host)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))              # no packet is sent; it only picks the outgoing interface
        add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return out


async def _open(ip, port):
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT)
        w.close()
        try:
            await w.wait_closed()
        except OSError:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def _sweep(hosts):
    sem = asyncio.Semaphore(CONCURRENCY)
    found = {}

    async def one(ip, port):
        async with sem:
            if await _open(ip, port):
                found.setdefault(ip, set()).add(port)

    await asyncio.gather(*(one(ip, p) for ip in hosts for p in PORTS))
    return found


class _Ssdp(asyncio.DatagramProtocol):
    def __init__(self):
        self.replies = []

    def datagram_received(self, data, addr):
        self.replies.append((addr[0], data.decode("latin-1", "replace")))


async def _ssdp(wait=2.0):
    """Multicast M-SEARCH. Returns {ip: {"location": url, "server": str}} (empty if multicast is not available)."""
    loop = asyncio.get_running_loop()
    try:
        transport, proto = await loop.create_datagram_endpoint(_Ssdp, local_addr=("0.0.0.0", 0), family=socket.AF_INET)
    except OSError:
        return {}
    try:
        msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n").encode()
        for _ in range(2):
            try:
                transport.sendto(msg, SSDP_ADDR)
            except OSError:
                return {}
            await asyncio.sleep(wait / 2)
    finally:
        transport.close()
    out = {}
    for ip, text in proto.replies:
        hdr = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in text.split("\r\n")[1:]) if k}
        d = out.setdefault(ip, {"location": "", "server": ""})
        d["location"] = d["location"] or hdr.get("location", "")
        d["server"] = d["server"] or hdr.get("server", "")
    return out


def classify(ports):
    """Map the open ports of one host to a kind (or None when it does not look like a TV)."""
    if 36669 in ports:
        return "vidaa"
    if 3000 in ports and 3001 in ports:
        return "webos"
    if 5555 in ports:
        return "android"
    if 8060 in ports:
        return "roku"
    if 8001 in ports or 8002 in ports:
        return "samsung"
    if 6466 in ports or 6467 in ports:
        return "androidtv-noadb"
    if 8008 in ports or 8009 in ports:
        return "cast"
    return None


async def _get(session, url, as_json=False):
    try:
        async with session.get(url) as r:
            if r.status != 200:
                return None
            return await (r.json(content_type=None) if as_json else r.text())
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeDecodeError):
        return None


def _xml_text(xml, tag):
    m = re.search(rf"<{tag}>([^<]{{1,80}})</{tag}>", xml or "")
    return m.group(1).strip() if m else ""


async def _describe(session, ip, kind, ports, ssdp):
    """Best-effort friendly name and model."""
    name = model = ""
    if kind == "roku":
        xml = await _get(session, f"http://{ip}:8060/query/device-info")
        name, model = _xml_text(xml, "user-device-name") or _xml_text(xml, "friendly-device-name"), _xml_text(xml, "model-name")
    elif kind == "samsung":
        j = await _get(session, f"http://{ip}:8001/api/v2/", as_json=True)
        dev = (j or {}).get("device", {}) if isinstance(j, dict) else {}
        name, model = str(dev.get("name", ""))[:40], str(dev.get("modelName", ""))[:40]
    if not name and (8008 in ports):
        j = await _get(session, f"http://{ip}:8008/setup/eureka_info?params=name", as_json=True)
        if isinstance(j, dict):
            name = str(j.get("name", ""))[:40]
    if not name and ssdp.get(ip, {}).get("location", "").startswith(f"http://{ip}"):
        xml = await _get(session, ssdp[ip]["location"])
        name, model = name or _xml_text(xml, "friendlyName"), model or _xml_text(xml, "modelName")
    return name, model


async def scan(cidrs, known_hosts=()):
    """Run a discovery. `known_hosts`: hosts already registered in TVHub (flagged, not hidden)."""
    nets, hosts = parse_cidrs(cidrs)
    started = time.monotonic()
    ssdp_task = asyncio.create_task(_ssdp())
    ports = await _sweep(hosts)
    ssdp = await ssdp_task
    allowed = {ip for ip in ssdp if any(ipaddress.ip_address(ip) in n for n in nets)}
    extra = [ip for ip in allowed if ip not in ports]            # seen over SSDP but missed by the sweep: probe them too
    if extra:
        for ip, p in (await _sweep(extra)).items():
            ports[ip] = p
    timeout = aiohttp.ClientTimeout(total=2.5)
    results = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def build(ip, open_ports):
            kind = classify(open_ports)
            if not kind:
                return
            label, typ, port, note = KINDS[kind]
            name, model = await _describe(session, ip, kind, open_ports, ssdp)
            results.append({"host": ip, "kind": kind, "label": label, "name": name or label, "model": model,
                            "type": typ, "port": port, "addable": bool(typ), "note": note,
                            "added": ip in known_hosts, "ports": sorted(open_ports)})
        await asyncio.gather(*(build(ip, p) for ip, p in ports.items()))
    order = {"vidaa": 0, "webos": 0, "android": 0}
    results.sort(key=lambda r: (order.get(r["kind"], 1), tuple(int(x) for x in r["host"].split("."))))
    return {"found": results, "scanned": len(hosts), "networks": [str(n) for n in nets],
            "ssdp": len(ssdp), "seconds": round(time.monotonic() - started, 1)}
