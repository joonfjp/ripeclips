use std::collections::HashSet;
use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::http::{Request, Response, StatusCode};
use tauri::path::BaseDirectory;
use tauri::{AppHandle, Emitter, Manager, Runtime};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

const MAX_SRT_BYTES: u64 = 10 * 1024 * 1024;
/// Max bytes returned per `stream://` response. The webview re-requests further
/// ranges as it needs them, so this just bounds per-response memory.
const STREAM_CHUNK: u64 = 8 * 1024 * 1024;

/// Files the user has selected and may stream through the `stream://` protocol.
/// Starts empty; each selection adds exactly one canonical path, so the webview
/// can never reach arbitrary files on disk.
#[derive(Default)]
struct MediaAccess {
    allowed: Mutex<HashSet<PathBuf>>,
}

#[derive(Debug, Deserialize)]
pub struct RenderRequest {
    pub input_path: String,
    pub output_path: String,
    pub working_directory: String,
    pub ass_content: String,
    pub font_path: Option<String>,
    pub bundled_font_name: Option<String>,
    /// Optional directory of fonts to make discoverable to libass by name.
    #[serde(default)]
    pub font_dir: Option<String>,
    pub duration_seconds: Option<f64>,
    /// Optional background music track to mix under the clip's own audio.
    #[serde(default)]
    pub music_path: Option<String>,
    /// Music gain as a 0..1 fraction (e.g. 0.1 for 10%).
    #[serde(default)]
    pub music_volume: Option<f64>,
}

#[derive(Debug, Serialize)]
pub struct RenderResult {
    pub output_path: String,
}

#[derive(Debug, Deserialize)]
pub struct TrimRequest {
    pub input_path: String,
    pub output_path: String,
    /// Loop start/end in seconds, as measured on the timeline.
    pub start_seconds: f64,
    pub end_seconds: f64,
    /// Optional background music track to mix under the clip's own audio.
    #[serde(default)]
    pub music_path: Option<String>,
    /// Music gain as a 0..1 fraction (e.g. 0.1 for 10%).
    #[serde(default)]
    pub music_volume: Option<f64>,
    /// When true, pillarbox the trimmed clip onto a vertical 9:16 canvas in the
    /// same encode (for Shorts/Reels/TikTok).
    #[serde(default)]
    pub portrait: bool,
    /// Portrait canvas dimensions (default 1080×1920 when omitted).
    #[serde(default)]
    pub out_width: Option<u32>,
    #[serde(default)]
    pub out_height: Option<u32>,
    /// Pillarbox background when `portrait` is set: "blur" (blurred fill) or
    /// "black" (solid bars). Defaults to blurred fill.
    #[serde(default)]
    pub background: Option<String>,
    /// Optional ASS subtitle script to burn into the trimmed clip. When present,
    /// captions are rendered after any pillarbox reframing (so they sit on the
    /// final canvas). The remaining font fields mirror `RenderRequest` and are
    /// only used when this is set.
    #[serde(default)]
    pub ass_content: Option<String>,
    /// App working directory; a per-operation subdirectory is created under it
    /// to hold the ASS script and fonts. Required when `ass_content` is set.
    #[serde(default)]
    pub working_directory: Option<String>,
    /// Optional custom font file to burn with (overrides the bundled font).
    #[serde(default)]
    pub font_path: Option<String>,
    /// Bundled font file name to burn with when no custom font is chosen.
    #[serde(default)]
    pub bundled_font_name: Option<String>,
    /// Optional directory of fonts to make discoverable to libass by name.
    #[serde(default)]
    pub font_dir: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct TrimResult {
    pub output_path: String,
}

#[derive(Debug, Deserialize)]
pub struct ExtractAudioRequest {
    pub input_path: String,
    /// Where to write the extracted MP3.
    pub output_path: String,
}

#[derive(Debug, Serialize)]
pub struct ExtractAudioResult {
    pub output_path: String,
}

#[derive(Debug, Deserialize)]
pub struct TranscribeRequest {
    pub audio_path: String,
    pub base_url: String,
    pub model: String,
}

/// One audio track discovered inside a background-music directory.
#[derive(Debug, Serialize)]
pub struct AudioTrack {
    /// Absolute path to the audio file.
    pub path: String,
    /// File name without the directory (e.g. "lofi-beat.mp3").
    pub name: String,
    /// Source directory the track was found in.
    pub dir: String,
}

/// Audio container extensions FFmpeg can decode as a music bed.
const AUDIO_EXTS: &[&str] = &["mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "aiff", "wma"];

fn is_audio_file(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| e.to_ascii_lowercase())
        .map(|e| AUDIO_EXTS.contains(&e.as_str()))
        .unwrap_or(false)
}

fn ensure_directory(path: &Path) -> Result<PathBuf, String> {
    if path.as_os_str().is_empty() {
        return Err("Working directory cannot be empty.".into());
    }
    fs::create_dir_all(path).map_err(|e| format!("Could not create working directory: {e}"))?;
    path.canonicalize()
        .map_err(|e| format!("Could not resolve working directory: {e}"))
}

fn default_working_path<R: Runtime>(_app: &AppHandle<R>) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    {
        let project_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or("Could not resolve project directory.")?
            .to_path_buf();
        return Ok(project_dir.join("output"));
    }

    #[cfg(not(debug_assertions))]
    {
        _app.path()
            .app_data_dir()
            .map(|path| path.join("output"))
            .map_err(|e| format!("Could not resolve app data directory: {e}"))
    }
}

#[tauri::command]
fn default_working_directory<R: Runtime>(app: AppHandle<R>) -> Result<String, String> {
    let path = ensure_directory(&default_working_path(&app)?)?;
    Ok(path.to_string_lossy().into_owned())
}

#[tauri::command]
fn ensure_working_directory(path: String) -> Result<String, String> {
    let path = ensure_directory(Path::new(path.trim()))?;
    Ok(path.to_string_lossy().into_owned())
}

#[tauri::command]
fn allocate_work_file(
    working_directory: String,
    prefix: String,
    extension: String,
) -> Result<String, String> {
    let root = ensure_directory(Path::new(working_directory.trim()))?;
    let intermediate_dir = ensure_directory(&root.join("intermediates"))?;
    let safe_prefix: String = prefix
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '-' | '_') {
                c
            } else {
                '_'
            }
        })
        .collect();
    let safe_prefix = safe_prefix.trim_matches('_');
    let extension = extension.trim_start_matches('.');
    if extension.is_empty() || !extension.chars().all(|c| c.is_ascii_alphanumeric()) {
        return Err("Invalid working-file extension.".into());
    }
    let name = format!(
        "{}-{}.{}",
        if safe_prefix.is_empty() {
            "work"
        } else {
            safe_prefix
        },
        uuid::Uuid::new_v4(),
        extension.to_ascii_lowercase()
    );
    Ok(intermediate_dir.join(name).to_string_lossy().into_owned())
}

