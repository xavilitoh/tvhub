"""Home Assistant add-on helpers (only used when TVHub runs as the add-on):

  * announce(): tell Home Assistant, through the Supervisor's discovery, where this TVHub is, with an API token,
    so the TVHub integration offers "TVHub discovered" and needs no IP or token typed by hand.
  * install_integration(): optionally copy the TVHub integration into Home Assistant's config (opt-in option).
"""
import asyncio
import json
import os
import shutil
from pathlib import Path

import aiohttp

TOKEN_NAME = "Home Assistant (add-on)"
MARKER = ".tvhub-addon"                  # set only on a copy we installed, so we never touch HACS / manual installs


def _sup():
    return os.environ.get("SUPERVISOR_URL", "http://supervisor").rstrip("/")


def _hdr():
    return {"Authorization": "Bearer " + os.environ.get("SUPERVISOR_TOKEN", "")}


def options(data_dir):
    """The add-on options the user set in Home Assistant (the Supervisor writes them to /data/options.json)."""
    try:
        with open(os.path.join(data_dir, "options.json")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def ha_token(sec, data_dir):
    """A TVHub API token for the integration. Kept in /data (0600) so restarts reuse it instead of piling up tokens."""
    path = Path(data_dir) / "ha-integration-token"
    try:
        raw = path.read_text().strip()
    except OSError:
        raw = ""
    if raw and sec.api_token(raw):
        return raw
    for tid, t in list(sec.tokens.items()):               # a stale record of ours whose secret is gone
        if t.get("name") == TOKEN_NAME[:40]:
            sec.delete_api_token(tid)
    raw, _ = sec.create_api_token(TOKEN_NAME)
    tmp = str(path) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(raw)
    os.replace(tmp, path)
    return raw


async def announce(sec, data_dir, port, log, attempts=5):
    for i in range(attempts):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as cs:
                async with cs.get(_sup() + "/addons/self/info", headers=_hdr()) as r:
                    host = ((await r.json()).get("data") or {}).get("hostname") if r.status == 200 else None
                if host:
                    body = {"service": "tvhub", "config": {"host": host, "port": port, "token": ha_token(sec, data_dir)}}
                    async with cs.post(_sup() + "/discovery", headers=_hdr(), json=body) as r:
                        if r.status == 200:
                            log(f"Home Assistant: announced TVHub at {host}:{port} (the integration offers to set itself up)")
                            return True
                        log(f"Home Assistant discovery: HTTP {r.status}")
        except Exception as e:                          # noqa: BLE001 - Supervisor still starting, etc.
            log("Home Assistant discovery:", e)
        await asyncio.sleep(10 * (i + 1))
    return False


def install_integration(log, src="/opt/tvhub-integration", config="/config"):
    """Copy the integration bundled in the image into <config>/custom_components/tvhub.
    -> installed | updated | current | foreign (someone else manages it) | unavailable"""
    src, config = Path(src), Path(config)
    if not (src / "manifest.json").is_file() or not config.is_dir():
        return "unavailable"
    dst = config / "custom_components" / "tvhub"
    new = json.loads((src / "manifest.json").read_text()).get("version")
    existed = dst.exists()
    if existed:
        if not (dst / MARKER).exists():
            log("Integration already in custom_components (HACS or manual): left untouched")
            return "foreign"
        try:
            if json.loads((dst / "manifest.json").read_text()).get("version") == new:
                return "current"
        except (OSError, ValueError):
            pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("tvhub.tmp-addon")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp / MARKER).write_text(str(new))
    if existed:
        shutil.rmtree(dst)
    os.replace(tmp, dst)
    return "updated" if existed else "installed"
