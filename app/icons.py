"""App logos for the remote.

Known streaming brands get their logo from the Simple Icons set (https://simpleicons.org, CC0), downloaded the first
time it is needed and cached in the data volume: nothing is bundled in this repository. TVHub keeps only the glyph's
path data and draws it itself on a tile in the brand colour, so no foreign markup ever reaches the browser.
LG webOS TVs also hand out the icon of every app they list; those are proxied from the TV (see `fetch_remote`).
Anything else falls back to the coloured initials the UI already draws.
"""
import asyncio
import ipaddress
import os
import re
import time

import aiohttp

CDN = os.environ.get("ICON_CDN", "https://cdn.jsdelivr.net/npm").rstrip("/")
# Several releases of the set: brands come and go between versions (Prime Video left after v13, HBO Max arrived later).
VERSIONS = ("13.21.0", "16.36.0", "9.21.0")
SLUG_RE = re.compile(r"^[a-z0-9]{2,40}$")
PATH_RE = re.compile(r'<path[^>]*\sd="([MmLlHhVvCcSsQqTtAaZz0-9 ,.\-eE]{10,6000})"')
MISS_TTL = 3600                                  # do not ask the CDN again for a missing icon within an hour

# (regex on the lower-cased package / app id / app name, simple-icons slug, tile colour)
BRANDS = [
    (r"netflix", "netflix", "E50914"),
    (r"youtube\.tv|youtubetv", "youtubetv", "FF0000"),
    (r"youtube", "youtube", "FF0000"),
    (r"cloud9|\bsilk\b", "amazon", "FF9900"),
    (r"amazon\.avod|primevideo|prime\.video|aiv\b|^amazon$|amazon\.alexa\.tv", "primevideo", "00A8E1"),
    (r"spotify", "spotify", "1DB954"),
    (r"plexapp|\bplex\b", "plex", "EBAF00"),
    (r"wbd\.stream|hbo|\bmax\b", "hbomax", "5822B4"),
    (r"twitch", "twitch", "9146FF"),
    (r"apple\.atve|appletv|apple\.tv", "appletv", "1C1C1E"),
    (r"kodi|xbmc", "kodi", "17B2E7"),
    (r"videolan|\bvlc\b", "vlcmediaplayer", "FF8800"),
    (r"crunchyroll", "crunchyroll", "F47521"),
    (r"paramount|cbs", "paramountplus", "0064FF"),
    (r"\bhulu\b|hulu\.", "hulu", "1CE783"),
    (r"tubi", "tubi", "7408FF"),
    (r"jellyfin", "jellyfin", "00A4DC"),
    (r"\bemby\b|emby\.", "emby", "52B54B"),
    (r"stremio", "stremio", "685CEE"),
    (r"dazn", "dazn", "0C0C0C"),
    (r"vimeo", "vimeo", "1AB7EA"),
    (r"tidal", "tidal", "0A0A0A"),
    (r"deezer", "deezer", "A238FF"),
    (r"soundcloud", "soundcloud", "FF5500"),
    (r"rakuten", "rakuten", "BF0000"),
    (r"music\.apple|applemusic", "applemusic", "FA243C"),
    (r"facebook", "facebook", "0866FF"),
    (r"dropbox", "dropbox", "0061FF"),
    (r"kick\.com|\bkick\b", "kick", "53FC19"),
    (r"android\.vending|googleplay", "googleplay", "414141"),
    (r"chrome", "googlechrome", "4285F4"),
]
BRANDS = [(re.compile(p), s, c) for p, s, c in BRANDS]
HDMI_RE = re.compile(r"hdmi[ _\-]?(\d)")

_miss = {}


def brand_for(*names):
    """(slug, colour) of the brand an app id / name belongs to, or None."""
    for n in names:
        n = (n or "").lower()
        for rx, slug, colour in BRANDS:
            if rx.search(n):
                return slug, colour
    return None


def _light(hexcolour):
    r, g, b = (int(hexcolour[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) > 170


def tile(path_d, colour):
    fg = "#111" if _light(colour) else "#fff"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#{colour}"/>'
            f'<svg x="13" y="13" width="38" height="38" viewBox="0 0 24 24" fill="{fg}"><path d="{path_d}"/></svg></svg>').encode()


def hdmi_tile(n):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#2b3140"/>'
            f'<text x="32" y="29" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" font-weight="700" font-size="15" fill="#fff">HDMI</text>'
            f'<text x="32" y="50" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" font-weight="700" font-size="20" fill="#6ea8ff">{int(n)}</text></svg>').encode()


async def _download(session, slug):
    for v in VERSIONS:
        try:
            async with session.get(f"{CDN}/simple-icons@{v}/icons/{slug}.svg") as r:
                if r.status != 200:
                    continue
                m = PATH_RE.search((await r.content.read(60000)).decode("utf-8", "replace"))
                if m:
                    return m.group(1)
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError):
            continue
    return None


async def brand_tile(cache_dir, slug, colour):
    """SVG tile for a brand, from the cache or the CDN. None when the set does not have it."""
    if not SLUG_RE.match(slug) or not re.fullmatch(r"[0-9A-F]{6}", colour):
        return None
    path = os.path.join(cache_dir, f"{slug}.d")
    try:
        with open(path) as f:
            return tile(f.read(), colour)
    except OSError:
        pass
    if time.time() - _miss.get(slug, 0) < MISS_TTL:
        return None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
        d = await _download(s, slug)
    if not d:
        _miss[slug] = time.time()
        return None
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w") as f:
            f.write(d)
    except OSError:
        pass
    return tile(d, colour)


def _public_or_same_host(url, tv_host):
    """The TV only ever hands out its own or public CDN URLs; refuse anything pointing at another private address."""
    m = re.match(r"^https?://([^/:@\s]+)(?::\d+)?/", url)
    if not m:
        return False
    host = m.group(1)
    if host == tv_host:
        return True
    try:
        return not (ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return True                                     # a hostname (e.g. lgsiapp.lgappstv.com)


async def fetch_remote(url, tv_host):
    """Download an icon that the TV itself lists. Returns (bytes, content_type) or None."""
    if not isinstance(url, str) or not _public_or_same_host(url, tv_host):
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            async with s.get(url, allow_redirects=False) as r:
                ct = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if r.status != 200 or ct not in ("image/png", "image/jpeg", "image/webp"):
                    return None
                body = await r.content.read(600_000)
                return (body, ct) if body else None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
