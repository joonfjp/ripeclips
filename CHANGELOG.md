# Changelog

All notable changes to RipeClips are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Nothing yet.

## [0.1.0] - 2026-07-15

Initial source release.

### Added
- **Clip it** — end-to-end pipeline: local transcription, LLM highlight
  detection (any OpenAI-compatible endpoint), timeline refinement, 9:16
  reframing, burned-in captions, and single or batch export.
- **Trim** — zoomable timeline trimmer with frame-accurate re-encode and
  optional portrait 9:16 reframe (blurred fill or black bars).
- **Caption** — transcribe and burn styled captions in one pass, with 18
  bundled fonts, 12 color presets, and 15 style variations.
- **Transcribe** — local, word-timed SRT generation via a self-hosted
  Parakeet-compatible endpoint.
- Background-music beds across every export flow.
- `highlightr` self-hosted highlight-selection API.
- `parakeet` vendored transcription service (fork of
  [groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai](https://github.com/groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai)).

[Unreleased]: https://github.com/joonfjp/ripeclips/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/joonfjp/ripeclips/releases/tag/v0.1.0
