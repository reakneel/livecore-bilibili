"""Tests for livecore.bili_http."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

from livecore.bili_http import BiliHttpError, HttpConfig, _WbiKeys, _WBI_SIGNER, _sign_wbi, fetch_danmu_endpoint
from livecore.types import DanmuEndpoint


class _FakeResp:
    def __init__(self, payload: dict, *, ok: bool = True) -> None:
        self._payload = payload
        self._ok = ok

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self) -> None:
        if not self._ok:
            raise RuntimeError("bad status")

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResp(self._responses[len(self.calls) - 1])


@pytest.fixture(autouse=True)
def reset_wbi_cache():
    _WBI_SIGNER.invalidate()
    yield
    _WBI_SIGNER.invalidate()


def test_sign_wbi_matches_reference_algorithm():
    keys = _WbiKeys(
        img_key="653657f524a547ac981ded72ea172057",
        sub_key="6e4909c702f846728e64f6007736a338",
        fetched_at=0,
    )
    signed = _sign_wbi({"foo": "114", "bar": "514", "baz": 1919810}, keys, now=1702204169)
    assert signed == {
        "bar": "514",
        "baz": "1919810",
        "foo": "114",
        "wts": "1702204169",
        "w_rid": "d3cbd2a2316089117134038bf4caf442",
    }


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_uses_exact_signed_query(monkeypatch):
    fake_session = _FakeSession([
        {
            "code": 0,
            "data": {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/653657f524a547ac981ded72ea172057.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/6e4909c702f846728e64f6007736a338.png",
                }
            },
        },
        {"code": 0, "data": {"host_list": [{"host": "danmu.example", "wss_port": 2245}], "token": "abcd"}},
        {"code": 0, "data": {"room_id": 12345}},
    ])
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **kw: fake_session)
    ep = await fetch_danmu_endpoint(12345, config=HttpConfig(total_timeout_sec=3, connect_timeout_sec=1))
    assert isinstance(ep, DanmuEndpoint)
    assert ep.host == "danmu.example"
    assert ep.wss_port == 2245
    assert ep.token == "abcd"
    assert ep.room_id == 12345

    query = parse_qs(urlsplit(fake_session.calls[1][0]).query)
    assert query["id"] == ["12345"]
    assert query["type"] == ["0"]
    assert len(query["wts"][0]) == 10
    assert len(query["w_rid"][0]) == 32


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_refreshes_key_on_wbi_mismatch(monkeypatch):
    key1 = "653657f524a547ac981ded72ea172057"
    sub1 = "6e4909c702f846728e64f6007736a338"
    key2 = "0123456789abcdef0123456789abcdef"
    sub2 = "fedcba9876543210fedcba9876543210"
    fake_session = _FakeSession([
        {"code": 0, "data": {"wbi_img": {"img_url": f"https://i0.hdslb.com/{key1}.png", "sub_url": f"https://i0.hdslb.com/{sub1}.png"}}},
        {"code": -352, "message": "-352", "data": None},
        {"code": 0, "data": {"wbi_img": {"img_url": f"https://i0.hdslb.com/{key2}.png", "sub_url": f"https://i0.hdslb.com/{sub2}.png"}}},
        {"code": 0, "data": {"host_list": [], "token": ""}},
        {"code": 0, "data": {}},
    ])
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **kw: fake_session)
    ep = await fetch_danmu_endpoint(1996443760)
    assert ep.host == "broadcastlv.chat.bilibili.com"
    assert len(fake_session.calls) == 5
    assert fake_session.calls[0][0].endswith("/nav")
    assert fake_session.calls[2][0].endswith("/nav")


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_accepts_empty_token_for_guest(monkeypatch):
    fake_session = _FakeSession([
        {"code": 0, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/a/653657f524a547ac981ded72ea172057.png", "sub_url": "https://i0.hdslb.com/a/6e4909c702f846728e64f6007736a338.png"}}},
        {"code": 0, "data": {"host_list": [], "token": ""}},
        {"code": 0, "data": {}},
    ])
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **kw: fake_session)
    ep = await fetch_danmu_endpoint(1)
    assert ep.token == ""
    assert ep.host == "broadcastlv.chat.bilibili.com"


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_requires_token_when_authenticated(monkeypatch):
    fake_session = _FakeSession([
        {"code": 0, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/a/653657f524a547ac981ded72ea172057.png", "sub_url": "https://i0.hdslb.com/a/6e4909c702f846728e64f6007736a338.png"}}},
        {"code": 0, "data": {"host_list": [], "token": ""}},
    ])
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **kw: fake_session)
    with pytest.raises(BiliHttpError, match="token required"):
        await fetch_danmu_endpoint(1, config=HttpConfig(require_token=True))


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_rejects_api_error_after_retry(monkeypatch):
    fake_session = _FakeSession([
        {"code": 0, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/a/653657f524a547ac981ded72ea172057.png", "sub_url": "https://i0.hdslb.com/a/6e4909c702f846728e64f6007736a338.png"}}},
        {"code": -352, "message": "-352", "data": None},
        {"code": 0, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/a/0123456789abcdef0123456789abcdef.png", "sub_url": "https://i0.hdslb.com/a/fedcba9876543210fedcba9876543210.png"}}},
        {"code": -352, "message": "-352", "data": None},
    ])
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **kw: fake_session)
    with pytest.raises(BiliHttpError, match="api code=-352"):
        await fetch_danmu_endpoint(1)


def test_fetch_danmu_endpoint_rejects_invalid_room_id():
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(fetch_danmu_endpoint(0))
