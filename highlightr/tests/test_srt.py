"""Tests for SRT parsing and decoding (app/srt.py)."""

from __future__ import annotations

from app.srt import decode_srt_bytes, parse_srt

SAMPLE_SRT = """1
00:00:01,000 --> 00:00:04,500
Hello and welcome to the show.

2
00:00:04,500 --> 00:00:09,250
Today we're talking about <i>highlights</i>.
"""


def test_parse_srt_basic_blocks():
    rows = parse_srt(SAMPLE_SRT)
    assert len(rows) == 2

    first = rows[0]
    assert first.id == 0
    assert first.start_seconds == 1.0
    assert first.end_seconds == 4.5
    assert first.text == "Hello and welcome to the show."
    assert first.speaker is None

    second = rows[1]
    assert second.id == 1
    assert second.start_seconds == 4.5
    assert second.end_seconds == 9.25


def test_parse_srt_strips_html_tags():
    rows = parse_srt(SAMPLE_SRT)
    # <i>...</i> tags removed, surrounding text preserved.
    assert rows[1].text == "Today we're talking about highlights."


def test_parse_srt_joins_multiline_captions():
    text = """1
00:00:00,000 --> 00:00:02,000
Line one
Line two
"""
    rows = parse_srt(text)
    assert len(rows) == 1
    assert rows[0].text == "Line one Line two"


def test_parse_srt_handles_dot_millisecond_separator():
    text = """1
00:01:02.500 --> 00:01:05.000
Dot separated millis.
"""
    rows = parse_srt(text)
    assert rows[0].start_seconds == 62.5
    assert rows[0].end_seconds == 65.0


def test_parse_srt_normalizes_crlf_line_endings():
    text = "1\r\n00:00:00,000 --> 00:00:01,000\r\nWindows line endings.\r\n"
    rows = parse_srt(text)
    assert len(rows) == 1
    assert rows[0].text == "Windows line endings."


def test_parse_srt_skips_empty_and_captionless_blocks():
    text = """1
00:00:00,000 --> 00:00:01,000

2
00:00:01,000 --> 00:00:02,000
Real caption.
"""
    rows = parse_srt(text)
    # First block has a timing line but no caption text, so it is dropped.
    assert len(rows) == 1
    assert rows[0].text == "Real caption."
    # ID is derived from output position, not the SRT index.
    assert rows[0].id == 0


def test_parse_srt_empty_input_returns_empty_list():
    assert parse_srt("") == []
    assert parse_srt("   \n\n  ") == []


def test_decode_srt_bytes_handles_utf8_bom():
    raw = "1\n00:00:00,000 --> 00:00:01,000\nCafé opens.\n".encode("utf-8-sig")
    decoded = decode_srt_bytes(raw)
    # BOM should be stripped by utf-8-sig decoding.
    assert not decoded.startswith("\ufeff")
    rows = parse_srt(decoded)
    assert rows[0].text == "Café opens."


def test_decode_srt_bytes_falls_back_to_latin1():
    # 0xE9 is 'é' in latin-1 but invalid as standalone utf-8.
    raw = b"1\n00:00:00,000 --> 00:00:01,000\nCaf\xe9.\n"
    decoded = decode_srt_bytes(raw)
    assert "Caf" in decoded