/// List every audio file directly inside each of the given directories.
///
/// Used by the background-music picker: the user manages a set of folders and
/// this returns the playable tracks across all of them, sorted by name. Missing
/// or unreadable directories are skipped rather than failing the whole call.
#[tauri::command]
fn list_audio_files(directories: Vec<String>) -> Result<Vec<AudioTrack>, String> {
    let mut tracks: Vec<AudioTrack> = Vec::new();
    let mut seen: HashSet<PathBuf> = HashSet::new();

    for dir in directories {
        let dir_path = PathBuf::from(&dir);
        if !dir_path.is_dir() {
            continue;
        }
        let entries = match fs::read_dir(&dir_path) {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_file() || !is_audio_file(&path) {
                continue;
            }
            let canonical = path.canonicalize().unwrap_or_else(|_| path.clone());
            if !seen.insert(canonical) {
                continue;
            }
            let name = path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_string();
            tracks.push(AudioTrack {
                path: path.to_string_lossy().to_string(),
                name,
                dir: dir.clone(),
            });
        }
    }

    tracks.sort_by(|a, b| a.name.to_ascii_lowercase().cmp(&b.name.to_ascii_lowercase()));
    Ok(tracks)
}

/// Grant the webview `stream://` access to one audio file so it can be played
/// as a live preview alongside the muted/native video audio.
#[tauri::command]
fn allow_audio_file<R: Runtime>(app: AppHandle<R>, path: String) -> Result<String, String> {
    let path = PathBuf::from(&path);
    if !path.is_file() {
        return Err("Audio file does not exist.".into());
    }
    if !is_audio_file(&path) {
        return Err("Selected file is not a supported audio format.".into());
    }
    grant_media_access(&app, &path)?;
    Ok(media_stream_url(path.to_string_lossy().to_string()))
}

/// Read a user-selected `.srt` file as UTF-8, tolerating a BOM.
///
/// This is the only path through which subtitle text reaches the frontend, so
/// it stays deliberately narrow: existing file, `.srt` extension, size cap.
#[tauri::command]
fn read_srt(path: String) -> Result<String, String> {
    let path = PathBuf::from(&path);

    if !path.is_file() {
        return Err("Subtitle file does not exist.".into());
    }
    if path.extension().and_then(|e| e.to_str()).map(|e| e.to_ascii_lowercase()) != Some("srt".into()) {
        return Err("Expected a .srt file.".into());
    }

    let metadata = fs::metadata(&path).map_err(|e| format!("Could not read subtitles: {e}"))?;
    if metadata.len() > MAX_SRT_BYTES {
        return Err("Subtitle file is larger than 10 MB.".into());
    }

    let bytes = fs::read(&path).map_err(|e| format!("Could not read subtitles: {e}"))?;
    let text = String::from_utf8(bytes).map_err(|_| "Subtitle file is not valid UTF-8.".to_string())?;
    Ok(text.strip_prefix('\u{feff}').unwrap_or(&text).to_string())
}

/// Allow one user-selected media file to be streamed for preview.
///
/// Extends both the asset-protocol scope and the `stream://` allow-list at
/// runtime so the webview can read the selected video and nothing else.
#[tauri::command]
fn allow_media_file<R: Runtime>(app: AppHandle<R>, path: String) -> Result<(), String> {
    let path = PathBuf::from(&path);
    if !path.is_file() {
        return Err("Selected file does not exist.".into());
    }
    // Canonicalize so the allow-list comparison in the protocol handler matches
    // regardless of how the path is later presented.
    let canonical = path
        .canonicalize()
        .map_err(|e| format!("Could not resolve file: {e}"))?;

    app.state::<MediaAccess>()
        .allowed
        .lock()
        .map_err(|_| "Media access state was poisoned.".to_string())?
        .insert(canonical.clone());

    app.asset_protocol_scope()
        .allow_file(&canonical)
        .map_err(|e| format!("Could not grant preview access: {e}"))
}

/// Build a `stream://localhost/<percent-encoded-abs-path>` URL for the webview.
///
/// Served by our custom protocol with full HTTP Range support, which WKWebView
/// needs to initialize the audio track (the asset protocol's range handling is
/// unreliable for some MP4s, yielding video-only playback).
#[tauri::command]
fn media_stream_url(path: String) -> String {
    let encoded = byte_percent_encode(path.as_bytes());
    format!("stream://localhost/{encoded}")
}

/// Transcode a source video into a WKWebView-friendly H.264/AAC MP4 and return
/// a `stream://` URL for it.
///
/// WKWebView (AVFoundation) only decodes a narrow codec set. Files using VP9,
/// AV1, unusual HEVC profiles, or non-AAC audio fail to preview (silently, on
/// the `<video>` element). The frontend calls this as a fallback when native
/// playback errors out: the result is a preview-only proxy, so rendering and
/// trimming still run against the original file.
///
/// Proxies are cached under the configured app working directory, keyed by the
/// source path + mtime + size, so re-loading the same file doesn't re-transcode.
#[tauri::command]
async fn transcode_preview<R: Runtime>(
    app: AppHandle<R>,
    path: String,
    working_directory: String,
) -> Result<String, String> {
    let input = PathBuf::from(&path);
    if !input.is_file() {
        return Err("Selected file does not exist.".into());
    }

    let canonical = input
        .canonicalize()
        .map_err(|e| format!("Could not resolve file: {e}"))?;
    let metadata = fs::metadata(&canonical).map_err(|e| format!("Could not read file: {e}"))?;
    let mtime = metadata
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs())
        .unwrap_or(0);

    // Stable cache key so the same source reuses an existing proxy.
    let key = cache_key(
        canonical.to_string_lossy().as_bytes(),
        mtime,
        metadata.len(),
    );
    let working_root = ensure_directory(Path::new(working_directory.trim()))?;
    let proxy_dir = ensure_directory(&working_root.join("previews"))?;
    // Version the cache name because older builds could leave a partial MP4 at
    // the final path and later mistake any non-empty file for a valid proxy.
    let proxy = proxy_dir.join(format!("v2-{key}.mp4"));
    let partial_proxy = proxy_dir.join(format!("v2-{key}.partial.mp4"));

    // Reuse a finished proxy from a previous run.
    if proxy.is_file() && fs::metadata(&proxy).map(|m| m.len() > 0).unwrap_or(false) {
        grant_media_access(&app, &proxy)?;
        return Ok(media_stream_url(proxy.to_string_lossy().to_string()));
    }
    // FFmpeg always writes to a temporary name. Only an atomic rename below
    // makes a proxy visible to the cache, so interrupted transcodes are never
    // reused as though they were complete.
    let _ = fs::remove_file(&partial_proxy);

    // H.264 + AAC in an MP4, capped at 1280px wide to keep the transcode quick.
    // faststart so the webview can begin playback before the file is complete.
    let sidecar = app
        .shell()
        .sidecar("ffmpeg")
        .map_err(|e| format!("Could not locate FFmpeg: {e}"))?
        .args([
            "-hide_banner",
            "-y",
            "-i",
            &canonical.to_string_lossy(),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            "scale='min(1280,iw)':-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            &partial_proxy.to_string_lossy(),
        ]);

    let result =
        run_ffmpeg_with_progress(&app, sidecar, &partial_proxy, None, "preview-progress").await;
    if let Err(error) = result {
        let _ = fs::remove_file(&partial_proxy);
        return Err(error);
    }
    fs::rename(&partial_proxy, &proxy)
        .map_err(|e| format!("Could not finalize preview proxy: {e}"))?;

    grant_media_access(&app, &proxy)?;
    Ok(media_stream_url(proxy.to_string_lossy().to_string()))
}

