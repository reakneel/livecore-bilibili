from __future__ import annotations

from .types import DanmuEndpoint

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


async def fetch_danmu_endpoint(room_id: int) -> DanmuEndpoint:
    import aiohttp

    url = f"https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo?id={room_id}&type=0"
    headers = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json()
    data = payload.get("data") or {}
    hosts = data.get("host_list") or []
    host = hosts[0] if hosts else {"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}
    real_id = room_id
    info_url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(info_url) as resp:
            if resp.ok:
                info = await resp.json()
                real_id = int((info.get("data") or {}).get("room_id") or room_id)
    return DanmuEndpoint(
        host=host.get("host") or "broadcastlv.chat.bilibili.com",
        wss_port=int(host.get("wss_port") or 443),
        token=str(data.get("token") or ""),
        room_id=real_id,
    )
