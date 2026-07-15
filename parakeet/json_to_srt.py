#!/usr/bin/env python3
"""Convert Parakeet verbose_json output into readable SRT subtitles."""

import argparse
import json
import re
from pathlib import Path


WORD_RE = re.compile(r"\b[\w]+(?:['’][\w]+)*\b", re.UNICODE)
BREAK_RE = re.compile(r"[.!?][\"')\]]*$")


def srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def timed_words(data: dict) -> list[dict]:
    """Align subword timing fragments to words in the complete transcript."""
    fragments = data.get("words") or []
    transcript = data.get("text") or ""

    # Create one timing entry per alphanumeric character in the model tokens.
    char_times = []
    for fragment in fragments:
        start = float(fragment["start"])
        end = float(fragment["end"])
        chars = [c.casefold() for c in str(fragment.get("word", "")) if c.isalnum()]
        if not chars:
            continue
        step = (end - start) / len(chars)
        char_times.extend(
            (char, start + i * step, start + (i + 1) * step)
            for i, char in enumerate(chars)
        )

    result = []
    cursor = 0
    for index, match in enumerate(WORD_RE.finditer(transcript)):
        text = match.group()
        chars = [c.casefold() for c in text if c.isalnum()]
        if not chars:
            continue

        # The transcript and fragments originate from the same decoder, so their
        # alphanumeric character streams should match exactly.
        available = [entry[0] for entry in char_times[cursor : cursor + len(chars)]]
        if available != chars:
            raise ValueError(
                f"Could not align transcript word {text!r} at character {match.start()}"
            )

        start = char_times[cursor][1]
        end = char_times[cursor + len(chars) - 1][2]
        cursor += len(chars)

        # Preserve punctuation following the word so sentence boundaries remain.
        next_start = (
            WORD_RE.search(transcript, match.end()).start()
            if WORD_RE.search(transcript, match.end())
            else len(transcript)
        )
        suffix = transcript[match.end() : next_start].strip()
        result.append({"text": text + suffix, "start": start, "end": end})

    if cursor != len(char_times):
        raise ValueError("Timed fragments contain text not present in transcript")
    return result


def make_cues(
    words: list[dict],
    max_duration: float,
    max_words_per_line: int,
    rows: int,
    pause: float,
) -> list[dict]:
    cues = []
    current = []
    max_words_per_cue = max_words_per_line * rows

    for word in words:
        candidate = current + [word]
        duration = word["end"] - candidate[0]["start"]
        gap = word["start"] - current[-1]["end"] if current else 0

        if current and (
            duration > max_duration
            or len(candidate) > max_words_per_cue
            or gap >= pause
        ):
            cues.append(current)
            current = [word]
        else:
            current = candidate

        if BREAK_RE.search(word["text"]):
            cues.append(current)
            current = []

    if current:
        cues.append(current)

    def format_lines(cue: list[dict]) -> str:
        lines = []
        for start in range(0, len(cue), max_words_per_line):
            line = cue[start : start + max_words_per_line]
            lines.append(" ".join(word["text"] for word in line))
        return "\n".join(lines)

    return [
        {
            "start": cue[0]["start"],
            "end": max(cue[-1]["end"], cue[0]["start"] + 0.2),
            "text": format_lines(cue),
        }
        for cue in cues
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="Parakeet verbose_json file")
    parser.add_argument("output", type=Path, nargs="?", help="Output SRT file")
    parser.add_argument("--max-duration", type=float, default=4.0)
    parser.add_argument("--max-words-per-line", type=int, default=6)
    parser.add_argument("--rows", type=int, default=2)
    parser.add_argument("--pause", type=float, default=0.6)
    args = parser.parse_args()

    if args.max_words_per_line < 1:
        parser.error("--max-words-per-line must be at least 1")
    if args.rows < 1:
        parser.error("--rows must be at least 1")

    data = json.loads(args.input.read_text(encoding="utf-8"))
    cues = make_cues(
        timed_words(data),
        args.max_duration,
        args.max_words_per_line,
        args.rows,
        args.pause,
    )
    srt = "\n\n".join(
        f"{i}\n{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n{cue['text']}"
        for i, cue in enumerate(cues, 1)
    ) + "\n"

    if args.output:
        args.output.write_text(srt, encoding="utf-8")
    else:
        print(srt, end="")


if __name__ == "__main__":
    main()