/// Add a canonical path to the `stream://` allow-list and asset-protocol scope.
fn grant_media_access<R: Runtime>(app: &AppHandle<R>, path: &Path) -> Result<(), String> {
    let canonical = path
        .canonicalize()
        .map_err(|e| format!("Could not resolve file: {e}"))?;
    app.state::<MediaAccess>()
        .allowed
        .lock()
        .map_err(|_| "Media access state was poisoned.".to_string())?
        .insert(canonical.clone());
    app.asset_protocol_scope()
        .allow_file(&canonical)
        .map_err(|e| format!("Could not grant preview access: {e}"))
}

/// Deterministic, filesystem-safe cache key (FNV-1a over the inputs). Avoids a
/// hashing dependency for what is only a temp-file naming scheme.
fn cache_key(path_bytes: &[u8], mtime: u64, size: u64) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    let mix = |hash: &mut u64, bytes: &[u8]| {
        for &b in bytes {
            *hash ^= b as u64;
            *hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
    };
    mix(&mut hash, path_bytes);
    mix(&mut hash, &mtime.to_le_bytes());
    mix(&mut hash, &size.to_le_bytes());
    format!("{hash:016x}")
}

/// Reveal a finished file in Finder.
#[tauri::command]
fn reveal_in_finder(path: String) -> Result<(), String> {
    let path = PathBuf::from(&path);
    if !path.exists() {
        return Err("File no longer exists.".into());
    }
    std::process::Command::new("open")
        .arg("-R")
        .arg(&path)
        .spawn()
        .map_err(|e| format!("Could not open Finder: {e}"))?;
    Ok(())
}

/// Write UTF-8 text to a user-chosen path. Used to persist the highlights JSON
/// returned by the "Clip it" tab.
#[tauri::command]
fn save_text_file(path: String, contents: String) -> Result<(), String> {
    let path = PathBuf::from(&path);
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent).map_err(|e| format!("Could not create folder: {e}"))?;
        }
    }
    fs::write(&path, contents).map_err(|e| format!("Could not write file: {e}"))?;
    Ok(())
}

fn extension_is(path: &Path, expected: &str) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| e.eq_ignore_ascii_case(expected))
        .unwrap_or(false)
}

/// Burn the supplied ASS subtitles into the input video with the bundled
/// FFmpeg sidecar. Progress is emitted on the `render-progress` channel.
#[tauri::command]
async fn render_video<R: Runtime>(
    app: AppHandle<R>,
    request: RenderRequest,
) -> Result<RenderResult, String> {
    let input = PathBuf::from(&request.input_path);
    let output = PathBuf::from(&request.output_path);

    if !input.is_file() {
        return Err("Input video does not exist.".into());
    }
    if !extension_is(&input, "mp4") {
        return Err("Input must be an .mp4 file.".into());
    }
    if !extension_is(&output, "mp4") {
        return Err("Output must be an .mp4 file.".into());
    }

    // Unique operation directory under the configured app working directory.
    // FFmpeg runs with this as its CWD so subtitle/font paths stay relative.
    let working_root = ensure_directory(Path::new(request.working_directory.trim()))?;
    let render_root = ensure_directory(&working_root.join("renders"))?;
    let work_dir = render_root.join(format!("ripe-clips-{}", uuid::Uuid::new_v4()));
    fs::create_dir_all(&work_dir).map_err(|e| format!("Could not create work dir: {e}"))?;

    // Everything below should clean up the work dir, so wrap the body and
    // remove the directory regardless of the outcome.
    let result = render_inner(&app, &request, &input, &output, &work_dir).await;
    let _ = fs::remove_dir_all(&work_dir);

    result.map(|_| RenderResult {
        output_path: request.output_path.clone(),
    })
}

