"""Tests for livecore.bili_http."""

from __future__ import annotations

import json

import pytest

from livecore.bili_http import fetch_danmu_endpoint
from livecore.types import DanmuEndpoint


class _FakeResp:
    def __init__(self, payload: dict, *, ok: bool = True) -> None:
        self._payload = payload
        self._ok = ok

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def ok(self) -> bool:
        return self._ok

    def raise_for_status(self) -> None:
        if not self._ok:
            raise RuntimeError("bad status")

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url: str):
        self.calls.append(url)
        # round-robin
        return _FakeResp(self._responses[len(self.calls) - 1])


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_parses_response(monkeypatch):
    fake_session = _FakeSession([
        # getDanmuInfo
        {"data": {"host_list": [{"host": "danmu.example", "wss_port": 2245}], "token": "abcd"}},
        # Room/get_info (resolved short room)
        {"data": {"room_id": 12345}},
    ])

    import aiohttp  # ensure module is importable for the test
    monkeypatch.setattr(
        "aiohttp.ClientSession",
        lambda *a, **kw: fake_session,
    )
    ep = await fetch_danmu_endpoint(12345)
    assert isinstance(ep, DanmuEndpoint)
    assert ep.host == "danmu.example"
    assert ep.wss_port == 2245
    assert ep.token == "abcd"
    assert ep.room_id == 12345


@pytest.mark.asyncio
async def test_fetch_danmu_endpoint_falls_back_to_default_host(monkeypatch):
    fake_session = _FakeSession([
        {"data": {}},  # no host_list
        {"data": {}},
    ])
    monkeypatch.setattr(
        "aiohttp.ClientSession",
        lambda *a, **kw: fake_session,
    )
    ep = await fetch_danmu_endpoint(1)
    assert ep.host == "broadcastlv.chat.bilibili.com"
    assert ep.wss_port == 443
    assert ep.token == ""
