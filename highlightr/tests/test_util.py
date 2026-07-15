"""Tests for timestamp helpers in util.py."""

from __future__ import annotations

import pytest

from util import _format_seconds, _parse_timestamp_to_seconds


def test_parse_plain_seconds():
    assert _parse_timestamp_to_seconds("12.345") == 12.345
    assert _parse_timestamp_to_seconds("0") == 0.0


def test_parse_mm_ss():
    assert _parse_timestamp_to_seconds("01:30") == 90.0


def test_parse_hh_mm_ss_with_millis():
    assert _parse_timestamp_to_seconds("01:02:03.5") == 3723.5


def test_parse_strips_whitespace():
    assert _parse_timestamp_to_seconds("  00:10  ") == 10.0


def test_parse_rejects_too_many_parts():
    with pytest.raises(ValueError):
        _parse_timestamp_to_seconds("01:02:03:04")


def test_format_seconds_trims_trailing_zeros():
    assert _format_seconds(12.0) == "12"
    assert _format_seconds(12.5) == "12.5"


def test_format_seconds_keeps_precision():
    assert _format_seconds(1.234567) == "1.234567"


def test_parse_and_format_roundtrip():
    seconds = _parse_timestamp_to_seconds("00:01:05")
    assert seconds == 65.0
    assert _format_seconds(seconds) == "65"