/// Trim the input MP4 between two timestamps, re-encoding for frame-accurate
/// cuts: a fast seek to a keyframe ~10s before the start, then a precise seek
/// and duration limit, with a forced constant 30 fps GOP and
/// QuickTime-compatible output. When `portrait` is set, the trimmed clip is
/// pillarboxed onto a vertical 9:16 canvas (blurred fill or black bars) in the
/// same encode, so no second pass is needed.
#[tauri::command]
async fn trim_video<R: Runtime>(
    app: AppHandle<R>,
    request: TrimRequest,
) -> Result<TrimResult, String> {
    let input = PathBuf::from(&request.input_path);
    let output = PathBuf::from(&request.output_path);

    if !input.is_file() {
        return Err("Input video does not exist.".into());
    }
    if !extension_is(&input, "mp4") {
        return Err("Input must be an .mp4 file.".into());
    }
    if !extension_is(&output, "mp4") {
        return Err("Output must be an .mp4 file.".into());
    }

    let start = request.start_seconds;
    let end = request.end_seconds;
    if !start.is_finite() || !end.is_finite() || start < 0.0 {
        return Err("Invalid trim timestamps.".into());
    }
    if end <= start {
        return Err("End must be greater than start.".into());
    }

    let duration = end - start;
    // GOP-safe fast seek: jump to a keyframe up to 10s before the cut, then
    // trim the remainder precisely inside the filter graph. Keeping that
    // precise trim in the graph is important: subtitle events are evaluated
    // by the filter graph too, so its video timestamps must begin at zero.
    let seek_buffer = start.min(10.0);
    let fast_seek = (start - seek_buffer).max(0.0);
    let precise_seek = start - fast_seek;

    // Resolve an optional background-music track up front.
    let music = music_input(request.music_path.as_deref())?;
    let music_volume = normalize_music_volume(request.music_volume);

    // Optional 9:16 pillarbox in the same encode (Shorts/Reels/TikTok).
    let portrait = request.portrait;
    let out_w = request.out_width.unwrap_or(1080);
    let out_h = request.out_height.unwrap_or(1920);
    if portrait && (out_w == 0 || out_h == 0) {
        return Err("Portrait dimensions must be non-zero.".into());
    }
    let blurred = request
        .background
        .as_deref()
        .map(|b| b.eq_ignore_ascii_case("blur"))
        .unwrap_or(true);
    // Optional subtitle burn-in. When `ass_content` is supplied, set up a
    // per-operation work dir holding the ASS script + fonts; the subtitles
    // filter (added to the graph below) reads them relative to the sidecar's
    // CWD, and is applied after any pillarbox so captions sit on the final
    // canvas.
    let burn_subtitles = request
        .ass_content
        .as_ref()
        .map(|s| !s.trim().is_empty())
        .unwrap_or(false);

    let subtitle_work_dir = if burn_subtitles {
        let working = request
            .working_directory
            .as_ref()
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .ok_or("Working directory is required to burn captions.")?;
        let working_root = ensure_directory(Path::new(working))?;
        let render_root = ensure_directory(&working_root.join("renders"))?;
        let work_dir = render_root.join(format!("ripe-clips-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&work_dir).map_err(|e| format!("Could not create work dir: {e}"))?;

        // Rewrite the ASS `Fontname` to a custom font's real family when a
        // custom font file is used, so libass resolves it (mirrors render).
        let ass_source = request.ass_content.as_deref().unwrap_or_default();
        let ass_content = match request
            .font_path
            .as_ref()
            .filter(|p| !p.is_empty())
            .and_then(|p| read_font_family(Path::new(p)))
        {
            Some(family) => rewrite_ass_fontname(ass_source, &family),
            None => ass_source.to_string(),
        };
        fs::write(work_dir.join("subtitles.ass"), &ass_content)
            .map_err(|e| format!("Could not write subtitles: {e}"))?;

        let fonts_dir = work_dir.join("fonts");
        fs::create_dir_all(&fonts_dir).map_err(|e| format!("Could not create fonts dir: {e}"))?;
        copy_font(
            &app,
            request.font_path.as_ref(),
            request.bundled_font_name.as_ref(),
            &fonts_dir,
        )?;
        if let Some(dir) = request.font_dir.as_ref().filter(|p| !p.trim().is_empty()) {
            link_font_dir(Path::new(dir), &fonts_dir);
        }
        Some(work_dir)
    } else {
        None
    };

    // Compose the video filter graph. `trim` does the frame-accurate portion
    // after the GOP-safe input seek, and `setpts` makes the trimmed clip start
    // at zero before any subtitle event is evaluated. The final stage always
    // emits `[v]` for the mapping below. Order matters: trim/rebase first,
    // then reframe, then burn captions onto the final canvas.
    let precise_trim = format!(
        "[0:v:0]trim=start={}:duration={},setpts=PTS-STARTPTS[vt]",
        format_seconds(precise_seek),
        format_seconds(duration),
    );
    let video_chain = match (portrait, burn_subtitles) {
        (false, false) => format!("{precise_trim};[vt]null[v]"),
        // Pillarbox only: rewire pillarbox_chain from the trimmed video.
        (true, false) => format!(
            "{precise_trim};{}",
            pillarbox_chain(out_w, out_h, blurred).replacen("[0:v:0]", "[vt]", 1)
        ),
        // Subtitles only: burn straight onto the source video.
        (false, true) => format!(
            "{precise_trim};[vt]subtitles=filename=subtitles.ass:fontsdir=fonts[v]"
        ),
        // Both: pillarbox to an intermediate label, then subtitles onto [v].
        // pillarbox_chain emits its result exactly once as the trailing `[v]`
        // label, so renaming it to `[vp]` just re-wires that output.
        (true, true) => {
            let reframe = pillarbox_chain(out_w, out_h, blurred)
                .replacen("[0:v:0]", "[vt]", 1)
                .replace("[v]", "[vp]");
            format!(
                "{precise_trim};{reframe};[vp]subtitles=filename=subtitles.ass:fontsdir=fonts[v]"
            )
        }
    };

    let mut args: Vec<String> = vec![
        "-hide_banner".into(),
        "-ss".into(),
        format_seconds(fast_seek),
        "-i".into(),
        input.to_string_lossy().into_owned(),
        // Open the source a second time for audio at the requested clip start.
        // This keeps `0:a?` optional while avoiding an output-side `-ss`, which
        // would otherwise leave the subtitle filter on the pre-trim timeline.
        "-ss".into(),
        format_seconds(start),
        "-i".into(),
        input.to_string_lossy().into_owned(),
    ];

    // Second input: the looped music bed (no seek; looped indefinitely and
    // trimmed by the output duration / amix below).
    if let Some(ref music_path) = music {
        args.push("-stream_loop".into());
        args.push("-1".into());
        args.push("-i".into());
        args.push(music_path.to_string_lossy().into_owned());
    }

    // Bound the final mux to the requested duration. The video graph already
    // has the same limit; this also bounds the separately-opened audio/mix.
    args.extend(["-t".into(), format_seconds(duration)]);

    // Build audio + video mapping. The video graph is always present because it
    // owns the frame-accurate trim and timestamp reset. The second input holds
    // the clip audio; the optional music input is third.
    let audio_chain = music.as_ref().map(|_| {
        // Mix the clip's own audio with the volume-scaled music bed. duration=first
        // keeps the result as long as the clip audio (already bounded by -t), and
        // normalize=0 stops amix from attenuating the original speech.
        format!(
            "[2:a]volume={music_volume}[bg];[1:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    });

    let mut graph_parts: Vec<String> = vec![video_chain];
    if let Some(ref a) = audio_chain {
        graph_parts.push(a.clone());
    }
    args.push("-filter_complex".into());
    args.push(graph_parts.join(";"));

    args.push("-map".into());
    args.push("[v]".into());
    args.push("-map".into());
    if audio_chain.is_some() {
        args.push("[aout]".into());
    } else {
        args.push("1:a:0?".into());
    }

    args.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-x264-params",
            "keyint=30:min-keyint=30:no-scenecut:bframes=0",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-vsync",
            "cfr",
            "-r",
            "30",
            "-progress",
            "pipe:1",
            "-nostats",
            "-y",
        ]
        .iter()
        .map(|s| s.to_string()),
    );
    args.push(output.to_string_lossy().into_owned());

    let mut sidecar = app
        .shell()
        .sidecar("ffmpeg")
        .map_err(|e| format!("Could not locate FFmpeg: {e}"))?;
    // When burning captions, run with the work dir as CWD so the relative
    // `subtitles.ass` / `fonts` paths in the filter graph resolve.
    if let Some(ref work_dir) = subtitle_work_dir {
        sidecar = sidecar.current_dir(work_dir.clone());
    }
    let sidecar = sidecar.args(args);

    let result =
        run_ffmpeg_with_progress(&app, sidecar, &output, Some(duration), "trim-progress").await;

    // Clean up the subtitle work dir regardless of outcome.
    if let Some(work_dir) = subtitle_work_dir {
        let _ = fs::remove_dir_all(&work_dir);
    }

    result?;

    Ok(TrimResult {
        output_path: request.output_path.clone(),
    })
}

/// Extract the audio track of an MP4 into an MP3 with the bundled FFmpeg
/// sidecar (equivalent to `ffmpeg -i input.mp4 output.mp3`). Used by the
/// "Generate SRT" tab as the first step before transcription.
#[tauri::command]
async fn extract_audio<R: Runtime>(
    app: AppHandle<R>,
    request: ExtractAudioRequest,
) -> Result<ExtractAudioResult, String> {
    let input = PathBuf::from(&request.input_path);
    let output = PathBuf::from(&request.output_path);

    if !input.is_file() {
        return Err("Input video does not exist.".into());
    }
    if !extension_is(&input, "mp4") {
        return Err("Input must be an .mp4 file.".into());
    }

    let sidecar = app
        .shell()
        .sidecar("ffmpeg")
        .map_err(|e| format!("Could not locate FFmpeg: {e}"))?
        .args([
            "-hide_banner",
            "-y",
            "-i",
            &input.to_string_lossy(),
            // Drop any video stream; encode audio to MP3.
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            "-progress",
            "pipe:1",
            "-nostats",
            &output.to_string_lossy(),
        ]);

    run_ffmpeg_with_progress(&app, sidecar, &output, None, "extract-audio-progress").await?;

    Ok(ExtractAudioResult {
        output_path: request.output_path.clone(),
    })
}

/// Pillarbox filter chain from `[0:v:0]` to a `[v]` output label: the whole
/// frame scaled to fit the canvas and centered over either a blurred, enlarged
/// copy of the frame (`blurred`) or solid black bars. Used by the Edit tab's
/// optional "Save as 9:16" path.
fn pillarbox_chain(out_w: u32, out_h: u32, blurred: bool) -> String {
    if blurred {
        // Background: enlarge to cover the canvas, then blur it heavily (a
        // larger sigma plus extra passes for a soft, frosted look that doesn't
        // distract from the centered foreground). Foreground: the whole frame
        // fit to the canvas, overlaid centered.
        format!(
            "[0:v:0]split=2[bgp][fgp];\
             [bgp]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,\
             crop={out_w}:{out_h},gblur=sigma=60:steps=3[bgbp];\
             [fgp]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fgsp];\
             [bgbp][fgsp]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
        )
    } else {
        format!(
            "[0:v:0]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,\
             pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v]"
        )
    }
}

/// POST an audio file to a local Parakeet-compatible transcription endpoint and
/// return the raw verbose_json response as a string. Mirrors:
///   curl <base>/v1/audio/transcriptions \
///     -F "file=@clip.mp3" -F "model=<model>" \
///     -F "response_format=verbose_json" -F "timestamp_granularities=word"
///
/// The audio file is read directly from disk in Rust so large uploads never
/// cross the JS bridge.
#[tauri::command]
async fn transcribe_audio(request: TranscribeRequest) -> Result<String, String> {
    let audio = PathBuf::from(&request.audio_path);
    if !audio.is_file() {
        return Err("Audio file does not exist.".into());
    }

    let bytes = fs::read(&audio).map_err(|e| format!("Could not read audio: {e}"))?;
    let file_name = audio
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("audio.mp3")
        .to_string();

    let base = request.base_url.trim_end_matches('/');
    let url = format!("{base}/v1/audio/transcriptions");

    let part = reqwest::multipart::Part::bytes(bytes)
        .file_name(file_name)
        .mime_str("audio/mpeg")
        .map_err(|e| format!("Could not build upload: {e}"))?;
    let form = reqwest::multipart::Form::new()
        .part("file", part)
        .text("model", request.model)
        .text("response_format", "verbose_json")
        .text("timestamp_granularities", "word");

    let client = reqwest::Client::new();
    let response = client
        .post(&url)
        .multipart(form)
        .send()
        .await
        .map_err(|e| format!("Transcription request failed: {e}"))?;

    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|e| format!("Could not read transcription response: {e}"))?;
    if !status.is_success() {
        return Err(format!("Transcription HTTP {status}: {body}"));
    }
    Ok(body)
}

/// Delete a file. Used to clean up the intermediate MP3 and JSON the
/// "Generate SRT" pipeline creates. Missing files are treated as success.
#[tauri::command]
fn delete_file(path: String) -> Result<(), String> {
    let path = PathBuf::from(&path);
    if !path.exists() {
        return Ok(());
    }
    fs::remove_file(&path).map_err(|e| format!("Could not delete file: {e}"))
}

async fn render_inner<R: Runtime>(
    app: &AppHandle<R>,
    request: &RenderRequest,
    input: &Path,
    output: &Path,
    work_dir: &Path,
) -> Result<(), String> {
    // 1. Write the subtitle script. When a custom font file is used, the ASS
    // `Fontname` (chosen from the bundled dropdown on the frontend) won't match
    // the custom file's real family name, so libass would fall back to a
    // default face. Rewrite the style's Fontname to the custom font's actual
    // family so libass resolves it against the copied file.
    let ass_path = work_dir.join("subtitles.ass");
    let ass_content = match request
        .font_path
        .as_ref()
        .filter(|p| !p.is_empty())
        .and_then(|p| read_font_family(Path::new(p)))
    {
        Some(family) => rewrite_ass_fontname(&request.ass_content, &family),
        None => request.ass_content.clone(),
    };
    fs::write(&ass_path, &ass_content)
        .map_err(|e| format!("Could not write subtitles: {e}"))?;

    // 2. Provide the font inside a fonts/ directory next to the script.
    let fonts_dir = work_dir.join("fonts");
    fs::create_dir_all(&fonts_dir).map_err(|e| format!("Could not create fonts dir: {e}"))?;
    copy_font(
        app,
        request.font_path.as_ref(),
        request.bundled_font_name.as_ref(),
        &fonts_dir,
    )?;

    // Also expose any fonts from the user-selected font directory so custom
    // font names referenced by the subtitle style resolve during the burn.
    if let Some(dir) = request.font_dir.as_ref().filter(|p| !p.trim().is_empty()) {
        link_font_dir(Path::new(dir), &fonts_dir);
    }

    // 3. Build and run the sidecar.
    let duration = request.duration_seconds.filter(|d| d.is_finite() && *d > 0.0);

    let music = music_input(request.music_path.as_deref())?;
    let music_volume = normalize_music_volume(request.music_volume);

    let mut args: Vec<String> = vec![
        "-y".into(),
        "-i".into(),
        input.to_string_lossy().into_owned(),
    ];

    // Optional looped music bed as a second input.
    if let Some(ref music_path) = music {
        args.push("-stream_loop".into());
        args.push("-1".into());
        args.push("-i".into());
        args.push(music_path.to_string_lossy().into_owned());
    }

    if music.is_some() {
        // Burn subtitles on the video and mix the music bed under the clip's
        // own audio. duration=first bounds the mix to the video length.
        args.push("-filter_complex".into());
        args.push(format!(
            "[0:v:0]subtitles=filename=subtitles.ass:fontsdir=fonts[v];\
             [1:a]volume={music_volume}[bg];\
             [0:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        ));
        args.push("-map".into());
        args.push("[v]".into());
        args.push("-map".into());
        args.push("[aout]".into());
        args.extend(["-c:a", "aac", "-b:a", "192k"].iter().map(|s| s.to_string()));
    } else {
        args.push("-filter_complex".into());
        args.push("[0:v:0]subtitles=filename=subtitles.ass:fontsdir=fonts[v]".into());
        args.push("-map".into());
        args.push("[v]".into());
        args.push("-map".into());
        args.push("0:a?".into());
        args.push("-c:a".into());
        args.push("copy".into());
    }

    args.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
        ]
        .iter()
        .map(|s| s.to_string()),
    );
    args.push(output.to_string_lossy().into_owned());

    let sidecar = app
        .shell()
        .sidecar("ffmpeg")
        .map_err(|e| format!("Could not locate FFmpeg: {e}"))?
        .current_dir(work_dir.to_path_buf())
        .args(args);

    run_ffmpeg_with_progress(app, sidecar, output, duration, "render-progress").await
}

/// Spawn a prepared FFmpeg sidecar command, forward `-progress` output as a
/// 0..1 fraction on `event_name`, and surface a useful error on failure.
async fn run_ffmpeg_with_progress<R: Runtime>(
    app: &AppHandle<R>,
    sidecar: tauri_plugin_shell::process::Command,
    output: &Path,
    duration: Option<f64>,
    event_name: &str,
) -> Result<(), String> {
    let (mut rx, _child) = sidecar
        .spawn()
        .map_err(|e| format!("Could not start FFmpeg: {e}"))?;

    let mut stderr_tail: Vec<String> = Vec::new();
    // Fall back to the duration FFmpeg prints in stderr when the caller can't
    // supply one (e.g. audio extraction, where the frontend has no duration).
    let mut duration = duration;

    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(line) => {
                let text = String::from_utf8_lossy(&line);
                if let Some(progress) = parse_progress(&text, duration) {
                    let _ = app.emit(event_name, progress);
                }
            }
            CommandEvent::Stderr(line) => {
                let text = String::from_utf8_lossy(&line).trim().to_string();
                if !text.is_empty() {
                    if duration.is_none() {
                        if let Some(found) = parse_ffmpeg_duration(&text) {
                            duration = Some(found);
                        }
                    }
                    stderr_tail.push(text);
                    if stderr_tail.len() > 12 {
                        stderr_tail.remove(0);
                    }
                }
            }
            CommandEvent::Terminated(payload) => {
                if payload.code != Some(0) {
                    let detail = stderr_tail
                        .iter()
                        .rev()
                        .find(|l| !l.starts_with("frame="))
                        .cloned()
                        .unwrap_or_else(|| "no error output".into());
                    return Err(format!(
                        "FFmpeg exited with code {:?}: {detail}",
                        payload.code
                    ));
                }
            }
            CommandEvent::Error(err) => {
                return Err(format!("FFmpeg error: {err}"));
            }
            _ => {}
        }
    }

    if !output.is_file() {
        return Err("FFmpeg finished but produced no output file.".into());
    }

    let _ = app.emit(event_name, 1.0_f64);
    Ok(())
}

