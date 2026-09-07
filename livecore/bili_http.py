"""Bilibili HTTP handshake helpers with validation and bounded timeouts."""

from __future__ import annotations

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
GET_DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
WBI_CACHE_TTL_SEC = 6 * 60 * 60
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
    # Guest mode is valid when Bilibili returns an empty token. Set this to
    # True when the caller explicitly requires authenticated access.
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


def _extract_wbi_key(url: object, field: str) -> str:
    if not isinstance(url, str) or not url:
        raise BiliHttpError(f"nav: missing wbi_img.{field}")
    name = url.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _mixin_key(img_key: str, sub_key: str) -> str:
    original = img_key + sub_key
    if len(original) < max(MIXIN_KEY_ENC_TAB) + 1:
        raise BiliHttpError("nav: invalid WBI key length")
    return "".join(original[index] for index in MIXIN_KEY_ENC_TAB)[:32]


def _sign_wbi(params: dict[str, object], keys: _WbiKeys, now: int | None = None) -> dict[str, str]:
    signed = {key: str(value) for key, value in params.items()}
    signed["wts"] = str(int(time.time()) if now is None else now)
    signed = dict(sorted(signed.items()))
    signed = {
        key: "".join(char for char in value if char not in "!'()*")
        for key, value in signed.items()
    }
    query = urllib.parse.urlencode(signed, quote_via=urllib.parse.quote, safe="")
    signed["w_rid"] = hashlib.md5((query + _mixin_key(keys.img_key, keys.sub_key)).encode()).hexdigest()
    return signed


async def _get_wbi_keys(session) -> _WbiKeys:
    async with session.get(WBI_NAV_URL) as resp:
        resp.raise_for_status()
        data = _require_data(await resp.json(), "nav")
    wbi_img = data.get("wbi_img")
    if not isinstance(wbi_img, dict):
        raise BiliHttpError("nav: missing wbi_img")
    return _WbiKeys(
        img_key=_extract_wbi_key(wbi_img.get("img_url"), "img_url"),
        sub_key=_extract_wbi_key(wbi_img.get("sub_url"), "sub_url"),
        fetched_at=time.monotonic(),
    )


async def fetch_danmu_endpoint(room_id: int, *, config: HttpConfig | None = None) -> DanmuEndpoint:
    """Resolve a numeric room id into a usable danmaku WebSocket endpoint.

    Bilibili now requires WBI-signed parameters for ``getDanmuInfo``. The
    signing keys are fetched from the public navigation endpoint and cached
    for a short period. Empty tokens remain valid for guest mode.
    """
    if room_id <= 0:
        raise ValueError("room_id must be positive")

    import aiohttp

    cfg = config or HttpConfig()
    timeout = aiohttp.ClientTimeout(total=cfg.total_timeout_sec, connect=cfg.connect_timeout_sec)
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        keys = await _get_wbi_keys(session)
        params = _sign_wbi({"id": room_id, "type": 0}, keys)
        async with session.get(GET_DANMU_INFO_URL, params=params) as resp:
            resp.raise_for_status()
            data = _require_data(await resp.json(), "getDanmuInfo")

        token = str(data.get("token") or "")
        if cfg.require_token and not token:
            raise BiliHttpError("getDanmuInfo: token required for authenticated mode")

        hosts = data.get("host_list")
        usable = [h for h in hosts if isinstance(h, dict) and h.get("host")] if isinstance(hosts, list) else []
        host = usable[0] if usable else {"host": DEFAULT_HOST, "wss_port": 443}

        # Short-room resolution is useful but must not make a valid handshake
        # fail because the metadata endpoint is transient.
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
