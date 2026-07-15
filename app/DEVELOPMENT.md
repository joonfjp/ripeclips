# Development

## Run in development

```bash
mise exec -- npm run tauri:dev
```

This starts Vite on port 1420 and opens RipeClips as a native macOS window.
Both the Vite dev server and the Rust backend rebuild on change.

> The first run compiles the Rust backend from scratch, so it can take a few
> minutes. Later runs are fast.

## Architecture

RipeClips keeps a hard boundary between the webview UI and the filesystem: the
frontend never touches disk directly. Everything goes through narrow,
audited Rust commands.

| Layer | Technology |
|-------|-----------|
| Shell | [Tauri 2](https://tauri.app) (Rust) |
| Frontend | Vite + vanilla JavaScript |
| Media | Native FFmpeg sidecar (`libass`, `libx264`) |
| Transcription | Local Parakeet-compatible endpoint |
| Highlight detection | Local Highlights API (via Tauri HTTP plugin) |

- The frontend builds ASS subtitle scripts and CSS previews from shared
  `ass.js` / `presets.js` modules.
- File selection uses the Tauri dialog plugin and stores absolute paths.
- MP4 previews stream through a custom `stream://` protocol with full HTTP Range
  support. The scope starts **empty** and is extended at runtime to only the
  selected file, so the webview can never reach arbitrary files.
- SRTs are read through a single narrow `read_srt` command (existing file +
  `.srt` extension + 10 MB cap + UTF-8/BOM handling).
- All encoding (`render_video`, `trim_video`, `extract_audio`,
  `transcode_preview`) runs the FFmpeg sidecar from Rust and emits progress
  events to the UI. The `trim_video` command optionally pillarboxes to 9:16 in
  the same pass. **Only Rust can start FFmpeg.**
- A configurable working directory holds intermediates, render scratch, and
  cached preview proxies; intermediates are cleaned up after each run.

## Build a distributable app

```bash
mise exec -- npm run tauri:build
```

The output lands under `src-tauri/target/release/bundle/`:

- `macos/RipeClips.app` — the launchable application bundle.
- `dmg/RipeClips_0.1.0_aarch64.dmg` — a drag-to-install disk image.