/// Replace the `Fontname` field of the ASS `Style: Default,` line with `family`.
///
/// The ASS style Format is
/// `Name,Fontname,Fontsize,...`, so on a `Style:` line the font name is the
/// second comma-separated field (index 1). Only that field is touched; every
/// other field and every other line is passed through unchanged.
fn rewrite_ass_fontname(ass: &str, family: &str) -> String {
    let mut out = String::with_capacity(ass.len() + family.len());
    for (i, line) in ass.lines().enumerate() {
        if i > 0 {
            out.push('\n');
        }
        if let Some(rest) = line.strip_prefix("Style: ") {
            let mut fields: Vec<&str> = rest.split(',').collect();
            if fields.len() > 1 {
                fields[1] = family;
                out.push_str("Style: ");
                out.push_str(&fields.join(","));
                continue;
            }
        }
        out.push_str(line);
    }
    // Preserve a trailing newline if the original had one (lines() drops it).
    if ass.ends_with('\n') {
        out.push('\n');
    }
    out
}

/// Read a font's real family name from its `name` table so it can be used as
/// the ASS `Fontname` (what libass/fontconfig match against). Prefers the
/// typographic family (name ID 16), falling back to the legacy family
/// (name ID 1). Returns `None` if the file can't be parsed. This mirrors the
/// build-time parser in `scripts/fonts-plugin.js` so bundled and custom fonts
/// resolve identically.
fn read_font_family(path: &Path) -> Option<String> {
    let buffer = fs::read(path).ok()?;

    // A TrueType Collection points at its first font's table directory at
    // offset 12; a plain sfnt starts its directory at offset 0.
    let read_u16 = |off: usize| -> Option<u16> {
        buffer
            .get(off..off + 2)
            .map(|b| u16::from_be_bytes([b[0], b[1]]))
    };
    let read_u32 = |off: usize| -> Option<u32> {
        buffer
            .get(off..off + 4)
            .map(|b| u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    };

    let sfnt_offset = if buffer.get(0..4) == Some(b"ttcf") {
        read_u32(12)? as usize
    } else {
        0
    };

    let num_tables = read_u16(sfnt_offset + 4)? as usize;
    let records_start = sfnt_offset + 12;
    let mut name_table_offset = None;
    for i in 0..num_tables {
        let record = records_start + i * 16;
        if buffer.get(record..record + 4) == Some(b"name") {
            name_table_offset = Some(read_u32(record + 8)? as usize);
            break;
        }
    }
    let name_table_offset = name_table_offset?;

    let count = read_u16(name_table_offset + 2)? as usize;
    let string_storage = name_table_offset + read_u16(name_table_offset + 4)? as usize;

    // Track the best candidate for name IDs 16 (typographic) and 1 (legacy),
    // preferring a Windows/English (platform 3, language 0x409) record.
    let mut best_16: Option<(String, bool)> = None;
    let mut best_1: Option<(String, bool)> = None;
    for i in 0..count {
        let rec = name_table_offset + 6 + i * 12;
        let platform_id = read_u16(rec)?;
        let language_id = read_u16(rec + 4)?;
        let name_id = read_u16(rec + 6)?;
        if name_id != 16 && name_id != 1 {
            continue;
        }
        let length = read_u16(rec + 8)? as usize;
        let offset = string_storage + read_u16(rec + 10)? as usize;
        let bytes = buffer.get(offset..offset + length)?;
        let value = decode_name_record(bytes, platform_id);
        let value = value.trim().to_string();
        if value.is_empty() {
            continue;
        }
        let preferred = platform_id == 3 && language_id == 0x409;
        let slot = if name_id == 16 { &mut best_16 } else { &mut best_1 };
        if slot.is_none() || (preferred && !slot.as_ref().unwrap().1) {
            *slot = Some((value, preferred));
        }
    }

    best_16.or(best_1).map(|(value, _)| value)
}

/// Decode a `name` record's bytes into a String. Windows (platform 3) and
/// Unicode (platform 0) records are UTF-16BE; other platforms (e.g. Mac) are
/// treated as Latin-1, which covers the ASCII family names these fonts use.
fn decode_name_record(bytes: &[u8], platform_id: u16) -> String {
    if platform_id == 3 || platform_id == 0 {
        let units: Vec<u16> = bytes
            .chunks_exact(2)
            .map(|c| u16::from_be_bytes([c[0], c[1]]))
            .collect();
        String::from_utf16_lossy(&units)
    } else {
        bytes.iter().map(|&b| b as char).collect()
    }
}

/// Copy either the user-selected custom font or the requested bundled font into
/// the working `fonts/` directory under a stable name.
fn copy_font<R: Runtime>(
    app: &AppHandle<R>,
    font_path: Option<&String>,
    bundled_font_name: Option<&String>,
    fonts_dir: &Path,
) -> Result<(), String> {
    if let Some(custom) = font_path.filter(|p| !p.is_empty()) {
        let source = PathBuf::from(custom);
        if !source.is_file() {
            return Err("Selected font file does not exist.".into());
        }
        let ext = source
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_ascii_lowercase())
            .filter(|e| matches!(e.as_str(), "ttf" | "otf" | "ttc"))
            .unwrap_or_else(|| "ttf".into());
        fs::copy(&source, fonts_dir.join(format!("custom.{ext}")))
            .map_err(|e| format!("Could not copy custom font: {e}"))?;
        return Ok(());
    }

    let name = bundled_font_name
        .filter(|n| !n.is_empty())
        .ok_or("No font specified.")?;

    if Path::new(name).file_name().and_then(|n| n.to_str()) != Some(name.as_str()) {
        return Err("Invalid bundled font name.".into());
    }

    // Bundled fonts ship as Tauri resources under fonts/. Verify the selected
    // face, then expose every bundled variant so libass can honor bold/italic.
    let resource_dir = app
        .path()
        .resolve("fonts", BaseDirectory::Resource)
        .map_err(|e| format!("Could not locate bundled font: {e}"))?;
    if !resource_dir.join(name).is_file() {
        return Err(format!("Bundled font {name} is missing."));
    }

    let entries = fs::read_dir(&resource_dir)
        .map_err(|e| format!("Could not read bundled fonts: {e}"))?;
    for entry in entries.flatten() {
        let source = entry.path();
        let is_font = source
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_ascii_lowercase())
            .is_some_and(|e| matches!(e.as_str(), "ttf" | "otf" | "ttc"));
        if !is_font {
            continue;
        }
        let Some(file_name) = source.file_name() else {
            continue;
        };
        fs::copy(&source, fonts_dir.join(file_name))
            .map_err(|e| format!("Could not copy bundled font: {e}"))?;
    }
    Ok(())
}

