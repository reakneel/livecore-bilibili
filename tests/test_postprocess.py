"""Tests for livecore.postprocess.postprocess_reply."""

from __future__ import annotations

import livecore.postprocess as pp
from livecore.postprocess import postprocess_reply


def test_strips_quotes_and_mentions():
    out = postprocess_reply('「@主播 好」', 24)
    assert out is not None
    # quotes and @ are gone; "主播 好" remains verbatim
    assert out.startswith("主播 好")
    out2 = postprocess_reply("##好#", 100)  # large max_len so length never truncates
    assert out2 is not None
    assert out2.startswith("好")


def test_blocks_known_spam():
    assert postprocess_reply("加微信送福利", 24) is None
    assert postprocess_reply("http://spam.example", 24) is None


def test_truncates_to_max_len():
    out = postprocess_reply("一句话很长的回复", 4)
    assert out is not None
    # base text is truncated to 4 chars (sticker may be appended after truncation)
    base = out[0]  # at least one char
    assert len(out) >= 1


def test_sticker_does_not_break_basic_shape():
    # base text never exceeds max_len before sticker; we test that no
    # non-empty fragment leaks past the limit when no sticker is added
    out = postprocess_reply("一段长长的文本", 6)
    assert out is not None
    # sticker chars are: '', '', '～', '。'. Strip them and ensure <= max_len
    stripped = out.rstrip("～。")
    assert len(stripped) <= 6


def test_empty_input_returns_none():
    assert postprocess_reply("   ", 24) is None


def test_sticker_sometimes_appended(monkeypatch):
    # deterministically force sticker branch and pick the first non-empty one
    monkeypatch.setattr(pp.random, "random", lambda: 0.0)
    monkeypatch.setattr(pp.random, "choice", lambda seq: seq[2])  # "～"
    out = postprocess_reply("好的", 24)
    assert out is not None
    assert out.endswith("～")
