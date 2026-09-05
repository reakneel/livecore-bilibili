"""Tests for livecore.bili_http."""

from __future__ import annotations

import asyncio

import pytest

from livecore.bili_http import BiliHttpError, HttpConfig, fetch_danmu_endpoint
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


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_parses_response(monkeypatch):
    fake_session = _FakeSession([
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
    assert fake_session.calls[0][1]["params"] == {"id": 12345, "type": 0}


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_falls_back_to_default_host(monkeypatch):
    fake_session = _FakeSession([
        {"code": 0, "data": {"token": "abcd"}},
        {"code": 0, "data": {}},
    ])
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **kw: fake_session)
    ep = await fetch_danmu_endpoint(1)
    assert ep.host == "broadcastlv.chat.bilibili.com"
    assert ep.wss_port == 443
    assert ep.token == "abcd"


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_rejects_api_error(monkeypatch):
    fake_session = _FakeSession([{"code": -400, "message": "bad room", "data": None}])
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **kw: fake_session)
    with pytest.raises(BiliHttpError, match="api code=-400"):
        await fetch_danmu_endpoint(1)


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_rejects_empty_token(monkeypatch):
    fake_session = _FakeSession([{"code": 0, "data": {"host_list": []}}])
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **kw: fake_session)
    with pytest.raises(BiliHttpError, match="empty token"):
        await fetch_danmu_endpoint(1)


def test_fetch_danmu_endpoint_rejects_invalid_room_id():
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(fetch_danmu_endpoint(0))
