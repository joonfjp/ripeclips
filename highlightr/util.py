from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

def trim_mp4_exact(
    input_path: str | Path,
    output_path: str | Path,
    start_ts: str,
    end_ts: str,
    *,
    vcodec: str = "libx264",
    acodec: str = "aac",
    crf: int = 18,
    preset: str = "fast",
    audio_bitrate: str = "192k",
    fps: int = 30,
) -> None:
    """
    Trim an MP4 between start_ts and end_ts as accurately as ffmpeg allows (frame-accurate).
    This re-encodes video/audio to avoid keyframe-boundary limitations.
    Timestamps may be "HH:MM:SS[.ms]" or seconds like "12.345".
    Output is forced to a constant frame rate of ``fps`` (default 30). Pass the
    source frame rate to avoid resampling footage shot at 24/25/60 fps.
    Raises:
        FileNotFoundError: if input file doesn't exist
        RuntimeError: if ffmpeg isn't installed or trimming fails
        ValueError: if timestamps are invalid, fps <= 0, or end <= start
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH. Install ffmpeg and try again.")
    if fps <= 0:
        raise ValueError(f"fps must be positive (got {fps})")
    
    start_s = _parse_timestamp_to_seconds(start_ts)
    end_s = _parse_timestamp_to_seconds(end_ts)
    
    if end_s <= start_s:
        raise ValueError(f"end_ts must be > start_ts (got start={start_ts}, end={end_ts})")
    
    duration = end_s - start_s
    
    # Use larger buffer for GOP safety (10 seconds or start, whichever is smaller)
    seek_buffer = min(10, start_s)
    fast_seek = max(0, start_s - seek_buffer)
    precise_seek = start_s - fast_seek
    
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        _format_seconds(fast_seek),  # Fast seek to keyframe
        "-i",
        str(input_path),
        "-ss",
        _format_seconds(precise_seek),  # Precise trim
        "-t",
        _format_seconds(duration),
        "-map",
        "0:v:0",  # Explicitly map first video stream
        "-map",
        "0:a:0?",  # Map first audio stream if it exists
        "-c:v",
        vcodec,
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-x264-params",
        # 1-second GOP tied to the output fps, no B-frames, for consistent seeking.
        f"keyint={fps}:min-keyint={fps}:no-scenecut:bframes=0",
        "-pix_fmt",
        "yuv420p",  # Ensure QuickTime compatibility
        "-c:a",
        acodec,
        "-b:a",
        audio_bitrate,
        "-movflags",
        "+faststart",
        "-vsync",
        "cfr",  # Force constant frame rate
        "-r",
        str(fps),
        "-y",
        str(output_path),
    ]
    
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"ffmpeg trim failed (exit {e.returncode}). stderr: {e.stderr}"
        ) from e

def _parse_timestamp_to_seconds(ts: str) -> float:
    ts = ts.strip()
    if ":" not in ts:
        # seconds
        return float(ts)

    parts = ts.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid timestamp: {ts!r}")

    if len(parts) == 2:
        hh = 0
        mm, ss = parts
    else:
        hh, mm, ss = parts

    return int(hh) * 3600 + int(mm) * 60 + float(ss)


def _format_seconds(seconds: float) -> str:
    # ffmpeg accepts seconds with decimals; keep it simple and stable.
    return f"{seconds:.6f}".rstrip("0").rstrip(".")