/// Make every font in the user-selected directory discoverable to libass by
/// symlinking each font file into the working `fonts/` dir (which is passed to
/// the subtitles filter via `fontsdir`). Symlinks keep this instant even for
/// large system font folders. Non-font files and unreadable entries are
/// skipped silently; a missing directory is not an error.
fn link_font_dir(font_dir: &Path, fonts_dir: &Path) {
    if !font_dir.is_dir() {
        return;
    }
    let Ok(entries) = fs::read_dir(font_dir) else {
        return;
    };
    for entry in entries.flatten() {
        let source = entry.path();
        let is_font = source
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_ascii_lowercase())
            .is_some_and(|e| matches!(e.as_str(), "ttf" | "otf" | "ttc"));
        if !is_font {
            continue;
        }
        let Some(file_name) = source.file_name() else {
            continue;
        };
        let dest = fonts_dir.join(file_name);
        if dest.exists() {
            continue;
        }
        // A failed link for one font shouldn't abort the whole render.
        let _ = std::os::unix::fs::symlink(&source, &dest);
    }
}

/// Validate an optional background-music path and return it as a `PathBuf`.
/// An empty/None path means "no music" and yields `Ok(None)`.
fn music_input(path: Option<&str>) -> Result<Option<PathBuf>, String> {
    let raw = match path {
        Some(p) if !p.trim().is_empty() => p,
        _ => return Ok(None),
    };
    let music = PathBuf::from(raw);
    if !music.is_file() {
        return Err("Background music file does not exist.".into());
    }
    if !is_audio_file(&music) {
        return Err("Background music must be a supported audio file.".into());
    }
    Ok(Some(music))
}

