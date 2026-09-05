from __future__ import annotations

import random
import re

from .types import LiveEvent

CHECK_IN_LINES = ("来了来了", "打卡听一会儿", "今晚也在", "路过支持一下")
AMBIENT_LINES = ("哈哈", "这氛围可以", "好听", "稳")


def _pick(lines: tuple[str, ...] | list[str]) -> str:
    return random.choice(list(lines))


def match_rule(ev: LiveEvent) -> tuple[str, str] | None:
    if ev.kind in {"enter", "like", "popularity", "system"}:
        return None
    if ev.sentiment == "negative":
        return _pick(("主播辛苦了", "慢慢来就好", "今晚听个响")), "负向情绪降级"
    if ev.kind in {"gift", "guard", "superchat"}:
        return _pick(("感谢老板", "这波太顶了", "谢谢投喂")), "礼物/上舰事件"
    if ev.text and re.search(r"666+|牛|太强|绝了", ev.text):
        return _pick(("666", "这波稳", "好活")), "气氛弹幕"
    if ev.text and re.search(r"好听|再来一首|点歌|唱", ev.text):
        return _pick(("这段真好听", "耳膜被治愈了")), "歌曲相关"
    if ev.kind == "danmaku" and ev.text and len(ev.text) <= 12 and random.random() < 0.18:
        return _pick(("哈哈", "确实", "收到")), "短弹幕附和"
    return None
