<div align="center">

# 🍑 highlightr

**A self-hosted API that reads a podcast transcript and picks the moments worth clipping.**

highlightr is the highlight-selection engine behind RipeClips. You hand it a
transcript; it hands back a ranked, scored, non-overlapping set of highlight
clips with exact timings — using any LLM endpoint you control. A companion
script can splice those spans straight out of your source video.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](https://docs.docker.com/compose/)

</div>

---

## Contents

- [What it is](#what-it-is)
- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Getting started](#getting-started)
- [Usage](#usage)
- [What you get back](#what-you-get-back)
- [Tuning selection](#tuning-selection)
- [Processing options](#processing-options)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Testing](#testing)

---

## What it is

highlightr does exactly one thing well: **decide which spans of a long episode
deserve to become clips, then hand back their precise timings.** It is a
backend-first service — a FastAPI app with a JSON/SRT API — that runs on your own
hardware and points at any OpenAI-compatible LLM you control.

It deliberately owns only the *selection* stage. Transcription happens upstream
(feed it a transcript or an `.srt`); reframing, captioning, and export happen
downstream. The included `splice_highlights.py` script can cut clips from the
result JSON when you want a standalone backend workflow. In RipeClips, the
desktop app and the
[`parakeet`](../parakeet) transcription service handle those stages — but
highlightr has no dependency on them and works with any transcript source.

> The LLM never sets timing. It proposes candidate spans with transcript row IDs
> and verbatim text anchors; final clip boundaries are recovered from those row
> IDs, while anchor matching contributes to the clip's recovery confidence.

---

## Features

- **Transcript-driven highlight detection** — ingest a row-backed JSON
  transcript or upload a raw `.srt` file directly.
- **LLM discovery + global ranking** — the episode is scanned in overlapping
  windows to surface candidates, then de-duplicated candidates are re-ranked
  against the whole episode when there is more than one, so scores are
  calibrated, not local.
- **Timestamps you can trust** — final clip boundaries come from transcript row
  IDs, with confidence informed by verbatim text anchors, not from
  model-generated numbers.
- **Transparent scoring** — every clip reports `interestingness`,
  `self_contained`, `boundary_quality`, `topic_relevance`, and
  `recovery_confidence`, plus a calibrated `overall` score.
- **Topic focusing** — steer selection toward specific themes with `soft`,
  `hard`, or `only` topic policies, plus tunable admission strictness.
- **Discovery presets** — `best_only`, `topic_focused`, `topic_only`,
  `coverage`, and `research_sweep` cover everything from "just the bangers" to
  "show me everything for review."
- **Dedupe + non-overlap selection** — near-duplicate candidates are merged and
  overlapping spans are pruned so you get distinct clips.
- **Sync or async** — run a quick synchronous request, or submit a job and poll
  progress window-by-window for full-length episodes.
- **Frame-accurate cutting** — the bundled `splice_highlights.py` re-encodes
  with ffmpeg for frame-accurate, QuickTime-compatible clips, optionally with a
  time-shifted `.srt` beside each one.
- **Bring your own LLM** — point it at OpenAI, OpenRouter, or a local server
  (llama.cpp `llama-server`, Ollama, LM Studio, vLLM). Transcript windows are
  only sent to the LLM endpoint you configure; use a local endpoint to keep them
  on your machine.
- **Docker-ready** — one `docker compose up` and the API is live.

---

## How it works

```
  transcript (.srt or JSON rows)
              │
              ▼
   ┌──────────────────────┐
   │  Windowed discovery   │   LLM proposes scored candidate spans
   │  (overlapping chunks) │   per window
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  Anchor recovery      │   score confidence against verbatim quotes;
   │  + row-backed timing  │   pull exact start/end from transcript rows
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  Dedupe + global rank │   merge duplicates, re-score multiple
   │                       │   candidates across the whole episode
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  Non-overlap select   │   pick the final ranked, distinct clips
   └──────────┬───────────┘
              ▼
     highlight JSON  ──►  splice_highlights.py  ──►  MP4 clips (+ .srt)
```

---

## Requirements

- **Python 3.10+** (or just Docker; the included image uses Python 3.12)
- **ffmpeg** on your `PATH` — required only for `splice_highlights.py`, which
  cuts clips out of video. The API itself doesn't need it.
- **An OpenAI-compatible LLM API** — highlightr POSTs to `/chat/completions`
  with structured outputs (`response_format: json_schema`). Point it at any
  compatible backend with the `LLM_BASE_URL` and `LLM_MODEL` environment
  variables.

---

## Getting started

### 1. Configure your LLM endpoint

highlightr talks to any OpenAI-compatible API. Point it at your backend:

```bash
export LLM_BASE_URL="http://localhost:8080/v1"   # local llama-server on the same host
export LLM_MODEL="gemma4"
# export LLM_API_KEY="sk-..."   # only if your backend requires auth
```

Under the hood it POSTs to `{LLM_BASE_URL}/chat/completions` using structured
outputs (`response_format: json_schema`) and parses the JSON returned in the
assistant message.

Each request contains transcript text or excerpts derived from it. The default
configuration uses a local llama-server; if you configure a hosted endpoint,
that provider receives the request content. RipeClips never sends the source
audio or video to the LLM. See [`../PROMPTS.md`](../PROMPTS.md) for the exact
system and user prompt templates and the data inserted into them.


- [`llama-server`](https://github.com/ggml-org/llama.cpp/tree/master/tools/server)

  Get a compatible GGUF from the [Unsloth Gemma 4 Hugging Face
  collection](https://huggingface.co/collections/unsloth/gemma-4).

  ```bash
  # llama-server -m /path/to/gemma4.gguf --host 0.0.0.0 --port 8080  (started separately)
  LLM_BASE_URL="http://host.docker.internal:8080/v1" \
  LLM_MODEL="gemma4" \
    docker compose up --build
  ```

- [Ollama](https://ollama.com)

  ```bash
  LLM_BASE_URL="http://host.docker.internal:11434/v1" \
  LLM_MODEL="gemma4" \
    docker compose up --build
  ```

- [OpenRouter](https://openrouter.ai/docs/quickstart)

  ```bash
  LLM_BASE_URL="https://openrouter.ai/api/v1" \
  LLM_MODEL="anthropic/claude-opus-4.8" \
  LLM_API_KEY="sk-or-..." \
    docker compose up --build
  ```

- [Cerebras](https://inference-docs.cerebras.ai/resources/openai)

  ```bash
  LLM_BASE_URL="https://api.cerebras.ai/v1" \
  LLM_MODEL="gpt-oss-120b" \
  LLM_API_KEY="csk-..." \
    docker compose up --build
  ```

- [Groq](https://console.groq.com/docs/openai)

  ```bash
  LLM_BASE_URL="https://api.groq.com/openai/v1" \
  LLM_MODEL="openai/gpt-oss-120b" \
  LLM_API_KEY="gsk_..." \
    docker compose up --build
  ```

- [OpenAI](https://platform.openai.com/docs/api-reference)

  ```bash
  LLM_BASE_URL="https://api.openai.com/v1" \
  LLM_MODEL="gpt-5.5" \
  LLM_API_KEY="sk-..." \
    docker compose up --build
  ```


### 2. Run it

**Locally:**

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

**With Docker:**

```bash
LLM_BASE_URL="http://host.docker.internal:8080/v1" \
LLM_MODEL="gemma4" \
docker compose up --build
```

Use `host.docker.internal` when the container should reach a `llama-server` or
other local LLM running on your Mac.

The API listens on `http://localhost:8000`. Interactive docs are at
`http://localhost:8000/docs`.

---

## Usage

### API endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET`  | `/health` | Liveness check |
| `POST` | `/v2/highlights/identify` | Identify highlights from a JSON transcript |
| `POST` | `/v2/highlights/identify-srt` | Identify highlights from an uploaded `.srt` |
| `POST` | `/v2/highlights/jobs` | Start an async job from a JSON transcript |
| `POST` | `/v2/highlights/srt-jobs` | Start an async job from an uploaded `.srt` |
| `GET`  | `/v2/highlights/jobs/{job_id}` | Poll job status and progress |
| `GET`  | `/v2/highlights/jobs/{job_id}/result` | Fetch the completed result |

### Identify highlights from a transcript

```bash
curl -X POST http://localhost:8000/v2/highlights/identify \
  -H 'Content-Type: application/json' \
  -d '{
    "transcript": {
      "source_id": "episode_abc123",
      "language": "en",
      "rows": [
        {"id": 0, "start_seconds": 0, "end_seconds": 35, "speaker": "Host",
         "text": "Why did this become such an important problem?"},
        {"id": 1, "start_seconds": 35, "end_seconds": 95, "speaker": "Guest",
         "text": "The surprising part is everyone saw it coming, but nobody changed course."}
      ]
    },
    "selection": {
      "min_clip_duration_seconds": 30,
      "max_clip_duration_seconds": 1200,
      "max_clips": 3,
      "style": "insightful, surprising, emotionally engaging",
      "topics": ["AI strategy", "pricing"],
      "topic_policy": "hard",
      "admission_strictness": 0.75
    }
  }'
```

### Run against a real `.srt`

The included script uploads an `.srt`, uses async job mode by default, and saves
the result JSON to `output/`:

```bash
python scripts/test_identify_srt.py /path/to/episode.srt \
  --style "practical, surprising, self-contained" \
  --topic "AI strategy" \
  --topic "pricing" \
  --discovery-mode topic_focused
```

Pass `--sync` for tiny smoke tests. Omit `--max-clips` to return every
non-overlapping clip that passes selection.

### Cut the clips

Splice frame-accurate MP4 clips from a saved result:

```bash
python scripts/splice_highlights.py output/episode_result.json
```

Add time-shifted, wrapped subtitles beside each clip:

```bash
python scripts/splice_highlights.py output/episode_result.json \
  --write-srt \
  --srt-max-lines 2 \
  --srt-max-words-per-line 8
```

---

## What you get back

Every identify call returns the same shape: a ranked list of `clips`, the
`rejected` stats explaining what got filtered and why, and a
`processing_summary` describing how the run was configured.

```json
{
  "source_id": "episode_abc123",
  "clips": [
    {
      "clip_id": "clip_001",
      "rank": 1,
      "start_seconds": 512.24,
      "end_seconds": 663.01,
      "duration_seconds": 150.77,
      "start_row_id": 42,
      "end_row_id": 58,
      "title": "Everyone saw it coming",
      "summary": "The guest explains why an obvious problem went unaddressed for years.",
      "reason": "Self-contained setup and payoff with a surprising turn. Global ranking: strongest standalone moment.",
      "scores": {
        "overall": 0.91,
        "interestingness": 0.93,
        "self_contained": 0.88,
        "boundary_quality": 0.86,
        "topic_relevance": 0.95,
        "recovery_confidence": 1.0
      },
      "anchors": {
        "start": "The surprising part is everyone saw it coming",
        "end": "and that is when it finally changed"
      },
      "source": {
        "window_ids": ["window_0003"],
        "candidate_ids": ["cand_1a2b3c4d"]
      }
    }
  ],
  "rejected": {
    "raw_candidates": 24,
    "deduped_candidates": 11,
    "removed_for_overlap": 3,
    "removed_for_duration": 2,
    "removed_for_low_confidence": 1,
    "removed_for_low_score": 6,
    "removed_for_topic_mismatch": 1
  },
  "processing_summary": {
    "window_count": 8,
    "llm_calls": 9,
    "mode": "llm_structured",
    "version": "ripe-clips",
    "discovery_window_seconds": 1200,
    "stride_seconds": 300
  }
}
```

`start_row_id` / `end_row_id` tie each clip back to the exact transcript rows it
came from, and `anchors` hold the verbatim quotes used to score recovery
confidence. Feed this JSON straight into `splice_highlights.py` to cut the
clips.

---

## Tuning selection

Selection behavior is controlled through a few options (`selection.*` in the
JSON API, flags on the test script):

- **`style`** — free-form preference string passed to the LLM.
- **`topics`** — themes to focus on. In the JSON API this is a list; the `.srt`
  endpoints accept a comma-separated form field.
- **`topic_policy`** — `soft` (prefer topics, allow exceptional off-topic
  clips), `hard` (must substantially address a topic), or `only` (reject
  anything not centered on a topic).
- **`admission_strictness`** — the minimum score a candidate needs to be
  admitted (0–1).
- **`min_candidates_per_window`** — allow lower-scoring fallback candidates per
  window for coverage-style runs, when valid candidates are available.

The test script bundles these into `--discovery-mode` presets:

| Preset | Behavior |
| --- | --- |
| `best_only` | Strict, broad highlight discovery |
| `topic_focused` | Require substantial topic relevance |
| `topic_only` | Reject clips not centered on the topics |
| `coverage` | Prefer at least one usable candidate per window |
| `research_sweep` | Broadest mode, for review and debugging |

---

## Processing options

The `processing.*` block controls how the episode is chunked and de-duplicated.
All of these are optional — leave them out and highlightr picks smart defaults
scaled to your clip length.

- **`discovery_window_seconds`** — how much transcript the LLM sees at once. If
  omitted, it scales to 1.35x `max_clip_duration_seconds` (floored at 10 min,
  capped at 30 min) so short clips don't pay for oversized prompts.
- **`stride_seconds`** — how far each window advances. Defaults to half the
  window (clamped to 60–600s), giving overlapping coverage so moments on a
  window boundary aren't missed.
- **`candidates_per_window`** — max candidates the LLM proposes per window
  (default 5).
- **`dedupe_overlap_threshold`** — how much two candidates must overlap to be
  treated as duplicates (0–1, default 0.65).

For the default 20-minute max clip, that works out to a 27-minute window and a
10-minute stride. The `--window` and `--stride` flags on the test script
override these.

---

## Configuration

For Docker, copy [`.env.example`](.env.example) to `.env` and adjust as needed.
For local Python runs, export the same variables in your shell. Every variable
is optional and falls back to a built-in default.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_BASE_URL` | `http://localhost:8080/v1` | Base URL of an OpenAI-compatible API (should end in `/v1`) |
| `LLM_MODEL` | `gemma4` | Model name passed through to the backend |
| `LLM_API_KEY` | *(unset)* | Optional bearer token; leave unset for local backends |
| `LLM_TIMEOUT_SECONDS` | `180` | Per-request timeout for each chat completion call |
| `HIGHLIGHTS_INPUT_ROOT` | `./input` | Fallback media root for `splice_highlights.py` when result JSON has no `_local` paths |

> ⚠️ Set `LLM_BASE_URL` (and `LLM_MODEL`) to a backend you can actually reach
> before running — nothing works until the endpoint responds.

---

## Project layout

```
.
├── app/
│   ├── main.py         # FastAPI app: endpoints, async job orchestration
│   ├── highlights.py   # core engine: windowing, discovery, ranking, selection
│   ├── llm.py          # OpenAI-compatible chat/completions client with retries
│   ├── models.py       # Pydantic request/response schemas
│   └── srt.py          # SRT parsing into transcript rows
├── scripts/
│   ├── test_identify_srt.py   # upload an SRT, poll a job, save result JSON
│   └── splice_highlights.py   # cut MP4 clips (+ shifted SRT) from a result
├── tests/              # unit tests (pytest) for parsing, scoring, and models
├── util.py             # frame-accurate ffmpeg trimming helper
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Testing

```bash
pip install -r requirements.txt
pytest
```

The suite covers SRT parsing, scoring/selection logic, request/response models,
the LLM client, and timestamp helpers used by the ffmpeg trimming flow.

---