/// Clamp the requested music gain to a sane 0..0.5 range (UI max is 50%),
/// defaulting to 10% when unset or invalid.
fn normalize_music_volume(volume: Option<f64>) -> f64 {
    match volume {
        Some(v) if v.is_finite() => v.clamp(0.0, 0.5),
        _ => 0.1,
    }
}

/// Format seconds for FFmpeg's `-ss`/`-t` args: fixed 6-decimal precision with
/// trailing zeros (and a trailing dot) trimmed.
fn format_seconds(seconds: f64) -> String {
    let text = format!("{seconds:.6}");
    let trimmed = text.trim_end_matches('0').trim_end_matches('.');
    if trimmed.is_empty() { "0".to_string() } else { trimmed.to_string() }
}

/// Parse a `Duration: HH:MM:SS.xx` line from FFmpeg's stderr into seconds.
/// FFmpeg prints this once near the start when it opens the input.
fn parse_ffmpeg_duration(line: &str) -> Option<f64> {
    let idx = line.find("Duration:")?;
    let rest = line[idx + "Duration:".len()..].trim_start();
    let stamp = rest.split(',').next()?.trim();
    if stamp.starts_with("N/A") {
        return None;
    }
    let mut parts = stamp.split(':');
    let hours: f64 = parts.next()?.trim().parse().ok()?;
    let minutes: f64 = parts.next()?.trim().parse().ok()?;
    let seconds: f64 = parts.next()?.trim().parse().ok()?;
    let total = hours * 3600.0 + minutes * 60.0 + seconds;
    if total.is_finite() && total > 0.0 {
        Some(total)
    } else {
        None
    }
}

/// Parse `out_time_ms=` from an FFmpeg `-progress` block into a 0..1 fraction.
fn parse_progress(chunk: &str, duration: Option<f64>) -> Option<f64> {
    let duration = duration?;
    let mut latest = None;
    for line in chunk.lines() {
        if let Some(value) = line.strip_prefix("out_time_ms=") {
            if let Ok(micros) = value.trim().parse::<i64>() {
                // out_time_ms is reported in microseconds despite the name.
                latest = Some((micros as f64 / 1_000_000.0) / duration);
            }
        } else if line.trim() == "progress=end" {
            latest = Some(1.0);
        }
    }
    latest.map(|p| p.clamp(0.0, 1.0))
}

