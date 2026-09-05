from __future__ import annotations

import random

BLOCKED = ("加微信", "加qq", "http://", "https://", "www.", "免费领", "刷礼物")
STICKERS = ("", "", "～", "。")


def postprocess_reply(text: str, max_len: int) -> str | None:
    t = " ".join(text.split()).strip().strip("\"「」『』")
    t = t.replace("#", "").replace("@", "")
    if not t:
        return None
    lower = t.lower()
    if any(w in lower for w in BLOCKED):
        return None
    if len(t) > max_len:
        t = t[:max_len]
    if random.random() < 0.22:
        t += random.choice(STICKERS)
    return t
