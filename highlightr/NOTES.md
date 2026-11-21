# RipeClips API

FastAPI service for row-backed highlight identification. This service only decides
which transcript spans should become clips; rendering, subtitles, effects, and
file movement belong to later pipeline stages.

## Run locally

```bash
pip install -e .
uvicorn app.main:app --reload
```

## Run with Docker

```bash
docker compose up --build
```

The API listens on `http://localhost:8000`.

## Endpoints

- `GET /health`
- `GET /ready`
- `POST /v2/highlights/identify`
- `POST /v2/highlights/identify-srt`
- `POST /v2/highlights/jobs`
- `POST /v2/highlights/srt-jobs`
- `GET /v2/highlights/jobs/{job_id}`
- `GET /v2/highlights/jobs/{job_id}/result`

## Example

```bash
curl -X POST http://localhost:8000/v2/highlights/identify \
  -H 'Content-Type: application/json' \
  -d '{
    "transcript": {
      "source_id": "episode_abc123",
      "language": "en",
      "rows": [
        {
          "id": 0,
          "start_seconds": 0,
          "end_seconds": 35,
          "speaker": "Host",
          "text": "Why did this become such an important problem?"
        },
        {
          "id": 1,
          "start_seconds": 35,
          "end_seconds": 95,
          "speaker": "Guest",
          "text": "I think the surprising part is that everyone saw it coming, but nobody changed course."
        }
      ]
    },
    "selection": {
      "min_clip_duration_seconds": 30,
      "max_clip_duration_seconds": 1200,
      "max_clips": 3,
      "target": "best",
      "style": "insightful, surprising, emotionally engaging",
      "topics": ["AI strategy", "pricing"],
      "topic_policy": "hard",
      "admission_strictness": 0.75,
      "min_candidates_per_window": 0
    },
    "processing": {
      "discovery_window_seconds": 1200,
      "stride_seconds": 300,
      "candidates_per_window": 5,
      "dedupe_overlap_threshold": 0.65
    }
  }'
```

Candidate discovery, global ranking, and metadata enrichment use the injectable
`LLMClient`. Provider output is decoded strictly and validated against typed
contracts before the API recovers final timing from transcript row IDs.

## Test With A Real SRT

Start the service, then upload a `.srt` directly:

```bash
python scripts/test_identify_srt.py /path/to/episode.srt
```

For full podcasts with LLM discovery, the script uses async job mode by default:

```bash
python scripts/test_identify_srt.py /path/to/episode.srt \
  --style "practical, surprising, self-contained" \
  --topic "AI strategy" \
  --topic "pricing" \
  --discovery-mode topic_focused
```

Pass `--sync` only for tiny smoke tests where one HTTP request is acceptable.

To splice MP4 clips from a saved highlight JSON, use:

```bash
python scripts/splice_highlights.py output/episode_results.json
```

The script uses the same `trim_mp4_exact` ffmpeg trimming helper from `util.py`.
By default it only writes MP4 clips. To also write shifted `.srt` files beside
each clip, pass `--write-srt`. Shifted SRT output uses at most two subtitle text
rows per cue; configure wrapping with:

```bash
python scripts/splice_highlights.py output/episode_results.json \
  --write-srt \
  --srt-max-lines 2 \
  --srt-max-words-per-line 8
```

By default, the script writes the full JSON response to `highlightr/output/` and prints
a compact summary. Pass `--dump-json` to also print the full response.
If `--max-clips` is omitted, the API returns every deduped candidate that passes
the non-overlap selector. Pass `--max-clips N` to cap the final result count.

If `--window` or `--stride` are omitted, the backend chooses smart defaults from
`max_clip_duration_seconds`. For the default 20-minute max clip, it uses a
27-minute discovery window and a 10-minute stride.

The backend parses the SRT into row-backed transcript data and runs the same
highlight identifier used by the JSON endpoint.

`style` is a free-form string passed to the LLM as a preference. `topics` is a
structured list in the JSON API; the SRT upload endpoints accept it as a
comma-separated form field, which the test script builds from repeated
`--topic` flags or `--topics "topic one, topic two"`.

The internal strictness controls are `topic_policy`, `admission_strictness`, and
`min_candidates_per_window`. The test script exposes those through
`--discovery-mode` presets:

- `best_only`: strict, broad highlight discovery.
- `topic_focused`: independently verify candidates and return only clips that
  directly or materially relate to the supplied topics.
- `coverage`: try to keep at least one usable candidate per window.
- `research_sweep`: broadest mode for review/debugging.

Independent discovery windows can run with bounded concurrency through
`LLM_MAX_CONCURRENCY`. Ranking uses tournament batches and metadata uses
independent batches, both bounded by `LLM_MAX_PROMPT_CHARS`.

Async jobs use a bounded, expiring in-memory `JobStore` by default. They are
safe for a single API process but are not durable across restarts or shared
across multiple workers.
