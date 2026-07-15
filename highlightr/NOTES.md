# RipeClips API

FastAPI service for row-backed highlight identification. This service only decides
which transcript spans should become clips; rendering, subtitles, effects, and
file movement belong to later pipeline stages.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Run with Docker

```bash
docker compose up --build
```

The API listens on `http://localhost:8000`.

## Endpoints

- `GET /health`
- `POST /v2/highlights/identify`
- `POST /v2/highlights/identify-srt`
- `POST /v2/highlights/jobs`
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

Candidate discovery and global ranking use the configured structured LLM proxy
through `call_llm_structured`. The API still recovers final timing from
transcript row IDs instead of trusting model-generated timestamps.

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
20-minute discovery window and a 5-minute stride.

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
- `topic_focused`: require substantial topic relevance.
- `topic_only`: reject clips that are not directly centered on the topics.
- `coverage`: try to keep at least one usable candidate per window.
- `research_sweep`: broadest mode for review/debugging.
