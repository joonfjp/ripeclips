#!/usr/bin/env python3
"""Submit an SRT to the local Ripe Clips API and save highlight JSON.

Example:
    python3 highlightr/scripts/test_identify_srt.py /path/to/episode.srt

With repeated topic flags:
    python3 highlightr/scripts/test_identify_srt.py /path/to/episode.srt \
      --style "practical, surprising, self-contained" \
      --topic "AI strategy" \
      --topic "pricing"

With comma-separated topics:
    python3 highlightr/scripts/test_identify_srt.py /path/to/episode.srt \
      --style "practical, surprising, self-contained" \
      --topics "AI strategy, pricing"

With a discovery preset:
    python3 highlightr/scripts/test_identify_srt.py /path/to/episode.srt \
      --topics "AI strategy, pricing" \
      --discovery-mode topic_focused

For a quick synchronous smoke test:
    python3 highlightr/scripts/test_identify_srt.py /path/to/short.srt --sync
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError

DISCOVERY_MODE_PRESETS = {
    "best_only": {
        "topic_policy": "soft",
        "admission_strictness": "0.85",
        "min_candidates_per_window": "0",
    },
    "topic_focused": {
        "topic_policy": "hard",
        "admission_strictness": "0.65",
        "min_candidates_per_window": "0",
    },
    "coverage": {
        "topic_policy": "soft",
        "admission_strictness": "0.35",
        "min_candidates_per_window": "1",
    },
    "research_sweep": {
        "topic_policy": "soft",
        "admission_strictness": "0.25",
        "min_candidates_per_window": "1",
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload a real SRT file to the Ripe Clips SRT endpoint."
    )
    parser.add_argument("srt", type=Path, help="Path to a .srt file.")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/v2/highlights/identify-srt",
        help="SRT endpoint URL.",
    )
    parser.add_argument(
        "--job",
        action="store_true",
        help="Submit as an async job and poll until complete. This is the default.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Use the synchronous endpoint. Only recommended for tiny smoke tests.",
    )
    parser.add_argument(
        "--job-url",
        default="http://127.0.0.1:8000/v2/highlights/srt-jobs",
        help="Async SRT job endpoint URL.",
    )
    parser.add_argument(
        "--jobs-base-url",
        default="http://127.0.0.1:8000/v2/highlights/jobs",
        help="Base URL for job status/result endpoints.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-request timeout in seconds. Default: 300.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Polling interval in seconds for --job. Default: 5.",
    )
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--language", default="en")
    parser.add_argument(
        "--target",
        default=None,
        help="Selection target sent to the LLM. Default: backend default.",
    )
    parser.add_argument(
        "--style",
        default=None,
        help="Free-form style preference sent to the LLM.",
    )
    parser.add_argument(
        "--topic",
        action="append",
        default=[],
        help="Focus topic for clip selection. Can be passed more than once.",
    )
    parser.add_argument(
        "--topics",
        default=None,
        help="Comma-separated focus topics for clip selection.",
    )
    parser.add_argument(
        "--discovery-mode",
        choices=sorted(DISCOVERY_MODE_PRESETS),
        default=None,
        help="Preset for topic strictness and per-window coverage.",
    )
    parser.add_argument("--min-duration", type=int, default=60)
    parser.add_argument("--max-duration", type=int, default=1200)
    parser.add_argument(
        "--max-clips",
        type=int,
        default=None,
        help="Maximum clips to return. Omit to return all non-overlapping clips.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help="Override discovery window seconds. Defaults to backend auto.",
    )
    parser.add_argument(
        "--stride",
        type=int,
        default=None,
        help="Override stride seconds. Defaults to backend auto.",
    )
    parser.add_argument("--candidates-per-window", type=int, default=5)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "output",
        help="Directory for the JSON result file. Default: highlightr/output.",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=None,
        help="Exact JSON output path. Overrides --output-dir.",
    )
    parser.add_argument(
        "--no-output-file",
        action="store_true",
        help="Do not write the JSON response to disk.",
    )
    parser.add_argument("--dump-json", action="store_true")
    args = parser.parse_args()
    use_job = not args.sync

    if not args.srt.is_file():
        print(f"SRT file not found: {args.srt}", file=sys.stderr)
        return 2

    fields = {
        "language": args.language,
        "min_clip_duration_seconds": str(args.min_duration),
        "max_clip_duration_seconds": str(args.max_duration),
        "candidates_per_window": str(args.candidates_per_window),
    }
    if args.max_clips is not None:
        fields["max_clips"] = str(args.max_clips)
    if args.window is not None:
        fields["discovery_window_seconds"] = str(args.window)
    if args.stride is not None:
        fields["stride_seconds"] = str(args.stride)
    if args.source_id:
        fields["source_id"] = args.source_id
    if args.target:
        fields["target"] = args.target
    if args.style:
        fields["style"] = args.style
    topics = parse_topics(args.topics, args.topic)
    if topics:
        fields["topics"] = ", ".join(topics)
    if args.discovery_mode:
        fields.update(DISCOVERY_MODE_PRESETS[args.discovery_mode])

    body, content_type = build_multipart(
        fields=fields,
        file_field="file",
        file_path=args.srt,
    )

    if use_job:
        payload = run_job_request(body, content_type, args)
        if payload is None:
            return 1
    else:
        payload = run_sync_request(body, content_type, args)
        if payload is None:
            return 1

    output_path = None
    if not args.no_output_file:
        output_path = write_output_file(payload, args)

    if args.dump_json:
        print(json.dumps(payload, indent=2))
        if output_path:
            print(f"saved: {output_path}")
        return 0

    summary = payload["processing_summary"]
    rejected = payload["rejected"]
    print(f"source_id: {payload['source_id']}")
    print(
        "summary: "
        f"{summary['window_count']} windows, "
        f"window={summary['discovery_window_seconds']}s, "
        f"stride={summary['stride_seconds']}s, "
        f"mode={summary['mode']}, "
        f"llm_calls={summary['llm_calls']}, "
        f"{len(payload['clips'])} clips, "
        f"{rejected['raw_candidates']} raw candidates, "
        f"{rejected['deduped_candidates']} deduped"
    )
    if output_path:
        print(f"saved: {output_path}")

    for clip in payload["clips"]:
        print(
            f"#{clip['rank']} "
            f"{format_seconds(clip['start_seconds'])}-"
            f"{format_seconds(clip['end_seconds'])} "
            f"({clip['duration_seconds']:.1f}s) "
            f"score={clip['scores']['overall']:.3f} "
            f"{clip['title']}"
        )

    return 0


def run_sync_request(body: bytes, content_type: str, args: argparse.Namespace) -> dict | None:
    req = request.Request(
        args.url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    return read_json_response(req, timeout=args.timeout)


def run_job_request(body: bytes, content_type: str, args: argparse.Namespace) -> dict | None:
    create_req = request.Request(
        args.job_url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    status = read_json_response(create_req, timeout=args.timeout)
    if status is None:
        return None

    job_id = status["job_id"]
    print(
        f"job: {job_id} status={status['status']} "
        f"windows={status['progress']['windows_total']}",
        flush=True,
    )
    last_seen = (
        status["status"],
        status["stage"],
        status["progress"]["windows_done"],
        status["progress"]["windows_total"],
    )

    while True:
        time.sleep(args.poll_interval)
        status_req = request.Request(f"{args.jobs_base_url}/{job_id}", method="GET")
        status = read_json_response(status_req, timeout=args.timeout)
        if status is None:
            return None

        progress = status["progress"]
        current = (
            status["status"],
            status["stage"],
            progress["windows_done"],
            progress["windows_total"],
        )
        if current != last_seen:
            print(
                f"job: {job_id} status={status['status']} stage={status['stage']} "
                f"{progress['windows_done']}/{progress['windows_total']}",
                flush=True,
            )
            last_seen = current

        if status["status"] == "completed":
            result_req = request.Request(
                f"{args.jobs_base_url}/{job_id}/result",
                method="GET",
            )
            return read_json_response(result_req, timeout=args.timeout)
        if status["status"] == "failed":
            print(f"Job failed: {status.get('error')}", file=sys.stderr)
            return None


def read_json_response(req: request.Request, *, timeout: int) -> dict | None:
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code} from API: {body}", file=sys.stderr)
        return None
    except TimeoutError:
        print(f"Timed out after {timeout}s waiting for API response.", file=sys.stderr)
        return None
    except URLError as exc:
        print(f"Could not reach API: {exc.reason}", file=sys.stderr)
        return None


def write_output_file(payload: dict, args: argparse.Namespace) -> Path:
    payload = dict(payload)
    payload["_local"] = {
        "srt_path": str(args.srt.expanduser().resolve()),
        "mp4_path": str(args.srt.expanduser().resolve().with_suffix(".mp4")),
    }

    if args.output_file:
        output_path = args.output_file
    else:
        source_id = payload.get("source_id") or args.srt.stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{slugify(source_id)}_{timestamp}.json"
        output_path = args.output_dir / filename

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output_path


def parse_topics(comma_separated: str | None, repeated: list[str]) -> list[str]:
    topics: list[str] = []
    if comma_separated:
        topics.extend(comma_separated.split(","))
    topics.extend(repeated)
    return [topic.strip() for topic in topics if topic.strip()]


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return slug or "highlights"


def build_multipart(
    *,
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
) -> tuple[bytes, str]:
    boundary = f"----highlights-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )

    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
            ).encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )

    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def format_seconds(value: float) -> str:
    total = int(value)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
