"""Tests for livecore.rules.match_rule."""

from __future__ import annotations

from livecore.rules import match_rule
from livecore.types import GiftInfo, LiveEvent, LiveUser


def _ev(**kw) -> LiveEvent:
    base = dict(
        id="x", ts=0.0, kind="danmaku", room_id=1,
        user=LiveUser(uid=1, name="u"),
        text="hello",
    )
    base.update(kw)
    return LiveEvent(**base)


def test_skips_passive_kinds():
    for kind in ("enter", "like", "popularity", "system"):
        assert match_rule(_ev(kind=kind)) is None


def test_negative_sentiment_downgrades():
    ev = _ev(text="主播太差了", sentiment="negative")
    out = match_rule(ev)
    assert out is not None
    text, reason = out
    assert reason == "负向情绪降级"
    assert text in {"主播辛苦了", "慢慢来就好", "今晚听个响"}


def test_gift_event_suggests_thanks():
    ev = _ev(kind="gift", gift=GiftInfo(name="辣条", num=1, price=100), sentiment="positive")
    out = match_rule(ev)
    assert out is not None
    text, reason = out
    assert reason == "礼物/上舰事件"
    assert text in {"感谢老板", "这波太顶了", "谢谢投喂"}


def test_keyword_match_666():
    ev = _ev(text="66666 牛啊")
    out = match_rule(ev)
    assert out is not None
    _, reason = out
    assert reason == "气氛弹幕"


def test_short_danmaku_can_match_random():
    # probabilistic; over 50 trials we should see at least one match
    matched = False
    for _ in range(50):
        ev = _ev(text="hi")
        out = match_rule(ev)
        if out is not None:
            text, reason = out
            assert text in {"哈哈", "确实", "收到"}
            assert reason == "短弹幕附和"
            matched = True
            break
    assert matched, "expected at least one random short-danmaku match in 50 trials"