/// Percent-encode every byte that isn't an unreserved URL character. Keeps the
/// path round-trippable through the `stream://` URL regardless of spaces or
/// non-ASCII characters (common in downloaded podcast filenames).
fn byte_percent_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len());
    for &b in bytes {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char);
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Inverse of `byte_percent_encode`.
fn percent_decode(input: &str) -> Vec<u8> {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hi = (bytes[i + 1] as char).to_digit(16);
            let lo = (bytes[i + 2] as char).to_digit(16);
            if let (Some(hi), Some(lo)) = (hi, lo) {
                out.push((hi * 16 + lo) as u8);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    out
}

fn stream_path(uri_path: &str) -> Option<PathBuf> {
    // Remove exactly the URI path separator. Removing every leading slash
    // turns an absolute `/Users/...` path into a relative `Users/...` path.
    let encoded = uri_path.strip_prefix('/')?;
    String::from_utf8(percent_decode(encoded))
        .ok()
        .map(PathBuf::from)
}

/// Guess a content type from the file extension. Only the handful of container
/// types the preview ever loads need to be covered.
fn mime_for(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).map(|e| e.to_ascii_lowercase()).as_deref() {
        Some("mp4") | Some("m4v") => "video/mp4",
        Some("mov") => "video/quicktime",
        Some("webm") => "video/webm",
        Some("mkv") => "video/x-matroska",
        Some("m4a") => "audio/mp4",
        Some("mp3") => "audio/mpeg",
        Some("wav") => "audio/wav",
        Some("flac") => "audio/flac",
        Some("ogg") | Some("opus") => "audio/ogg",
        Some("aac") => "audio/aac",
        Some("aiff") => "audio/aiff",
        _ => "application/octet-stream",
    }
}

/// Parse a single-range `Range: bytes=start-end` header into byte offsets.
/// Returns the inclusive `(start, end)` clamped to `len`. Suffix ranges
/// (`bytes=-N`) and open-ended ranges (`bytes=N-`) are both handled.
fn parse_range(header: &str, len: u64) -> Option<(u64, u64)> {
    let spec = header.trim().strip_prefix("bytes=")?;
    // Only the first range of a possibly-multi-range header is served.
    let spec = spec.split(',').next()?.trim();
    let (start_str, end_str) = spec.split_once('-')?;

    if start_str.is_empty() {
        // Suffix: last N bytes.
        let n: u64 = end_str.parse().ok()?;
        if n == 0 || len == 0 {
            return Some((0, len.saturating_sub(1)));
        }
        let start = len.saturating_sub(n);
        return Some((start, len - 1));
    }

    let start: u64 = start_str.parse().ok()?;
    let end = if end_str.is_empty() {
        len.saturating_sub(1)
    } else {
        end_str.parse::<u64>().ok()?.min(len.saturating_sub(1))
    };
    if start > end {
        return None;
    }
    Some((start, end))
}

/// Serve a user-selected media file over `stream://` with proper HTTP Range
/// support. WKWebView relies on range responses to set up the audio track; the
/// built-in asset protocol's range handling is unreliable for some MP4s, which
/// leaves them playing video without sound. This handler always advertises
/// `Accept-Ranges` and answers 206 for range requests.
fn build_media_response<R: Runtime>(app: &AppHandle<R>, request: &Request<Vec<u8>>) -> Response<Vec<u8>> {
    let not_found = || {
        Response::builder()
            .status(StatusCode::NOT_FOUND)
            .body(Vec::new())
            .unwrap()
    };

    // URL shape: stream://localhost/<percent-encoded-abs-path>
    let uri = request.uri();
    let path = match stream_path(uri.path()) {
        Some(path) => path,
        None => return not_found(),
    };

    // Only files the user explicitly selected may be read.
    let canonical = match path.canonicalize() {
        Ok(p) => p,
        Err(_) => return not_found(),
    };
    let permitted = app
        .state::<MediaAccess>()
        .allowed
        .lock()
        .map(|set| set.contains(&canonical))
        .unwrap_or(false);
    if !permitted {
        return Response::builder()
            .status(StatusCode::FORBIDDEN)
            .body(Vec::new())
            .unwrap();
    }

    let mut file = match fs::File::open(&canonical) {
        Ok(f) => f,
        Err(_) => return not_found(),
    };
    let total = match file.metadata() {
        Ok(m) => m.len(),
        Err(_) => return not_found(),
    };
    let mime = mime_for(&canonical);

    let range_header = request
        .headers()
        .get("range")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());

    match range_header.and_then(|h| parse_range(&h, total)) {
        Some((start, end_req)) => {
            // Cap the served slice so a single response can't be unbounded.
            let end = end_req.min(start + STREAM_CHUNK - 1).min(total.saturating_sub(1));
            let length = end - start + 1;

            let mut buffer = vec![0u8; length as usize];
            if file.seek(SeekFrom::Start(start)).is_err() {
                return not_found();
            }
            if file.read_exact(&mut buffer).is_err() {
                return not_found();
            }

            Response::builder()
                .status(StatusCode::PARTIAL_CONTENT)
                .header("Content-Type", mime)
                .header("Accept-Ranges", "bytes")
                .header("Content-Range", format!("bytes {start}-{end}/{total}"))
                .header("Content-Length", length.to_string())
                .body(buffer)
                .unwrap_or_else(|_| not_found())
        }
        None => {
            // No (or unsatisfiable) range: return the whole file.
            let mut buffer = Vec::with_capacity(total as usize);
            if file.read_to_end(&mut buffer).is_err() {
                return not_found();
            }
            Response::builder()
                .status(StatusCode::OK)
                .header("Content-Type", mime)
                .header("Accept-Ranges", "bytes")
                .header("Content-Length", total.to_string())
                .body(buffer)
                .unwrap_or_else(|_| not_found())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{byte_percent_encode, parse_range, stream_path};
    use std::path::PathBuf;

    #[test]
    fn stream_url_round_trips_an_absolute_path() {
        let path = "/Users/example/Videos/a clip #1.mp4";
        let uri_path = format!("/{}", byte_percent_encode(path.as_bytes()));

        assert_eq!(stream_path(&uri_path), Some(PathBuf::from(path)));
        assert!(uri_path.starts_with("/%2FUsers"));
    }

    #[test]
    fn stream_path_preserves_legacy_double_slash_urls() {
        assert_eq!(
            stream_path("//Users/example/video.mp4"),
            Some(PathBuf::from("/Users/example/video.mp4"))
        );
    }

    #[test]
    fn parses_open_ended_and_suffix_ranges() {
        assert_eq!(parse_range("bytes=100-", 1000), Some((100, 999)));
        assert_eq!(parse_range("bytes=-100", 1000), Some((900, 999)));
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_http::init())
        .manage(MediaAccess::default())
        .register_asynchronous_uri_scheme_protocol("stream", |ctx, request, responder| {
            let app = ctx.app_handle().clone();
            // Read off the main thread so large seeks never stutter playback.
            std::thread::spawn(move || {
                responder.respond(build_media_response(&app, &request));
            });
        })
        .invoke_handler(tauri::generate_handler![
            default_working_directory,
            ensure_working_directory,
            allocate_work_file,
            read_srt,
            allow_media_file,
            allow_audio_file,
            list_audio_files,
            media_stream_url,
            transcode_preview,
            reveal_in_finder,
            render_video,
            trim_video,
            extract_audio,
            transcribe_audio,
            delete_file,
            save_text_file
        ])
        .run(tauri::generate_context!())
        .expect("error while running RipeClips");
}
