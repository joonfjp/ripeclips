#!/usr/bin/env python3
"""Splice MP4 highlight clips from a saved Ripe Clips JSON result.

Example:
    python3 highlightr/scripts/splice_highlights.py highlightr/output/episode_result.json

To also write shifted, wrapped SRT files next to each clip:
    python3 highlightr/scripts/splice_highlights.py highlightr/output/episode_result.json --write-srt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from util import trim_mp4_exact  # noqa: E402

DEFAULT_INPUT_ROOT = Path(
    os.environ.get("HIGHLIGHTS_INPUT_ROOT", Path.cwd() / "input")
)
TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
)


@dataclass(slots=True)
class Subtitle:
    start: float
    end: float
    text: list[str]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Splice highlight MP4 clips from a v2 highlight JSON file."
    )
    parser.add_argument("json_path", type=Path, help="Path to highlight result JSON.")
    parser.add_argument(
        "--mp4",
        type=Path,
        default=None,
        help="Override source MP4 path. Defaults to _local.mp4_path or SRT basename.",
    )
    parser.add_argument(
        "--write-srt",
        action="store_true",
        help="Write shifted SRT files next to the MP4 clips.",
    )
    parser.add_argument(
        "--srt-source",
        "--srt",
        dest="srt_source",
        type=Path,
        default=None,
        help="Override source SRT path. Defaults to _local.srt_path or MP4 basename.",
    )
    parser.add_argument(
        "--srt-max-lines",
        type=int,
        default=2,
        help="Maximum text rows per shifted subtitle cue. Default: 2.",
    )
    parser.add_argument(
        "--srt-max-words-per-line",
        "--srt-max-words-per-sentence",
        dest="srt_max_words_per_line",
        type=int,
        default=8,
        help="Maximum words per subtitle row. Default: 8.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory. Defaults to <source_mp4_stem>_clips next to the MP4.",
    )
    parser.add_argument(
        "--input-root",
        type=Path,
        default=DEFAULT_INPUT_ROOT,
        help="Fallback input root for older JSON without _local paths.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only splice the first N clips.",
    )
    args = parser.parse_args()

    if not args.json_path.is_file():
        print(f"JSON file not found: {args.json_path}", file=sys.stderr)
        return 2
    if args.srt_max_lines <= 0:
        print("--srt-max-lines must be positive.", file=sys.stderr)
        return 2
    if args.srt_max_words_per_line <= 0:
        print("--srt-max-words-per-line must be positive.", file=sys.stderr)
        return 2

    payload = json.loads(args.json_path.read_text(encoding="utf-8"))
    mp4_path = resolve_mp4_path(payload, args)
    if not mp4_path.is_file():
        print(f"MP4 file not found: {mp4_path}", file=sys.stderr)
        return 2
    srt_path = resolve_srt_path(payload, args, mp4_path) if args.write_srt else None
    subtitles = []
    if srt_path is not None:
        if not srt_path.is_file():
            print(f"SRT file not found: {srt_path}", file=sys.stderr)
            return 2
        subtitles = parse_srt(srt_path.read_text(encoding="utf-8-sig"))

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else mp4_path.with_name(f"{mp4_path.stem}_clips")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    clips = payload.get("clips", [])
    if args.limit is not None:
        clips = clips[: args.limit]

    if not clips:
        print("No clips found in JSON.", file=sys.stderr)
        return 1

    for clip in clips:
        rank = int(clip["rank"])
        title = slugify(str(clip.get("title") or f"clip_{rank:03d}"))
        output_path = output_dir / f"{rank:03d}_{title}.mp4"
        start_seconds = float(clip["start_seconds"])
        end_seconds = float(clip["end_seconds"])
        start = str(start_seconds)
        end = str(end_seconds)

        print(f"{rank:03d}: {start}s -> {end}s {output_path}")
        trim_mp4_exact(mp4_path, output_path, start, end)
        if subtitles:
            srt_output_path = output_path.with_suffix(".srt")
            write_shifted_srt(
                subtitles,
                srt_output_path,
                clip_start=start_seconds,
                clip_end=end_seconds,
                max_lines=args.srt_max_lines,
                max_words_per_line=args.srt_max_words_per_line,
            )

    print(f"wrote {len(clips)} clips to {output_dir}")
    return 0


def resolve_mp4_path(payload: dict, args: argparse.Namespace) -> Path:
    if args.mp4 is not None:
        return args.mp4.expanduser().resolve()

    local = payload.get("_local") if isinstance(payload.get("_local"), dict) else {}
    mp4_path = local.get("mp4_path")
    if mp4_path:
        return Path(mp4_path).expanduser().resolve()

    srt_path = local.get("srt_path")
    if srt_path:
        return Path(srt_path).expanduser().resolve().with_suffix(".mp4")

    source_id = payload.get("source_id")
    if source_id:
        input_root = args.input_root.expanduser().resolve()
        candidates = [
            input_root / str(source_id) / f"{source_id}.mp4",
            input_root / "_done" / str(source_id) / f"{source_id}.mp4",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate

    raise SystemExit(
        "Could not determine MP4 path. Re-run test_identify_srt.py or pass --mp4."
    )


def resolve_srt_path(payload: dict, args: argparse.Namespace, mp4_path: Path) -> Path:
    if args.srt_source is not None:
        return args.srt_source.expanduser().resolve()

    local = payload.get("_local") if isinstance(payload.get("_local"), dict) else {}
    srt_path = local.get("srt_path")
    if srt_path:
        return Path(srt_path).expanduser().resolve()

    return mp4_path.with_suffix(".srt")


def parse_srt(text: str) -> list[Subtitle]:
    subtitles: list[Subtitle] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return subtitles

    for block in re.split(r"\n{2,}", normalized):
        lines = [line.rstrip() for line in block.split("\n")]
        time_index = next(
            (index for index, line in enumerate(lines) if TIME_RANGE_RE.search(line)),
            None,
        )
        if time_index is None:
            continue

        match = TIME_RANGE_RE.search(lines[time_index])
        if match is None:
            continue

        body = [line for line in lines[time_index + 1 :] if line.strip()]
        if not body:
            continue

        subtitles.append(
            Subtitle(
                start=parse_srt_timestamp(match.group("start")),
                end=parse_srt_timestamp(match.group("end")),
                text=body,
            )
        )

    return subtitles


def write_shifted_srt(
    subtitles: list[Subtitle],
    output_path: Path,
    *,
    clip_start: float,
    clip_end: float,
    max_lines: int = 2,
    max_words_per_line: int = 8,
) -> None:
    shifted_blocks: list[str] = []
    index = 1

    for subtitle in subtitles:
        if subtitle.end <= clip_start or subtitle.start >= clip_end:
            continue

        start = max(subtitle.start, clip_start) - clip_start
        end = min(subtitle.end, clip_end) - clip_start
        if end <= start:
            continue

        text_chunks = split_subtitle_text(
            subtitle.text,
            max_lines=max_lines,
            max_words_per_line=max_words_per_line,
        )
        if not text_chunks:
            continue

        duration = end - start
        total_words = sum(count_words(lines) for lines in text_chunks) or len(text_chunks)
        current = start

        for chunk_index, lines in enumerate(text_chunks):
            if chunk_index == len(text_chunks) - 1:
                chunk_end = end
            else:
                weight = count_words(lines) or 1
                chunk_end = min(end, current + duration * (weight / total_words))

            if chunk_end <= current:
                continue

            shifted_blocks.append(
                "\n".join(
                    [
                        str(index),
                        f"{format_srt_timestamp(current)} --> {format_srt_timestamp(chunk_end)}",
                        *lines,
                    ]
                )
            )
            index += 1
            current = chunk_end

    output_path.write_text("\n\n".join(shifted_blocks) + "\n", encoding="utf-8")


def split_subtitle_text(
    lines: list[str],
    *,
    max_lines: int,
    max_words_per_line: int,
) -> list[list[str]]:
    words = " ".join(line.strip() for line in lines if line.strip()).split()
    if not words:
        return []

    max_words_per_cue = max_lines * max_words_per_line
    chunks: list[list[str]] = []

    for start in range(0, len(words), max_words_per_cue):
        cue_words = words[start : start + max_words_per_cue]
        chunks.append(wrap_words(cue_words, max_words_per_line))

    return chunks


def wrap_words(words: list[str], max_words_per_line: int) -> list[str]:
    return [
        " ".join(words[start : start + max_words_per_line])
        for start in range(0, len(words), max_words_per_line)
    ]


def count_words(lines: list[str]) -> int:
    return sum(len(line.split()) for line in lines)


def parse_srt_timestamp(value: str) -> float:
    hours_text, minutes_text, seconds_text = value.replace(",", ".").split(":")
    seconds_part, millis_part = seconds_text.split(".")
    millis = int(millis_part.ljust(3, "0")[:3])
    return (
        int(hours_text) * 3600
        + int(minutes_text) * 60
        + int(seconds_part)
        + millis / 1000.0
    )


def format_srt_timestamp(value: float) -> str:
    millis_total = max(0, int(round(value * 1000)))
    hours, remainder = divmod(millis_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return slug[:80] or "highlight"


if __name__ == "__main__":
    raise SystemExit(main())
