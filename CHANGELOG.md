# Changelog

All notable changes to RipeClips are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Highlightr transcript-row-based highlight selection, publishing metadata,
  and a metadata-only endpoint for clips supplied as SRT files.
- Bounded, expiring in-memory Highlightr jobs with progress polling and
  configurable LLM retries, timeouts, and concurrency limits.
- Parakeet upload size limits and service regression coverage.

### Changed
- Pin the bundled service images to `joonfjp/highlightr:1.0.0` and
  `joonfjp/parakeet:1.0.0` and refresh service deployment documentation.
- Isolate Parakeet audio processing from the API event loop and guard
  concurrent model loading.

### Fixed
- Parakeet audio chunking after forced cuts, preserving subsequent speech
  without adding a spurious trailing chunk.

### Removed
- Legacy Parakeet monolithic server, HTML interface, helper scripts, and
  separate GPU Dockerfile.

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
