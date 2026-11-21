from __future__ import annotations

import re

from .models import TranscriptRow

TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
)
TAG_RE = re.compile(r"<[^>]+>")


def parse_srt(text: str) -> list[TranscriptRow]:
    rows: list[TranscriptRow] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return rows

    for block in re.split(r"\n{2,}", normalized):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        time_line_index = next(
            (index for index, line in enumerate(lines) if TIME_RANGE_RE.search(line)),
            None,
        )
        if time_line_index is None:
            continue

        match = TIME_RANGE_RE.search(lines[time_line_index])
        if match is None:
            continue

        caption_lines = lines[time_line_index + 1 :]
        caption_text = _clean_caption_text(" ".join(caption_lines))
        if not caption_text:
            continue

        rows.append(
            TranscriptRow(
                id=len(rows),
                start_seconds=_parse_srt_timestamp(match.group("start")),
                end_seconds=_parse_srt_timestamp(match.group("end")),
                speaker=None,
                text=caption_text,
            )
        )

    return rows


def decode_srt_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _parse_srt_timestamp(value: str) -> float:
    hours_text, minutes_text, seconds_text = value.replace(",", ".").split(":")
    seconds_part, millis_part = seconds_text.split(".")
    millis = int(millis_part.ljust(3, "0")[:3])
    return (
        int(hours_text) * 3600
        + int(minutes_text) * 60
        + int(seconds_part)
        + millis / 1000.0
    )


def _clean_caption_text(text: str) -> str:
    without_tags = TAG_RE.sub("", text)
    return re.sub(r"\s+", " ", without_tags).strip()

