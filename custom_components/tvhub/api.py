"""Tiny client for the TVHub HTTP API."""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp


class TVHubError(Exception):
    """Cannot talk to TVHub."""


class TVHubAuthError(TVHubError):
    """TVHub needs an API token (or the token is wrong)."""


class TVHubClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: str = "", ssl: bool = False) -> None:
        self._s = session
        self._base = f"{'https' if ssl else 'http'}://{host}:{port}"
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            async with asyncio.timeout(15):
                async with self._s.request(method, self._base + path, headers=self._headers, json=body) as r:
                    if r.status in (401, 403):
                        raise TVHubAuthError("token required")
                    if r.status >= 400:
                        raise TVHubError(f"{method} {path}: HTTP {r.status}")
                    return await r.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TVHubError(str(err)) from err

    async def tvs(self) -> list[dict[str, Any]]:
        return (await self._req("GET", "/api/tvs"))["tvs"]

    async def apps(self, tv_id: str) -> list[dict[str, Any]]:
        return await self._req("GET", f"/api/tvs/{tv_id}/apps")

    async def key(self, tv_id: str, *keys: str) -> None:
        await self._req("POST", f"/api/tvs/{tv_id}/key", {"keys": list(keys)})

    async def text(self, tv_id: str, text: str) -> None:
        await self._req("POST", f"/api/tvs/{tv_id}/text", {"text": text})

    async def launch(self, tv_id: str, pkg: str) -> None:
        await self._req("POST", f"/api/tvs/{tv_id}/launch", {"pkg": pkg})

    async def volume(self, tv_id: str, level: int) -> None:
        await self._req("POST", f"/api/tvs/{tv_id}/volume", {"level": level})

    async def power(self, tv_id: str, on: bool) -> None:
        await self._req("POST", f"/api/tvs/{tv_id}/power", {"on": on})
