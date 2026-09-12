"""Bilibili HTTP handshake helpers with validation and bounded timeouts."""

from __future__ import annotations

import asyncio
import hashlib
import time
import urllib.parse
from dataclasses import dataclass

from .types import DanmuEndpoint

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_HOST = "broadcastlv.chat.bilibili.com"
WBI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BUVID_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
GET_DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
WBI_CACHE_TTL_SEC = 2 * 60 * 60
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


@dataclass(frozen=True, slots=True)
class HttpConfig:
    total_timeout_sec: float = 10.0
    connect_timeout_sec: float = 5.0
    require_token: bool = False


@dataclass(frozen=True, slots=True)
class _WbiKeys:
    img_key: str
    sub_key: str
    fetched_at: float


class BiliHttpError(RuntimeError):
    """Raised when Bilibili returns an unusable handshake response."""


def _require_data(payload: object, endpoint: str) -> dict:
    if not isinstance(payload, dict):
        raise BiliHttpError(f"{endpoint}: response is not an object")
    code = payload.get("code")
    if code not in (None, 0):
        raise BiliHttpError(f"{endpoint}: api code={code}, message={payload.get('message', '')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BiliHttpError(f"{endpoint}: missing data")
    return data


def _require_nav_data(payload: object) -> dict:
    """Return nav data for both logged-in and anonymous callers."""
    if not isinstance(payload, dict):
        raise BiliHttpError("nav: response is not an object")
    code = payload.get("code")
    if code not in (0, -101):
        raise BiliHttpError(f"nav: api code={code}, message={payload.get('message', '')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BiliHttpError("nav: missing data")
    return data


def _extract_wbi_key(url: object, field: str) -> str:
    if not isinstance(url, str) or not url:
        raise BiliHttpError(f"nav: missing wbi_img.{field}")
    name = url.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _mixin_key(img_key: str, sub_key: str) -> str:
    original = list(img_key + sub_key)
    required = max(MIXIN_KEY_ENC_TAB) + 1
    if len(original) < required:
        raise BiliHttpError("nav: invalid WBI key length")
    return "".join(original[index] for index in MIXIN_KEY_ENC_TAB[:32])


def _sign_wbi(params: dict[str, object], keys: _WbiKeys, now: int | None = None) -> dict[str, str]:
    signed = {key: str(value) for key, value in params.items()}
    signed["wts"] = str(int(time.time()) if now is None else now)
    signed = dict(sorted(signed.items()))
    sanitized = {
        key: "".join(char for char in value if char not in "!'()*")
        for key, value in signed.items()
    }
    query = urllib.parse.urlencode(
        sanitized,
        doseq=False,
        quote_via=urllib.parse.quote,
        safe="",
    )
    mixin_key = _mixin_key(keys.img_key, keys.sub_key)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


class _WbiSigner:
    def __init__(self) -> None:
        self._keys: _WbiKeys | None = None
        self._lock = asyncio.Lock()

    async def get_keys(self, session, *, force_refresh: bool = False) -> _WbiKeys:
        now = time.monotonic()
        if not force_refresh and self._keys is not None and now - self._keys.fetched_at < WBI_CACHE_TTL_SEC:
            return self._keys
        async with self._lock:
            now = time.monotonic()
            if not force_refresh and self._keys is not None and now - self._keys.fetched_at < WBI_CACHE_TTL_SEC:
                return self._keys
            async with session.get(WBI_NAV_URL) as resp:
                resp.raise_for_status()
                data = _require_nav_data(await resp.json())
            wbi_img = data.get("wbi_img")
            if not isinstance(wbi_img, dict):
                raise BiliHttpError("nav: missing wbi_img")
            self._keys = _WbiKeys(
                img_key=_extract_wbi_key(wbi_img.get("img_url"), "img_url"),
                sub_key=_extract_wbi_key(wbi_img.get("sub_url"), "sub_url"),
                fetched_at=time.monotonic(),
            )
            return self._keys

    def invalidate(self) -> None:
        self._keys = None


_WBI_SIGNER = _WbiSigner()


async def _get_danmu_info(session, room_id: int, keys: _WbiKeys):
    signed = _sign_wbi({"id": room_id, "type": 0}, keys)
    query = urllib.parse.urlencode(signed, doseq=False, quote_via=urllib.parse.quote, safe="")
    async with session.get(f"{GET_DANMU_INFO_URL}?{query}") as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_buvid3() -> str:
    """Fetch a ``buvid3`` fingerprint value.

    ``getDanmuInfo`` has expected a buvid3 cookie since 2025-06-27. Guest sessions
    without one still work today, but the value is echoed in the auth body and
    keeps the connection out of the risk-control bucket. This helper is not called
    implicitly so the handshake keeps its exact request sequence; pass the result
    to :class:`~livecore.platforms.bilibili.BilibiliAdapter` when you want it.
    """
    import aiohttp

    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.get(BUVID_SPI_URL) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        except (aiohttp.ClientError, ValueError):
            return ""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ""
    return str(data.get("b_3") or "")


async def fetch_danmu_endpoint(room_id: int, *, config: HttpConfig | None = None) -> DanmuEndpoint:
    if room_id <= 0:
        raise ValueError("room_id must be positive")

    import aiohttp

    cfg = config or HttpConfig()
    # aiohttp uses `sock_connect`, not `connect_timeout`.
    timeout = aiohttp.ClientTimeout(total=cfg.total_timeout_sec, sock_connect=cfg.connect_timeout_sec)
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        keys = await _WBI_SIGNER.get_keys(session)
        payload = await _get_danmu_info(session, room_id, keys)
        if isinstance(payload, dict) and payload.get("code") == -352:
            _WBI_SIGNER.invalidate()
            keys = await _WBI_SIGNER.get_keys(session, force_refresh=True)
            payload = await _get_danmu_info(session, room_id, keys)

        data = _require_data(payload, "getDanmuInfo")
        token = str(data.get("token") or "")
        if cfg.require_token and not token:
            raise BiliHttpError("getDanmuInfo: token required for authenticated mode")

        hosts = data.get("host_list")
        usable = [h for h in hosts if isinstance(h, dict) and h.get("host")] if isinstance(hosts, list) else []
        host = usable[0] if usable else {"host": DEFAULT_HOST, "wss_port": 443}

        real_id = room_id
        try:
            async with session.get(ROOM_INFO_URL, params={"room_id": room_id}) as resp:
                resp.raise_for_status()
                info_data = _require_data(await resp.json(), "get_info")
                real_id = int(info_data.get("room_id") or room_id)
        except (aiohttp.ClientError, BiliHttpError, ValueError, TypeError):
            pass

    return DanmuEndpoint(
        host=str(host.get("host") or DEFAULT_HOST),
        wss_port=int(host.get("wss_port") or 443),
        token=token,
        room_id=real_id,
    )
