from __future__ import annotations

from .types import Sentiment

POSITIVE = (
    "好", "棒", "爱", "喜欢", "厉害", "绝了", "好听", "好看", "哈哈", "666",
    "牛", "感谢", "谢谢", "支持", "加油", "可爱", "稳", "太强", "yyds", "awsl",
)
NEGATIVE = (
    "差", "烂", "难听", "难看", "无聊", "垃圾", "滚", "傻", "恶心", "讨厌",
    "举报", "下播", "闭嘴", "骗", "坑", "无语",  "尴尬", "崩",
)


def analyze_sentiment(text: str) -> Sentiment:
    t = text.lower()
    score = 0
    for w in POSITIVE:
        if w in t:
            score += 1
    for w in NEGATIVE:
        if w in t:
            score -= 2
    if score > 0:
        return "positive"
    if score < 0:
        return "negative"
    return "neutral"
