"""Bilibili HTTP handshake helpers with validation and bounded timeouts."""

from __future__ import annotations

from dataclasses import dataclass

from .types import DanmuEndpoint

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_HOST = "broadcastlv.chat.bilibili.com"


@dataclass(frozen=True, slots=True)
class HttpConfig:
    total_timeout_sec: float = 10.0
    connect_timeout_sec: float = 5.0
    # Guest mode is valid when Bilibili returns an empty token. Set this to
    # True when the caller explicitly requires authenticated access.
    require_token: bool = False


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


async def fetch_danmu_endpoint(room_id: int, *, config: HttpConfig | None = None) -> DanmuEndpoint:
    """Resolve a numeric room id into a usable danmaku WebSocket endpoint.

    Empty tokens are intentionally accepted for guest/unauthenticated mode.
    Authentication becomes strict only when ``HttpConfig.require_token`` is
    enabled; the WebSocket auth reply remains the final server-side check.
    """
    if room_id <= 0:
        raise ValueError("room_id must be positive")

    import aiohttp

    cfg = config or HttpConfig()
    timeout = aiohttp.ClientTimeout(total=cfg.total_timeout_sec, connect=cfg.connect_timeout_sec)
    headers = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}
    url = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
    info_url = "https://api.live.bilibili.com/room/v1/Room/get_info"

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(url, params={"id": room_id, "type": 0}) as resp:
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
            async with session.get(info_url, params={"room_id": room_id}) as resp:
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
