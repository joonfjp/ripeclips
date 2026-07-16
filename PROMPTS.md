# LLM prompts and data disclosure

RipeClips uses the LLM only to discover and rank highlight candidates. The
transcription service does not call an LLM. Requests are sent to the
OpenAI-compatible endpoint configured by `LLM_BASE_URL`; the default Docker
configuration uses llama-server on the host at
`http://host.docker.internal:8080/v1` with the `gemma4` model.

If you replace that URL with a hosted provider, the provider receives the data
described below and handles it under its own data and retention policies.

## Request envelope

Every LLM call is an HTTP `POST` to `{LLM_BASE_URL}/chat/completions` containing:

- the configured model name;
- the shared system prompt below;
- one of the two user prompts below; and
- a strict JSON response schema.

An `Authorization: Bearer ...` header is included only when `LLM_API_KEY` is
set. Source audio and video files are never included in an LLM request.

## Shared system prompt

The following system prompt is sent with every request:

```text
You are a precise assistant. Respond only with JSON that conforms exactly to the provided JSON schema.
```

## 1. Window discovery prompt

This prompt is sent once for each overlapping transcript window. A window may
cover 10–30 minutes by default, so some transcript rows can be sent more than
once where windows overlap.

Dynamic values include the requested clip duration, selection style, topics,
topic policy, admission threshold, language, window timestamps, and transcript
rows. Each transcript row includes its ID, start/end timestamps, optional
speaker name, and full text.

```text
You are selecting highlight clips from a podcast transcript.

Return up to {candidates_per_window} strong candidate clips from this window.
You may return fewer than {candidates_per_window}, including zero.

Selection target: {target}
Preferred style: {style}
Focus topics: {topics_or_none}
Topic policy: {topic_policy}
Minimum admission score: {admission_strictness}
Transcript language: {language_or_unknown}

Rules:
- Candidate duration must be between {min_duration} and {max_duration} seconds.
- Choose self-contained spans with clear setup and payoff.
- Prefer surprising, emotionally engaging, insightful, funny, useful, or high-stakes moments.
{topic_policy_rule}
- topic_relevance is 0.00 for unrelated clips and 1.00 for clips directly centered on the focus topics.
- Except for fallback coverage, only return candidates whose overall score is at least {admission_strictness}.
{coverage_rule}
- Use only row IDs that appear in this transcript window.
- start_row_id and end_row_id are inclusive row IDs.
- Do not invent timestamps or text.
- Anchors must be short verbatim quotes from near the selected start/end rows.
- Scores must be numbers from 0 to 1 and calibrated strictly:
  - 0.95-1.00: rare, exceptional even compared with a full episode
  - 0.90-0.94: excellent and highly shareable
  - 0.80-0.89: strong candidate
  - 0.70-0.79: usable but imperfect
  - below 0.70: weak or context-dependent
- Do not give every good clip a score above 0.90. Reserve high scores.
- Return JSON matching the provided schema exactly.

Transcript window {window_id}, {window_start}-{window_end}:
[{row_id}] {row_start} - {row_end} {optional_speaker}: {row_text}
...
```

The response schema requests candidate titles, summaries, reasons, row IDs,
verbatim boundary anchors, and five numeric scores: interestingness,
self-contained quality, boundary quality, topic relevance, and overall score.

## 2. Global ranking prompt

After discovery and local de-duplication, this prompt is sent when more than one
candidate remains. It contains every remaining candidate's generated title,
summary, reason, timestamps, duration, local score, topic-relevance score, and
a transcript excerpt from the candidate span.

```text
Rank these recovered podcast highlight candidates from best to worst.

Preferred style: {style}
Focus topics: {topics_or_none}
Topic policy: {topic_policy}

Rules:
- Include every candidate_id exactly once.
- Prefer clips with strong standalone value, clear setup/payoff, and memorable content.
- Apply the topic policy when ordering candidates.
- Calibrate global_score across the whole candidate set, not just within one topic.
- Use the full 0.00-1.00 range honestly:
  - 0.95-1.00: rare, episode-defining, likely top 1-2 clips only
  - 0.90-0.94: excellent, clearly worth clipping
  - 0.80-0.89: strong but not elite
  - 0.70-0.79: usable but has flaws
  - below 0.70: weak, context-dependent, or redundant
- Avoid tied scores unless two candidates are genuinely indistinguishable.
- Return JSON matching the provided schema exactly.

Candidates:
candidate_id: {candidate_id}
time: {start}-{end}
duration_seconds: {duration}
title: {title}
summary: {summary}
reason: {reason}
local_score: {local_score}
topic_relevance: {topic_relevance}
excerpt: {transcript_excerpt}

---
...
```

The response schema requests each candidate ID exactly once with a global score
and ranking reason.

## Source of truth

The executable prompt builders live in
[`highlightr/app/highlights.py`](highlightr/app/highlights.py), and the shared
system prompt and request envelope live in
[`highlightr/app/llm.py`](highlightr/app/llm.py). Update this document whenever
those prompts or schemas change.
