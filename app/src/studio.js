import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open, save } from '@tauri-apps/plugin-dialog';
import {
  DISCOVERY_MODE_PRESETS,
  DEFAULT_BASE_URL,
  runIdentification,
  slugify,
  parseTopics,
} from './identifyApi.js';
import { timestamp } from './datetime.js';
import { jsonToSrt, GENERATE_DEFAULTS } from './generate.js';
import { parseSrt, createAss } from './ass.js';
import { createStyleController } from './subtitleStyle.js';
import {
  ASS_MARGIN_X,
  ASS_TO_CSS_FONT_SCALE,
  COLORS,
  FONTS,
  VARIATIONS,
  DEFAULT_FONT_FAMILY,
  fontIndexByFamily,
  populateVariationSelect,
} from './presets.js';

// User preference: whether the floating clip transcript panel is shown.
const TRANSCRIPT_PREF_KEY = 'ripe-clips-transcript-open';

// Transcription defaults for the Clip it (mp4-only) flow, matching the
// Generate SRT tab. The SRT is produced with 6 words per line over 4 rows.
const TRANSCRIBE_BASE_URL = 'http://localhost:5092';
const TRANSCRIBE_MODEL = 'parakeet-tdt-0.6b-v3';
const CLIPIT_SRT_OPTIONS = { ...GENERATE_DEFAULTS, maxWordsPerLine: 6, rows: 4 };

// libass resolves font names against the macOS system font folder during the
// burn, so a chosen custom font file can map to its real family name.
const SYSTEM_FONT_DIR = '/System/Library/Fonts';

// Default subtitle style for burned-in captions on generated clips. Matches the
// Burn/Caption tabs' defaults (orange text, standard outline, 20% up).
const CAPTION_STYLE_DEFAULTS = {
  colorIndex: 7, // Orange
  fontIndex: fontIndexByFamily(DEFAULT_FONT_FAMILY), // Lato
  variationIndex: 2, // Standard outline
  outlineColorIndex: 1, // Black
  fontSize: 72,
  letterSpacing: 0,
  yPosition: 20, // 20% up from the bottom
};

// Caption cue shaping defaults for the Clip it captioning step: short, single
// lines that read well on vertical clips (4 words per line, 1 row).
const CAPTION_CUE_DEFAULTS = { ...GENERATE_DEFAULTS, maxWordsPerLine: 4, rows: 1 };

// Minimum distance, in seconds, kept between the start and end markers so the
// loop range can never collapse to zero (or invert).
const MIN_RANGE = 0.1;
// Tightest zoom: the main timeline never shows less than this many seconds.
const MIN_VIEW_SPAN = 0.5;
// Candidate ruler intervals (seconds). The smallest one giving a sensible tick
// count for the current view span is chosen.
const TICK_STEPS = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];

// Default values for the highlight-identification knobs.
const IDENTIFY_DEFAULTS = {
  minDuration: 15,
  maxDuration: 60,
  candidates: 3,
};
const LANGUAGE = 'en';
// Ordered pipeline stages reflected in the identify tracker rail. The mp4-only
// "Clip it" flow prepends a transcription stage before the identify stages.
const IDENTIFY_STAGES = ['submit', 'queue', 'process', 'done'];
const CLIPIT_STAGES = ['transcribe', ...IDENTIFY_STAGES];
const GENERATE_STAGES = ['prepare', 'generate', 'done'];
// Stages for the single-selection save/render tracker.
const SAVE_STAGES = ['prepare', 'render', 'done'];

const baseName = (path) => path.split(/[\\/]/).pop() || path;
const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));

// Render seconds as M:SS.mmm (or H:MM:SS.mmm past an hour) for the inputs.
function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const millis = String(Math.round((seconds % 1) * 1000)).padStart(3, '0');
  const whole = Math.floor(seconds);
  const hrs = Math.floor(whole / 3600);
  const mins = Math.floor((whole % 3600) / 60);
  const secs = String(whole % 60).padStart(2, '0');
  if (hrs > 0) {
    return `${hrs}:${String(mins).padStart(2, '0')}:${secs}.${millis}`;
  }
  return `${mins}:${secs}.${millis}`;
}

// Shorter label for ruler ticks: M:SS, or H:MM:SS past an hour.
function formatTick(seconds) {
  const whole = Math.round(seconds);
  const hrs = Math.floor(whole / 3600);
  const mins = Math.floor((whole % 3600) / 60);
  const secs = String(whole % 60).padStart(2, '0');
  if (hrs > 0) return `${hrs}:${String(mins).padStart(2, '0')}:${secs}`;
  return `${mins}:${secs}`;
}

// Parse "H:MM:SS.mmm", "M:SS.mmm", or bare seconds into a number.
// Returns null when invalid.
function parseTime(text) {
  const value = String(text).trim();
  if (!value) return null;
  if (!value.includes(':')) {
    const seconds = Number(value);
    return Number.isFinite(seconds) ? seconds : null;
  }
  const parts = value.split(':');
  if (parts.length < 2 || parts.length > 3) return null;
  const nums = parts.map((p) => Number(p));
  if (nums.some((n) => !Number.isFinite(n) || n < 0)) return null;
  const [h, m, s] = parts.length === 3 ? nums : [0, nums[0], nums[1]];
  return h * 3600 + m * 60 + s;
}

const errorMessage = (error) =>
  error instanceof Error ? error.message : typeof error === 'string' ? error : 'Unknown error';

/**
 * Wire up the "Clip it" tab: drop an MP4 (SRT optional), identify highlight
 * clips through the local Highlights API, scrub a zoomable timeline, refine
 * each detected clip's bounds, and either export the current selection or
 * batch-generate an MP4 for every detected clip through the FFmpeg sidecar.
 *
 * The `mode` option supports two source flows: 'mp4' (transcribe the video to
 * an SRT first, then identify) and 'srt' (identify directly against a supplied
 * MP4 + SRT pair).
 */
export function initStudio({
  onStatus,
  music,
  workingDirectory,
  prefix = 'studio',
  mode = 'srt',
  transcribe,
} = {}) {
  const $ = (selector) => document.querySelector(selector);

  // 'srt': source is an MP4 + SRT, identify runs directly.
  // 'mp4' (Clip it): source is an MP4 only; identify first generates the SRT
  // from the video's audio, then runs identification on it.
  const mp4Only = mode === 'mp4';
  // The tracker rail includes a transcription stage in mp4-only mode.
  const STAGES = mp4Only ? CLIPIT_STAGES : IDENTIFY_STAGES;

  const video = $(`#${prefix}-video`);
  // Portrait 9:16 preview frame + its muted, blurred background copy of the
  // clip (both preview-only; null in flows without the markup, e.g. the Edit
  // tab which doesn't have a portrait frame).
  const portraitFrame = $(`#${prefix}-portrait-frame`);
  const portraitBg = $(`#${prefix}-video-bg`);
  const emptyState = $(`#${prefix}-empty-state`);
  const timeline = $(`#${prefix}-timeline`);
  const grid = $(`#${prefix}-grid`);
  const ruler = $(`#${prefix}-ruler`);
  const rangeEl = $(`#${prefix}-range`);
  const playhead = $(`#${prefix}-playhead`);
  const startHandle = $(`#${prefix}-handle-start`);
  const endHandle = $(`#${prefix}-handle-end`);
  const hover = $(`#${prefix}-hover`);
  const startInput = $(`#${prefix}-start-input`);
  const endInput = $(`#${prefix}-end-input`);
  const durationOutput = $(`#${prefix}-duration`);
  const loopToggle = $(`#${prefix}-enabled`);
  const resetButton = $(`#${prefix}-reset`);
  const clearButton = $(`#${prefix}-clear-button`);
  const saveButton = $(`#${prefix}-save-button`);
  const saveButtonLabel = saveButton.querySelector('span');
  const revealButton = $(`#${prefix}-reveal-button`);
  const formatField = $(`#${prefix}-format-field`);
  const bgField = $(`#${prefix}-bg-field`);
  // The whole Export clip section, greyed until a detected clip is selected.
  const exportSection = $(`#${prefix}-export-section`);
  const status = $(`#${prefix}-status`);
  const progressWrap = $(`#${prefix}-progress-wrap`);
  const progressBar = $(`#${prefix}-progress-bar`);
  const progressStatus = $(`#${prefix}-progress-status`);
  const progressSpinner = $(`#${prefix}-progress-spinner`);
  const progressPhase = $(`#${prefix}-progress-phase`);
  const progressElapsed = $(`#${prefix}-progress-elapsed`);
  const progressStagesEl = $(`#${prefix}-progress-stages`);

  // Source SRT input.
  const srtButton = $(`#${prefix}-srt-button`);
  const srtName = $(`#${prefix}-srt-name`);
  const srtMeta = $(`#${prefix}-srt-meta`);

  // Identify controls.
  const identifyButton = $(`#${prefix}-identify-button`);
  const idTarget = $(`#${prefix}-id-target`);
  const idStyle = $(`#${prefix}-id-style`);
  const idTopics = $(`#${prefix}-id-topics`);
  const idDiscoveryMode = $(`#${prefix}-id-discovery-mode`);
  const idPresetHint = $(`#${prefix}-id-preset-hint`);
  const idMinDuration = $(`#${prefix}-id-min-duration`);
  const idMinDurationOutput = $(`#${prefix}-id-min-duration-output`);
  const idMaxDuration = $(`#${prefix}-id-max-duration`);
  const idMaxDurationOutput = $(`#${prefix}-id-max-duration-output`);
  const idMaxClips = $(`#${prefix}-id-max-clips`);
  const idCandidates = $(`#${prefix}-id-candidates`);
  const idWindow = $(`#${prefix}-id-window`);
  const idStride = $(`#${prefix}-id-stride`);
  const idSync = $(`#${prefix}-id-sync`);
  const idBaseUrl = $(`#${prefix}-id-base-url`);
  const idReset = $(`#${prefix}-id-reset`);
  const idCollapse = $(`#${prefix}-id-collapse`);
  const idSettings = $(`#${prefix}-identify-settings`);

  // Identify status tracker.
  const idTracker = $(`#${prefix}-identify-tracker`);
  const idSpinner = $(`#${prefix}-identify-spinner`);
  const idPhase = $(`#${prefix}-identify-phase`);
  const idElapsed = $(`#${prefix}-identify-elapsed`);
  const idStagesEl = $(`#${prefix}-identify-stages`);
  const idProgressBar = $(`#${prefix}-identify-progress-bar`);
  const idStatus = $(`#${prefix}-identify-status`);
  const idWindows = $(`#${prefix}-identify-windows`);

  // Batch generate controls.
  const generateButton = $(`#${prefix}-generate-button`);
  const generateProgressWrap = $(`#${prefix}-generate-progress-wrap`);
  const generateSpinner = $(`#${prefix}-generate-spinner`);
  const generatePhase = $(`#${prefix}-generate-phase`);
  const generateElapsed = $(`#${prefix}-generate-elapsed`);
  const generateStagesEl = $(`#${prefix}-generate-stages`);
  const generateProgressBar = $(`#${prefix}-generate-progress-bar`);
  const generateStatus = $(`#${prefix}-generate-status`);
  const generateRevealButton = $(`#${prefix}-generate-reveal-button`);

  const clipsContainer = $(`#${prefix}-clips`);
  const clipsSummary = $(`#${prefix}-clips-summary`);
  // Right-pane wrapper (+ its divider) that holds the identified-clip cards
  // below the timeline; gated on having at least one detected clip.
  const clipsBlock = $(`#${prefix}-clips-block`);
  const clipsRule = $(`#${prefix}-clips-rule`);

  // Floating clip transcript panel + its toggle (Clip it tab only).
  const transcriptToggle = $(`#${prefix}-transcript-toggle`);
  const transcriptPanel = $(`#${prefix}-transcript-panel`);

  // Collapsible source/identify sections + the gated generate section.
  const sourceSection = $(`#${prefix}-source-section`);
  const identifySection = $(`#${prefix}-identify-section`);
  const generateSection = $(`#${prefix}-generate-section`);
  const generateRule = $(`#${prefix}-generate-rule`);

  // Zoom / overview elements.
  const zoomInBtn = $(`#${prefix}-zoom-in`);
  const zoomOutBtn = $(`#${prefix}-zoom-out`);
  const zoomSelBtn = $(`#${prefix}-zoom-sel`);
  const zoomAllBtn = $(`#${prefix}-zoom-all`);
  const zoomLevel = $(`#${prefix}-zoom-level`);
  const zoomSlider = $(`#${prefix}-zoom-slider`);
  const overviewSlider = $(`#${prefix}-overview-slider`);
  const overview = $(`#${prefix}-overview`);
  const overviewClips = $(`#${prefix}-overview-clips`);
  const overviewRange = $(`#${prefix}-overview-range`);
  const overviewPlayhead = $(`#${prefix}-overview-playhead`);
  const overviewView = $(`#${prefix}-overview-view`);

  // Clip edit / overwrite elements.
  const clipEdit = $(`#${prefix}-clip-edit`);
  const clipEditTitle = $(`#${prefix}-clip-edit-title`);
  const clipEditDelta = $(`#${prefix}-clip-edit-delta`);
  const clipSaveBtn = $(`#${prefix}-clip-save`);
  const clipRevertBtn = $(`#${prefix}-clip-revert`);

  let duration = 0;
  let startTime = 0;
  let endTime = 0;
  // Visible window of the main timeline. Equals [0, duration] when fully out.
  let viewStart = 0;
  let viewEnd = 0;
  let dragging = null; // 'start' | 'end' | 'playhead' | 'view'
  let dragViewAnchor = 0; // pointer-to-view offset while panning the minimap
  let videoName = '';
  let videoPath = '';
  let srtPath = '';
  let srtFileName = '';
  let rafId = null;
  let saving = false;
  let identifying = false;
  // True while ffmpeg is extracting audio in the mp4-only transcription step.
  let extracting = false;
  let generating = false;
  let lastOutputPath = '';
  let lastGenerateDir = '';
  let elapsedTimer = null;
  let generateElapsedTimer = null;
  let generateStartedAt = 0;
  let startedAt = 0;
  // Save-selection render tracker timer.
  let saveElapsedTimer = null;
  let saveStartedAt = 0;
  let clips = [];
  let editingClip = null; // reference into `clips` for the selected card
  // Parsed SRT cues for the current clip's source, used to build the floating
  // transcript for a selected clip. Refreshed whenever the SRT changes.
  let transcriptCues = [];
  // Raw Parakeet verbose_json from the last transcription, kept so caption cues
  // can be re-shaped with the caption word/row settings (independent of the
  // 6-words/4-rows shaping used for identification). Empty when the transcript
  // came from a supplied SRT rather than our own transcription.
  let transcriptRawJson = '';
  // Caption cues (shaped with the caption settings) for the whole video, and
  // the subset shifted to the currently-selected clip's local time for preview.
  let captionCues = [];
  // Hooks exposed by the captioning block (set up near the end of init); null
  // in the 'srt' flow where the caption section isn't present.
  let captionHooks = null;
  // Style controller for burned-in captions (null until the caption block wires
  // it up). Declared here so the generate flow can read styleState() from it.
  let captionStyle = null;
  let captionToggleEl = null;
  const captioningEnabled = () => Boolean(captionToggleEl?.checked);
  // Persisted user preference: is the transcript panel toggled on?
  let transcriptOpen = false;
  try {
    transcriptOpen = localStorage.getItem(TRANSCRIPT_PREF_KEY) === '1';
  } catch {
    transcriptOpen = false;
  }

  const setStatus = (message) => {
    status.textContent = message;
    onStatus?.(message);
  };

  function setProgress(message, fraction) {
    progressWrap.hidden = false;
    progressStatus.textContent = message;
    progressBar.style.width = `${Math.max(0, Math.min(100, fraction * 100))}%`;
  }

  function formatSaveElapsed(ms) {
    const total = Math.floor(ms / 1000);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function startSaveElapsed() {
    saveStartedAt = Date.now();
    progressElapsed.textContent = '0:00';
    clearInterval(saveElapsedTimer);
    saveElapsedTimer = setInterval(() => {
      progressElapsed.textContent = formatSaveElapsed(Date.now() - saveStartedAt);
    }, 1000);
  }

  function stopSaveElapsed() {
    clearInterval(saveElapsedTimer);
    saveElapsedTimer = null;
  }

  function setSavePhase(text) {
    progressPhase.textContent = text;
  }

  // Mark a stage active (everything before it done); `failed` flips the current
  // stage into an error state.
  function setSaveStage(stage, { failed = false } = {}) {
    const activeIndex = SAVE_STAGES.indexOf(stage);
    for (const li of progressStagesEl.children) {
      const index = SAVE_STAGES.indexOf(li.dataset.stage);
      li.classList.toggle('failed', failed && index === activeIndex);
      li.classList.toggle('done', !failed && index < activeIndex);
      li.classList.toggle('active', !failed && index === activeIndex);
    }
  }

  // Collapse/expand a studio section by toggling a class and the chevron state.
  function setSectionCollapsed(section, collapsed) {
    if (!section) return;
    section.classList.toggle('collapsed', collapsed);
    const toggle = section.querySelector('.studio-section-toggle');
    if (toggle) {
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.setAttribute('aria-label', collapsed ? 'Expand section' : 'Collapse section');
    }
  }

  // Show or hide the right-pane clip cards (below the timeline) once clips have
  // been identified. The "Generate all clips" batch section is kept in the DOM
  // (its logic is still wired) but hidden from the UI for now — see the
  // permanently-hidden #clipit-generate-section in index.html.
  function setGenerateVisible(visible) {
    if (clipsBlock) clipsBlock.hidden = !visible;
    if (clipsRule) clipsRule.hidden = !visible;
  }

  for (const section of [sourceSection, identifySection]) {
    const toggle = section?.querySelector('.studio-section-toggle');
    toggle?.addEventListener('click', () => {
      setSectionCollapsed(section, !section.classList.contains('collapsed'));
    });
  }

  function updateSaveState() {
    // Export clip stays greyed out until identify has produced clips and the
    // user has selected one (editingClip). This keeps export scoped to a chosen
    // detected clip rather than an arbitrary range.
    const canExport = duration > 0 && Boolean(editingClip);
    saveButton.disabled = !canExport || saving || generating;
    // Grey the whole Export clip section (not just the button) until a detected
    // clip is selected, so the format/caption controls read as unavailable too.
    // Never grey it mid-export, so the progress tracker stays legible.
    exportSection?.classList.toggle('section-locked', !canExport && !saving);
    // In mp4-only mode the identify flow generates the SRT itself, so it only
    // needs a loaded video; in srt mode it needs a chosen SRT.
    identifyButton.disabled = (mp4Only ? !videoPath : !srtPath) || identifying;
    generateButton.disabled = !(duration > 0) || clips.length === 0 || generating || identifying;
  }

  const viewSpan = () => viewEnd - viewStart || 1;
  // Time -> percentage within the current view window (may fall outside 0..100).
  const viewPct = (time) => ((time - viewStart) / viewSpan()) * 100;
  // Time -> percentage within the full-clip overview.
  const fullPct = (time) => (duration ? (time / duration) * 100 : 0);

  function clampRange() {
    if (!duration) return;
    startTime = Math.max(0, Math.min(startTime, duration - MIN_RANGE));
    endTime = Math.min(duration, Math.max(endTime, startTime + MIN_RANGE));
  }

  function clampView() {
    const span = clamp(viewEnd - viewStart, MIN_VIEW_SPAN, duration || MIN_VIEW_SPAN);
    viewStart = clamp(viewStart, 0, Math.max(0, duration - span));
    viewEnd = viewStart + span;
  }

  // --- Rendering ------------------------------------------------------------

  // Place a handle by its true position; if it falls outside the zoomed view,
  // pin it to the edge and flag it so the arrow indicator shows.
  function placeHandle(handle, time) {
    const raw = viewPct(time);
    handle.style.left = `${clamp(raw, 0, 100)}%`;
    handle.classList.toggle('off-start', raw < 0);
    handle.classList.toggle('off-end', raw > 100);
  }

  function renderMarkers({ skipInputs = false } = {}) {
    if (!duration) return;
    const left = clamp(viewPct(startTime), 0, 100);
    const right = clamp(viewPct(endTime), 0, 100);
    rangeEl.style.left = `${left}%`;
    rangeEl.style.width = `${Math.max(0, right - left)}%`;
    placeHandle(startHandle, startTime);
    placeHandle(endHandle, endTime);

    // Overview band (always full-clip coordinates).
    const oLeft = fullPct(startTime);
    const oRight = fullPct(endTime);
    overviewRange.style.left = `${oLeft}%`;
    overviewRange.style.width = `${Math.max(0.4, oRight - oLeft)}%`;

    if (!skipInputs) {
      if (document.activeElement !== startInput) startInput.value = formatTime(startTime);
      if (document.activeElement !== endInput) endInput.value = formatTime(endTime);
    }
    updateClipEditUI();
  }

  function renderPlayhead() {
    if (!duration) return;
    const raw = viewPct(video.currentTime);
    playhead.style.left = `${clamp(raw, 0, 100)}%`;
    playhead.classList.toggle('hidden', raw < -2 || raw > 102);
    overviewPlayhead.style.left = `${fullPct(video.currentTime)}%`;
    highlightCurrentCue();
  }

  function renderView() {
    // Overview viewport box reflects the zoomed window over the full clip.
    overviewView.style.left = `${fullPct(viewStart)}%`;
    overviewView.style.width = `${Math.max(1, fullPct(viewEnd) - fullPct(viewStart))}%`;
    const factor = duration ? duration / viewSpan() : 1;
    zoomLevel.textContent = `${factor < 9.5 ? factor.toFixed(1) : Math.round(factor)}×`;
    zoomAllBtn.disabled = viewStart <= 0 && viewEnd >= duration;
    syncSliders();
    renderRuler();
  }

  // Build ruler labels + grid lines for the current view at a "nice" interval.
  function renderRuler() {
    if (!duration) return;
    const span = viewSpan();
    const target = span / 7; // aim for ~7 ticks
    const step = TICK_STEPS.find((s) => s >= target) ?? TICK_STEPS[TICK_STEPS.length - 1];

    ruler.replaceChildren();
    grid.replaceChildren();
    const first = Math.ceil(viewStart / step) * step;
    for (let t = first; t <= viewEnd + 1e-6; t += step) {
      const pct = viewPct(t);
      if (pct < -0.5 || pct > 100.5) continue;

      const label = document.createElement('span');
      label.className = 'loop-tick';
      label.style.left = `${pct}%`;
      label.textContent = formatTick(t);
      ruler.append(label);

      const line = document.createElement('span');
      line.className = 'loop-gridline';
      line.style.left = `${pct}%`;
      grid.append(line);
    }
  }

  function renderAll() {
    renderMarkers();
    renderPlayhead();
    renderView();
  }

  // --- Loop playback --------------------------------------------------------

  function tick() {
    renderPlayhead();
    if (loopToggle.checked && video.currentTime >= endTime - 0.02) {
      video.currentTime = startTime;
    }
    if (!video.paused && !video.ended) {
      rafId = requestAnimationFrame(tick);
    } else {
      rafId = null;
    }
  }

  function startTicking() {
    if (rafId == null) rafId = requestAnimationFrame(tick);
  }

  // --- Zoom -----------------------------------------------------------------

  function setView(start, end) {
    viewStart = start;
    viewEnd = end;
    clampView();
    renderAll();
  }

  // Zoom by a factor around a focus time (keeps that time under the cursor).
  function zoomAround(focus, factor) {
    if (!duration) return;
    const span = clamp(viewSpan() / factor, MIN_VIEW_SPAN, duration);
    const ratio = clamp((focus - viewStart) / viewSpan(), 0, 1);
    setView(focus - span * ratio, focus - span * ratio + span);
  }

  function fitSelection() {
    if (!duration) return;
    const pad = Math.max(MIN_VIEW_SPAN / 2, (endTime - startTime) * 0.25);
    setView(startTime - pad, endTime + pad);
  }

  function fitAll() {
    setView(0, duration);
  }

  zoomInBtn.addEventListener('click', () => zoomAround((viewStart + viewEnd) / 2, 1.6));
  zoomOutBtn.addEventListener('click', () => zoomAround((viewStart + viewEnd) / 2, 1 / 1.6));
  zoomSelBtn.addEventListener('click', fitSelection);
  zoomAllBtn.addEventListener('click', fitAll);

  // Vertical zoom sliders (right of the main track and the overview). Custom
  // track + thumb (native vertical range inputs render unreliably in WKWebView).
  // They map a 0..1 position onto the same view-window zoom as the toolbar:
  // bottom = fit all, top = the tightest zoom. The mapping is logarithmic so
  // each step feels even, and the zoom keeps the current view center fixed.
  const maxZoomFactor = () => Math.max(1, (duration || MIN_VIEW_SPAN) / MIN_VIEW_SPAN);

  function applyZoomValue(value) {
    if (!duration) return;
    const factor = Math.pow(maxZoomFactor(), clamp(value, 0, 1));
    const span = clamp((duration || MIN_VIEW_SPAN) / factor, MIN_VIEW_SPAN, duration);
    const center = (viewStart + viewEnd) / 2;
    setView(center - span / 2, center + span / 2);
  }

  // Position the thumbs and ARIA from the current zoom (called by renderView so
  // button/drag zoom keeps them in sync). Guarded so it doesn't fight a drag.
  let sliderDragging = false;
  function syncSliders() {
    const max = maxZoomFactor();
    const factor = duration ? duration / viewSpan() : 1;
    const value = max > 1 ? clamp(Math.log(factor) / Math.log(max), 0, 1) : 0;
    for (const slider of [zoomSlider, overviewSlider]) {
      slider.classList.toggle('disabled', !duration);
      slider.setAttribute('aria-valuenow', String(Math.round(value * 100)));
      const thumb = slider.querySelector('.loop-vscroll-thumb');
      if (thumb) thumb.style.bottom = `${value * 100}%`;
    }
  }

  // Pointer position (0 = bottom, 1 = top) within a slider track.
  function valueFromPointer(slider, clientY) {
    const rect = slider.getBoundingClientRect();
    return clamp((rect.bottom - clientY) / rect.height, 0, 1);
  }

  function bindZoomSlider(slider) {
    const onMove = (event) => {
      if (!sliderDragging) return;
      applyZoomValue(valueFromPointer(slider, event.clientY));
    };
    const onUp = () => {
      sliderDragging = false;
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    slider.addEventListener('pointerdown', (event) => {
      if (!duration) return;
      event.preventDefault();
      sliderDragging = true;
      applyZoomValue(valueFromPointer(slider, event.clientY));
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    });
    slider.addEventListener('keydown', (event) => {
      if (!duration) return;
      const current = Number(slider.getAttribute('aria-valuenow')) / 100;
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        applyZoomValue(current + 0.05);
      } else if (event.key === 'ArrowDown') {
        event.preventDefault();
        applyZoomValue(current - 0.05);
      }
    });
  }

  bindZoomSlider(zoomSlider);
  bindZoomSlider(overviewSlider);

  // --- Pointer interaction --------------------------------------------------

  function timeFromPointer(clientX) {
    const rect = timeline.getBoundingClientRect();
    const fraction = clamp((clientX - rect.left) / rect.width, 0, 1);
    return viewStart + fraction * viewSpan();
  }

  function timeFromOverview(clientX) {
    const rect = overview.getBoundingClientRect();
    const fraction = clamp((clientX - rect.left) / rect.width, 0, 1);
    return fraction * duration;
  }

  function onPointerMove(event) {
    if (!dragging || !duration) return;
    if (dragging === 'view') {
      const span = viewSpan();
      const target = timeFromOverview(event.clientX) - dragViewAnchor;
      setView(target, target + span);
      return;
    }
    if (dragging === 'view-start') {
      const t = clamp(timeFromOverview(event.clientX), 0, viewEnd - MIN_VIEW_SPAN);
      setView(t, viewEnd);
      return;
    }
    if (dragging === 'view-end') {
      const t = clamp(timeFromOverview(event.clientX), viewStart + MIN_VIEW_SPAN, duration);
      setView(viewStart, t);
      return;
    }
    const time = timeFromPointer(event.clientX);
    if (dragging === 'start') {
      startTime = Math.max(0, Math.min(time, endTime - MIN_RANGE));
      video.currentTime = startTime;
      renderMarkers();
    } else if (dragging === 'end') {
      endTime = Math.min(duration, Math.max(time, startTime + MIN_RANGE));
      video.currentTime = endTime;
      renderMarkers();
    } else {
      video.currentTime = time; // playhead scrub
    }
    renderPlayhead();
  }

  function endDrag() {
    if (!dragging) return;
    dragging = null;
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('pointerup', endDrag);
    document.body.classList.remove('loop-dragging');
  }

  function beginDrag(which, event) {
    if (!duration) return;
    event.preventDefault();
    dragging = which;
    document.body.classList.add('loop-dragging');
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', endDrag);
  }

  startHandle.addEventListener('pointerdown', (event) => beginDrag('start', event));
  endHandle.addEventListener('pointerdown', (event) => beginDrag('end', event));

  // Pressing the track (away from the handles) seeks there and scrubs.
  timeline.addEventListener('pointerdown', (event) => {
    if (!duration || event.target === startHandle || event.target === endHandle) return;
    video.currentTime = timeFromPointer(event.clientX);
    renderPlayhead();
    beginDrag('playhead', event);
  });

  // Hover tooltip showing the time under the cursor.
  timeline.addEventListener('pointermove', (event) => {
    if (!duration || dragging) {
      hover.hidden = true;
      return;
    }
    const rect = timeline.getBoundingClientRect();
    const pct = clamp(((event.clientX - rect.left) / rect.width) * 100, 0, 100);
    hover.hidden = false;
    hover.style.left = `${pct}%`;
    hover.textContent = formatTime(timeFromPointer(event.clientX));
  });
  timeline.addEventListener('pointerleave', () => { hover.hidden = true; });

  // Overview: click to pan the view there; drag the viewport box to scrub it;
  // grab either end of the box to stretch (resize) the zoom window.
  overview.addEventListener('pointerdown', (event) => {
    if (!duration) return;
    const span = viewSpan();
    if (event.target === overviewView || overviewView.contains(event.target)) {
      const rect = overviewView.getBoundingClientRect();
      const edge = Math.min(8, rect.width / 3);
      if (event.clientX - rect.left <= edge) {
        beginDrag('view-start', event);
        return;
      }
      if (rect.right - event.clientX <= edge) {
        beginDrag('view-end', event);
        return;
      }
      dragViewAnchor = timeFromOverview(event.clientX) - viewStart;
    } else {
      const center = timeFromOverview(event.clientX);
      setView(center - span / 2, center + span / 2);
      dragViewAnchor = span / 2;
    }
    beginDrag('view', event);
  });

  // --- Timestamp inputs -----------------------------------------------------

  function commitInput(which) {
    if (!duration) return;
    const input = which === 'start' ? startInput : endInput;
    const parsed = parseTime(input.value);
    if (parsed == null) {
      input.value = formatTime(which === 'start' ? startTime : endTime);
      return;
    }
    if (which === 'start') {
      startTime = Math.max(0, Math.min(parsed, endTime - MIN_RANGE));
      video.currentTime = startTime;
    } else {
      endTime = Math.min(duration, Math.max(parsed, startTime + MIN_RANGE));
      video.currentTime = endTime;
    }
    renderMarkers();
    renderPlayhead();
  }

  for (const [which, input] of [['start', startInput], ['end', endInput]]) {
    input.addEventListener('change', () => commitInput(which));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        commitInput(which);
        input.blur();
      }
    });
  }

  loopToggle.addEventListener('change', () => {
    if (loopToggle.checked && (video.currentTime < startTime || video.currentTime > endTime)) {
      video.currentTime = startTime;
    }
  });

  resetButton.addEventListener('click', () => {
    if (!duration) return;
    deselectClip();
    startTime = 0;
    endTime = duration;
    clampRange();
    fitAll();
    video.currentTime = startTime;
    renderAll();
  });

  // Full reset: drop the loaded video/SRT, discard detected clips, clear the
  // identify/generate trackers, and restore every knob to its default.
  function clearAll() {
    if (saving || identifying || generating) return;

    // Detected clips and selection.
    clips = [];
    editingClip = null;
    transcriptCues = [];
    transcriptRawJson = '';
    captionCues = [];
    deselectClip();
    captionHooks?.updateCaptionPreview();
    renderClips(clips);
    if (clipsSummary) {
      clipsSummary.hidden = true;
      clipsSummary.textContent = '';
    }

    // Restore the collapsible sections: expand source/identify, re-hide the
    // gated generate step.
    setSectionCollapsed(sourceSection, false);
    setSectionCollapsed(identifySection, false);
    setGenerateVisible(false);

    // Video + timeline state.
    video.pause();
    video.removeAttribute('src');
    video.load();
    video.hidden = true;
    // Release the portrait background copy too.
    if (portraitBg) {
      portraitBg.pause();
      portraitBg.removeAttribute('src');
      portraitBg.load();
    }
    emptyState.hidden = false;
    videoPath = '';
    videoName = '';
    srtPath = '';
    srtFileName = '';
    duration = 0;
    startTime = 0;
    endTime = 0;
    viewStart = 0;
    viewEnd = 0;
    startInput.value = formatTime(0);
    endInput.value = formatTime(0);
    durationOutput.textContent = formatTime(0);
    loopToggle.checked = true;

    // Output format back to Original (hides the background cards).
    const originalFormat = formatField?.querySelector('input[value="original"]');
    if (originalFormat) originalFormat.checked = true;
    syncFormatUI();

    // Source file buttons. setStatus runs the onStatus callback (which writes
    // to #studio-video-meta), so reset the meta text afterward.
    setStatus('Waiting for a clip');
    $(`#${prefix}-video-name`).textContent = 'Choose a video';
    $(`#${prefix}-video-meta`).textContent = 'No file selected';
    if (srtName) srtName.textContent = mp4Only ? 'Optional subtitles' : 'Choose subtitles';
    if (srtMeta) srtMeta.textContent = mp4Only ? 'Auto-generated if not provided' : 'No file selected';
    updateTranscribeStageVisibility();

    // Identify knobs + tracker.
    resetIdentifyKnobs();
    stopElapsed();
    idSpinner.classList.remove('spinning');
    idTracker.hidden = true;
    setStage('submit');
    idPhase.textContent = 'Ready';
    idStatus.textContent = 'Ready';
    idProgressBar.style.width = '0%';
    idElapsed.textContent = '0:00';
    setIdentifyWindows(0, 0);

    // Selection save + generate trackers.
    progressWrap.hidden = true;
    progressBar.style.width = '0%';
    stopSaveElapsed();
    progressSpinner.classList.remove('spinning');
    progressPhase.textContent = 'Ready';
    progressElapsed.textContent = '0:00';
    setSaveStage('prepare');
    progressStatus.textContent = 'Ready';
    lastOutputPath = '';
    revealButton.classList.add('disabled');
    revealButton.setAttribute('aria-disabled', 'true');
    revealButton.disabled = true;
    generateProgressWrap.hidden = true;
    stopGenerateElapsed();
    generateSpinner.classList.remove('spinning');
    generatePhase.textContent = 'Ready';
    generateElapsed.textContent = '0:00';
    setGenerateStage('prepare');
    generateStatus.textContent = 'Ready';
    generateProgressBar.style.width = '0%';
    lastGenerateDir = '';
    generateRevealButton.hidden = true;

    updateSaveState();
  }

  clearButton.addEventListener('click', clearAll);

  // --- Clip edit / overwrite ------------------------------------------------

  function clipIsDirty() {
    if (!editingClip) return false;
    return Math.abs(editingClip.start_seconds - startTime) > 0.001 ||
      Math.abs(editingClip.end_seconds - endTime) > 0.001;
  }

  function updateClipEditUI() {
    if (!editingClip) {
      clipEdit.hidden = true;
      return;
    }
    clipEdit.hidden = false;
    clipEditTitle.textContent = `#${editingClip.rank} ${editingClip.title}`;
    const dirty = clipIsDirty();
    if (dirty) {
      const ds = startTime - editingClip.start_seconds;
      const de = endTime - editingClip.end_seconds;
      const sign = (n) => `${n >= 0 ? '+' : '−'}${Math.abs(n).toFixed(2)}s`;
      clipEditDelta.textContent = `start ${sign(ds)} · end ${sign(de)} · ${(endTime - startTime).toFixed(2)}s total`;
    } else {
      clipEditDelta.textContent = `Original bounds · ${(endTime - startTime).toFixed(2)}s total`;
    }
    clipSaveBtn.disabled = !dirty;
    clipRevertBtn.disabled = !dirty;
    cardFor(editingClip.clip_id)?.classList.toggle('modified', dirty);
  }

  function deselectClip() {
    editingClip = null;
    for (const el of clipsContainer.querySelectorAll('.loop-clip')) {
      el.classList.remove('selected', 'modified');
      el.setAttribute('aria-pressed', 'false');
    }
    clipEdit.hidden = true;
    // No clip selected: keep the panel visible if the toggle is on, but show
    // the "select a clip" hint (the toggle preference is untouched).
    updateTranscriptView();
    captionHooks?.updateCaptionPreview();
    // No clip selected — Export selection greys out again.
    updateSaveState();
  }

  function cardFor(clipId) {
    return clipsContainer.querySelector(`.loop-clip[data-clip-id="${clipId}"]`);
  }

  function refreshCardTimes(clip) {
    const card = cardFor(clip.clip_id);
    if (!card) return;
    const range = `${formatTime(clip.start_seconds)} – ${formatTime(clip.end_seconds)}`;
    const dur = `${(clip.end_seconds - clip.start_seconds).toFixed(1)}s`;
    card.querySelector('.loop-clip-range').textContent = range;
    card.querySelector('.loop-clip-dur').textContent = dur;
  }

  clipSaveBtn.addEventListener('click', () => {
    if (!editingClip || !clipIsDirty()) return;
    // Overwrite the stored clip with the edited bounds.
    editingClip.start_seconds = startTime;
    editingClip.end_seconds = endTime;
    editingClip.duration_seconds = endTime - startTime;
    refreshCardTimes(editingClip);
    cardFor(editingClip.clip_id)?.classList.remove('modified');
    renderOverviewClips();
    updateClipEditUI();
    setStatus(`Saved new bounds for clip ${editingClip.rank}`);
  });

  clipRevertBtn.addEventListener('click', () => {
    if (!editingClip) return;
    setRange(editingClip.start_seconds, editingClip.end_seconds);
    cardFor(editingClip.clip_id)?.classList.remove('modified');
    fitSelection();
  });

  // Apply an explicit range, mark the edited clip dirty, and refresh markers.
  function setRange(start, end, { seek = true } = {}) {
    if (!duration) return;
    startTime = start;
    endTime = end;
    clampRange();
    renderMarkers();
    if (seek) {
      video.currentTime = startTime;
      renderPlayhead();
    }
  }

  // --- Video lifecycle ------------------------------------------------------

  video.addEventListener('loadedmetadata', () => {
    duration = video.duration || 0;
    startTime = 0;
    endTime = duration;
    clampRange();
    viewStart = 0;
    viewEnd = duration;
    durationOutput.textContent = formatTime(duration);
    timeline.classList.toggle('ready', duration > 0);
    overview.classList.toggle('ready', duration > 0);
    renderAll();
    setStatus(`${videoName} · ${formatTime(duration)}`);
    updateSaveState();
    // The video now has real dimensions, so the caption overlay can be laid
    // out against it (and the portrait frame refreshed).
    syncPortraitPreview();
    captionStyle?.reflow();
    captionHooks?.updateCaptionPreview();
  });

  video.addEventListener('play', startTicking);
  video.addEventListener('timeupdate', renderPlayhead);
  video.addEventListener('seeked', renderPlayhead);
  window.addEventListener('resize', () => {
    renderMarkers({ skipInputs: true });
    renderPlayhead();
    renderView();
  });

  // --- Output format (Original vs Portrait 9:16) ----------------------------

  const isPortrait = () =>
    formatField?.querySelector('input:checked')?.value === 'portrait';
  const portraitBackground = () =>
    bgField?.querySelector('input:checked')?.value || 'blur';

  // The preview stage that hosts the portrait frame (Clip it only).
  const stageEl = video.closest('.stage');

  // Mirror the chosen output format in the preview: letterbox the clip into a
  // 9:16 box with black bars, or a muted blurred copy behind it for blurred
  // fill. Preview-only — export still reframes via ffmpeg. No-op where the
  // portrait frame markup is absent (e.g. the Edit tab).
  function syncPortraitPreview() {
    if (!portraitFrame || !stageEl) return;
    const portrait = isPortrait();
    const blurred = portrait && portraitBackground() === 'blur';
    const hasVideo = !video.hidden && Boolean(video.getAttribute('src'));
    const show = portrait && hasVideo;

    stageEl.classList.toggle('portrait-active', show);
    portraitFrame.hidden = !show;
    portraitFrame.classList.toggle('blurred', blurred);

    // The caption canvas just changed (source video ↔ 9:16 frame), so re-lay
    // the caption overlay to match the new export canvas.
    captionStyle?.reflow();

    if (blurred && portraitBg) {
      // Point the background copy at the same stream and keep it silent and
      // roughly in step with the foreground (a few frames of drift is fine on
      // a heavy blur). Only (re)assign the src when it changes.
      const src = video.getAttribute('src') || '';
      if (src && portraitBg.getAttribute('src') !== src) portraitBg.src = src;
      syncPortraitBg();
    } else if (portraitBg) {
      portraitBg.pause();
    }
  }

  // Keep the blurred background video aligned with the foreground playhead and
  // play/pause state. Cheap and only called while blurred fill is showing.
  function syncPortraitBg() {
    if (!portraitBg || portraitFrame?.hidden || !portraitFrame?.classList.contains('blurred')) return;
    if (Math.abs(portraitBg.currentTime - video.currentTime) > 0.3) {
      portraitBg.currentTime = video.currentTime;
    }
    if (!video.paused && portraitBg.paused) portraitBg.play().catch(() => {});
    if (video.paused && !portraitBg.paused) portraitBg.pause();
  }

  // Reveal the blur/black cards only when Portrait is selected, reflect the
  // format in the save button label, and update the live portrait preview. The
  // Portrait choice applies to both the single-selection save and the batch
  // "Generate all clips" export.
  function syncFormatUI() {
    const portrait = isPortrait();
    if (bgField) bgField.hidden = !portrait;
    if (saveButtonLabel) saveButtonLabel.textContent = portrait ? 'Save as 9:16' : 'Save selection';
    syncPortraitPreview();
  }

  if (formatField) {
    for (const input of formatField.querySelectorAll('input')) {
      input.addEventListener('change', syncFormatUI);
    }
    syncFormatUI();
  }
  if (bgField) {
    for (const input of bgField.querySelectorAll('input')) {
      input.addEventListener('change', syncFormatUI);
    }
  }

  // Reflect the initial state on startup: no video and no selected clip, so the
  // Export clip section starts locked/greyed until Identify runs and a clip is
  // chosen. Event handlers keep it in sync from there.
  updateSaveState();

  // Mirror foreground playback onto the blurred background copy.
  video.addEventListener('play', syncPortraitBg);
  video.addEventListener('pause', syncPortraitBg);
  video.addEventListener('seeked', syncPortraitBg);
  video.addEventListener('timeupdate', syncPortraitBg);

  // Portrait pillarbox fields to fold into a trim_video request, or nothing
  // when exporting at the original aspect ratio.
  function portraitFields() {
    return isPortrait() ? { portrait: true, background: portraitBackground() } : {};
  }

  // FFmpeg trim progress arrives as a 0..1 fraction on its own channel. It is
  // shared by both the single-selection save and the batch generate, so route
  // it to whichever flow is active.
  listen('trim-progress', (event) => {
    const fraction = Number(event.payload);
    if (!Number.isFinite(fraction) || fraction < 0 || fraction > 1) return;
    if (saving) {
      const label = isPortrait() ? 'Rendering 9:16' : 'Saving selection';
      setSaveStage('render');
      setSavePhase(isPortrait() ? 'Rendering 9:16' : 'Rendering MP4');
      setProgress(`${label} · ${Math.round(fraction * 100)}%`, 0.02 + fraction * 0.97);
    } else if (generating) {
      onGenerateClipProgress(fraction);
    }
  }).catch((error) => console.error(error));

  // In mp4-only mode the identify flow first extracts audio; ffmpeg reports
  // that as a 0..1 fraction on its own channel. Map it into the transcribe
  // slice (0–0.12) of the overall tracker bar.
  listen('extract-audio-progress', (event) => {
    if (!extracting) return;
    const fraction = Number(event.payload);
    if (Number.isFinite(fraction)) {
      setIdentifyStatus(`Extracting audio · ${Math.round(fraction * 100)}%`, fraction * 0.12);
    }
  }).catch((error) => console.error(error));

  function defaultOutputName() {
    const stem = videoName.replace(/\.mp4$/i, '') || 'clip';
    const suffix = isPortrait() ? 'vertical' : 'trim';
    return `${stem}-${suffix}-${timestamp()}.mp4`;
  }

  // Merge the currently-selected background-music track (if any) into a batch
  // request so every generated clip carries the same bed the preview plays.
  function withMusic(request) {
    const selection = music?.selectionForExport?.();
    return selection ? { ...request, ...selection } : request;
  }

  saveButton.addEventListener('click', async (event) => {
    if (saving || !duration || !videoPath) return;
    clampRange();

    // Offer with/without-music at the click point when a bed is selected.
    const choice = await music?.pickForExport?.(event);
    if (choice?.cancelled) return;
    const musicSelection = choice?.selection || null;
    const portrait = isPortrait();

    let outputPath;
    try {
      const selected = await save({
        title: portrait ? 'Save 9:16 MP4' : 'Save trimmed MP4',
        defaultPath: defaultOutputName(),
        filters: [{ name: 'MP4 video', extensions: ['mp4'] }],
      });
      outputPath = typeof selected === 'string' ? selected : undefined;
    } catch (error) {
      console.error(error);
      setProgress(`Could not save: ${errorMessage(error)}`, 0);
      return;
    }
    if (!outputPath) return;

    saving = true;
    updateSaveState();
    revealButton.classList.add('disabled');
    revealButton.setAttribute('aria-disabled', 'true');
    revealButton.disabled = true;
    progressWrap.hidden = false;
    progressSpinner.classList.add('spinning');
    setSaveStage('prepare');
    setSavePhase(portrait ? 'Preparing 9:16 render' : 'Preparing trim');
    startSaveElapsed();
    setProgress(portrait ? 'Preparing 9:16 render…' : 'Preparing trim…', 0.02);

    try {
      const result = await invoke('trim_video', {
        request: {
          input_path: videoPath,
          output_path: outputPath,
          start_seconds: startTime,
          end_seconds: endTime,
          ...portraitFields(),
          // Burn styled captions into the saved selection when captioning is
          // on. Cues are sliced to the current [startTime, endTime] range (the
          // selected clip's bounds, possibly nudged via the handles).
          ...captionFieldsForClip({ start_seconds: startTime, end_seconds: endTime }),
          ...(musicSelection || {}),
        },
      });
      lastOutputPath = result?.output_path || outputPath;
      revealButton.classList.remove('disabled');
      revealButton.setAttribute('aria-disabled', 'false');
      revealButton.disabled = false;
      setSaveStage('done');
      setSavePhase('Done');
      setProgress('Trim complete', 1);
    } catch (error) {
      console.error(error);
      setSaveStage('render', { failed: true });
      setSavePhase('Failed');
      setProgress(`Trim failed: ${errorMessage(error)}`, 0);
    } finally {
      saving = false;
      progressSpinner.classList.remove('spinning');
      stopSaveElapsed();
      updateSaveState();
    }
  });

  revealButton.addEventListener('click', async () => {
    if (!lastOutputPath) return;
    try {
      await invoke('reveal_in_finder', { path: lastOutputPath });
    } catch (error) {
      console.error(error);
    }
  });

  // --- SRT source -----------------------------------------------------------

  // `transient` is set when the SRT was auto-generated during the mp4-only
  // transcription step. In that case we keep the "Transcribe" tracker stage
  // visible (it just ran) instead of treating it as a user-supplied SRT.
  async function setSrtPath(path, { transient = false } = {}) {
    srtPath = path;
    srtFileName = baseName(path);
    if (srtName) srtName.textContent = srtFileName;
    try {
      const text = await invoke('read_srt', { path });
      const cues = (text.match(/-->/g) || []).length;
      if (srtMeta) srtMeta.textContent = `${cues} cues · id: ${slugify(srtFileName.replace(/\.srt$/i, ''))}`;
      // Keep the parsed cues around so the transcript panel can pull the lines
      // that fall inside a selected clip's time range.
      try {
        transcriptCues = parseSrt(text);
      } catch {
        transcriptCues = [];
      }
    } catch (error) {
      if (srtMeta) srtMeta.textContent = errorMessage(error);
      transcriptCues = [];
    }
    // Shape caption cues now that the SRT is parsed, so the caption preview has
    // data as soon as an SRT is supplied (matching the Burn tab) rather than
    // only after Identify runs.
    captionHooks?.rebuildCaptionCues();
    if (!transient) updateTranscribeStageVisibility();
    updateSaveState();
  }

  srtButton?.addEventListener('click', async () => {
    try {
      const selected = await open({
        multiple: false,
        filters: [{ name: 'SubRip subtitles', extensions: ['srt'] }],
      });
      if (typeof selected === 'string') await setSrtPath(selected);
    } catch (error) {
      console.error(error);
      setStatus(`Could not open subtitles: ${errorMessage(error)}`);
    }
  });

  // --- Identify knobs -------------------------------------------------------

  function showPresetHint() {
    const preset = DISCOVERY_MODE_PRESETS[idDiscoveryMode.value];
    if (!preset) {
      idPresetHint.hidden = true;
      return;
    }
    idPresetHint.hidden = false;
    idPresetHint.textContent =
      `topic_policy=${preset.topic_policy} · ` +
      `admission_strictness=${preset.admission_strictness} · ` +
      `min_candidates_per_window=${preset.min_candidates_per_window}`;
  }

  idDiscoveryMode.addEventListener('change', showPresetHint);
  idMinDuration.addEventListener('input', () => {
    idMinDurationOutput.value = `${idMinDuration.value}s`;
  });
  idMaxDuration.addEventListener('input', () => {
    idMaxDurationOutput.value = `${idMaxDuration.value}s`;
  });

  // Click a duration readout to type an exact number of seconds. The typed
  // value is clamped to the slider's range and snapped to its step.
  function makeOutputEditable(slider, output) {
    output.classList.add('editable');
    output.title = 'Click to type seconds';
    output.addEventListener('click', () => {
      const editor = document.createElement('input');
      editor.type = 'number';
      editor.className = 'output-editor';
      editor.value = slider.value;
      editor.min = slider.min;
      editor.max = slider.max;
      editor.step = slider.step;

      const commit = () => {
        const min = Number(slider.min);
        const max = Number(slider.max);
        const step = Number(slider.step) || 1;
        let value = Number(editor.value);
        if (Number.isFinite(value)) {
          value = Math.min(max, Math.max(min, value));
          value = Math.round(value / step) * step;
          slider.value = String(value);
          slider.dispatchEvent(new Event('input', { bubbles: true }));
        }
        editor.replaceWith(output);
      };

      editor.addEventListener('blur', commit);
      editor.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          commit();
        } else if (event.key === 'Escape') {
          event.preventDefault();
          editor.replaceWith(output);
        }
      });

      output.replaceWith(editor);
      editor.focus();
      editor.select();
    });
  }

  makeOutputEditable(idMinDuration, idMinDurationOutput);
  makeOutputEditable(idMaxDuration, idMaxDurationOutput);

  function optionalPositiveInteger(input, label) {
    const raw = input.value.trim();
    if (!raw) return null;
    const value = Number(raw);
    if (!Number.isInteger(value) || value <= 0) {
      input.focus();
      throw new Error(`${label} must be a whole number greater than 0, or left blank for Auto.`);
    }
    return value;
  }

  // Assemble the multipart text fields the Highlights API expects.
  function collectIdentifyFields() {
    const discoveryWindow = optionalPositiveInteger(idWindow, 'Discovery window');
    const stride = optionalPositiveInteger(idStride, 'Stride');
    const fields = {
      language: LANGUAGE,
      min_clip_duration_seconds: String(Number(idMinDuration.value)),
      max_clip_duration_seconds: String(Number(idMaxDuration.value)),
      candidates_per_window: String(Number(idCandidates.value) || IDENTIFY_DEFAULTS.candidates),
    };
    if (idMaxClips.value.trim()) fields.max_clips = String(Number(idMaxClips.value));
    if (discoveryWindow != null) fields.discovery_window_seconds = String(discoveryWindow);
    if (stride != null) fields.stride_seconds = String(stride);
    fields.source_id = slugify(srtFileName.replace(/\.srt$/i, ''));
    if (idTarget.value.trim()) fields.target = idTarget.value.trim();
    if (idStyle.value.trim()) fields.style = idStyle.value.trim();

    const topics = parseTopics(idTopics.value);
    if (topics.length) fields.topics = topics.join(', ');

    const preset = DISCOVERY_MODE_PRESETS[idDiscoveryMode.value];
    if (preset) Object.assign(fields, preset);

    return fields;
  }

  function resetIdentifyKnobs() {
    idTarget.value = '';
    idStyle.value = '';
    idTopics.value = '';
    idDiscoveryMode.value = '';
    idMinDuration.value = String(IDENTIFY_DEFAULTS.minDuration);
    idMinDurationOutput.value = `${IDENTIFY_DEFAULTS.minDuration}s`;
    idMaxDuration.value = String(IDENTIFY_DEFAULTS.maxDuration);
    idMaxDurationOutput.value = `${IDENTIFY_DEFAULTS.maxDuration}s`;
    idMaxClips.value = '';
    idCandidates.value = String(IDENTIFY_DEFAULTS.candidates);
    idWindow.value = '';
    idStride.value = '';
    idSync.checked = false;
    idBaseUrl.value = DEFAULT_BASE_URL;
    showPresetHint();
  }

  idReset.addEventListener('click', resetIdentifyKnobs);
  idCollapse.addEventListener('click', () => {
    idSettings.open = false;
  });

  // --- Identify tracker -----------------------------------------------------

  function formatElapsed(ms) {
    const total = Math.floor(ms / 1000);
    const m = Math.floor(total / 60);
    const s = String(total % 60).padStart(2, '0');
    return `${m}:${s}`;
  }

  function startElapsed() {
    startedAt = Date.now();
    idElapsed.textContent = '0:00';
    clearInterval(elapsedTimer);
    elapsedTimer = setInterval(() => {
      idElapsed.textContent = formatElapsed(Date.now() - startedAt);
    }, 1000);
  }

  function stopElapsed() {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }

  // In mp4-only (Clip it) mode the tracker rail leads with a "Transcribe"
  // stage. When the user supplies their own SRT that step is skipped, so hide
  // the stage from the rail to keep the tracker honest.
  function updateTranscribeStageVisibility() {
    if (!mp4Only) return;
    const li = idStagesEl.querySelector('[data-stage="transcribe"]');
    if (li) li.hidden = Boolean(srtPath);
  }

  // Mark a stage active (and everything before it done); `failed` flips the
  // current stage to an error state.
  function setStage(stage, { failed = false } = {}) {
    const activeIndex = STAGES.indexOf(stage);
    for (const li of idStagesEl.children) {
      const index = STAGES.indexOf(li.dataset.stage);
      li.classList.toggle('done', !failed && index < activeIndex);
      li.classList.toggle('active', !failed && index === activeIndex);
      li.classList.toggle('failed', failed && index === activeIndex);
    }
  }

  function setIdentifyStatus(message, fraction) {
    idTracker.hidden = false;
    idStatus.textContent = message;
    if (typeof fraction === 'number') {
      idProgressBar.style.width = `${Math.max(0, Math.min(100, fraction * 100))}%`;
    }
  }

  function setIdentifyWindows(done, total) {
    if (!total) {
      idWindows.hidden = true;
      return;
    }
    idWindows.hidden = false;
    idWindows.textContent = `${done} / ${total} windows`;
  }

  // mp4-only flow: extract audio → transcribe → build an SRT next to the video
  // (6 words/line, 4 rows), then point the identify pipeline at it. Reflected
  // in the tracker's leading "transcribe" stage. Returns the SRT path.
  async function generateSrtForVideo() {
    let srtOut = '';
    let audioPath = '';
    let audioCreated = false;

    setStage('transcribe');
    idPhase.textContent = 'Extracting audio';
    setIdentifyStatus('Extracting audio…', 0.01);
    extracting = true;
    try {
      audioPath = await workingDirectory.allocateFile(
        baseName(videoPath).replace(/\.mp4$/i, '') || 'audio',
        'mp3',
      );
      srtOut = await workingDirectory.allocateFile(
        `${baseName(videoPath).replace(/\.mp4$/i, '')}-${timestamp()}`,
        'srt',
      );
      await invoke('extract_audio', {
        request: { input_path: videoPath, output_path: audioPath },
      });
      extracting = false;
      audioCreated = true;

      idPhase.textContent = 'Transcribing audio';
      setIdentifyStatus('Transcribing audio · this can take a while…', 0.14);
      // Use the captioning section's endpoint/model overrides when present so
      // the transcription settings are configurable; fall back to defaults.
      const endpoint = captionHooks?.transcribeEndpoint?.() || {};
      const raw = await invoke('transcribe_audio', {
        request: {
          audio_path: audioPath,
          base_url: endpoint.baseUrl || TRANSCRIBE_BASE_URL,
          model: endpoint.model || TRANSCRIBE_MODEL,
        },
      });

      idPhase.textContent = 'Building subtitles';
      setIdentifyStatus('Building subtitles…', 0.2);
      // Keep the raw transcription so caption cues can be re-shaped later with
      // the caption word/row settings (see rebuildCaptionCues).
      transcriptRawJson = raw;
      const srt = jsonToSrt(JSON.parse(raw), CLIPIT_SRT_OPTIONS);
      await invoke('save_text_file', { path: srtOut, contents: srt });
      return srtOut;
    } catch (error) {
      if (srtOut) {
        try {
          await invoke('delete_file', { path: srtOut });
        } catch {
          // Preserve the original pipeline error.
        }
      }
      throw error;
    } finally {
      extracting = false;
      if (audioPath) {
        try {
          await invoke('delete_file', { path: audioPath });
        } catch (error) {
          console.error('Could not delete audio:', error);
        }
      }
    }
  }

  identifyButton.addEventListener('click', async () => {
    if (identifying || (mp4Only ? !videoPath : !srtPath)) return;

    // In mp4-only (Clip it) mode we normally transcribe the video to build the
    // SRT ourselves. If the user has already supplied an SRT, skip that step
    // and identify directly against it — matching the 'srt' flow.
    const needsTranscription = mp4Only && !srtPath;

    let fields;
    try {
      fields = collectIdentifyFields();
    } catch (error) {
      setIdentifyStatus(errorMessage(error), 0);
      return;
    }

    identifying = true;
    let transientSrtPath = '';
    updateSaveState();
    identifyButton.querySelector('span').textContent = 'Identifying…';
    idSpinner.classList.add('spinning');
    if (clipsSummary) {
      clipsSummary.hidden = true;
      clipsSummary.textContent = '';
    }
    startElapsed();
    setIdentifyWindows(0, 0);
    setStage(needsTranscription ? 'transcribe' : 'submit');
    idPhase.textContent = 'Preparing';
    setIdentifyStatus('Preparing request…', 0.02);

    try {
      // mp4-only mode has to produce the SRT (and its fields) before running
      // identification — unless the user already provided one.
      if (needsTranscription) {
        transientSrtPath = await generateSrtForVideo();
        await setSrtPath(transientSrtPath, { transient: true });
        fields = collectIdentifyFields();
      }

      const payload = await runIdentification({
        srtPath,
        srtFileName,
        fields,
        baseUrl: idBaseUrl.value,
        sync: idSync.checked,
        onStage: (stage) => setStage(stage),
        onPhase: (text) => { idPhase.textContent = text; },
        onStatus: setIdentifyStatus,
        onWindows: setIdentifyWindows,
      });
      clips = [...(payload.clips || [])].sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0));
      deselectClip();
      renderClips(clips);
      setStage('done');
      idPhase.textContent = 'Complete';
      const totalElapsed = formatElapsed(Date.now() - startedAt);
      setIdentifyStatus(`Done in ${totalElapsed} · ${clips.length} clips`, 1);
      setStatus(`${clips.length} ${clips.length === 1 ? 'clip' : 'clips'} identified`);
      if (clipsSummary) {
        clipsSummary.textContent = `${clips.length} clips · ${totalElapsed} elapsed`;
        clipsSummary.hidden = clips.length === 0;
      }
      // Shape caption cues from the (now available) transcript so the caption
      // preview and burn have something to work with.
      captionHooks?.rebuildCaptionCues();
      if (clips.length > 0) {
        // Reveal the export step and tuck away the source/identify sections so
        // the focus shifts to reviewing and generating clips.
        setGenerateVisible(true);
        setSectionCollapsed(sourceSection, true);
        setSectionCollapsed(identifySection, true);
        // Auto-select the top-ranked clip so the preview jumps straight to a
        // highlight instead of the full video.
        const firstClip = clips[0];
        selectClip(firstClip, cardFor(firstClip.clip_id));
      }
    } catch (error) {
      console.error(error);
      const failStage =
        extracting || (needsTranscription && !srtPath) ? 'transcribe' : idSync.checked ? 'process' : 'submit';
      setStage(failStage, { failed: true });
      idPhase.textContent = 'Failed';
      setIdentifyStatus(`Identify failed: ${errorMessage(error)}`, 0);
      setStatus(`Identify failed: ${errorMessage(error)}`);
    } finally {
      if (transientSrtPath) {
        try {
          await invoke('delete_file', { path: transientSrtPath });
          if (srtPath === transientSrtPath) srtPath = '';
        } catch (error) {
          console.error('Could not delete generated SRT:', error);
        }
      }
      identifying = false;
      extracting = false;
      idSpinner.classList.remove('spinning');
      stopElapsed();
      identifyButton.querySelector('span').textContent = 'Identify clips';
      updateSaveState();
    }
  });

  // --- Detected clips -------------------------------------------------------

  const pct = (value) => `${Math.round((value ?? 0) * 100)}%`;

  // --- Clip transcript panel ------------------------------------------------

  // Collect the transcript text for a clip by joining every SRT cue that
  // overlaps the clip's time range. Cues store inline b/i/u tags, so strip
  // them to plain text. Returns the cues (with timing) that overlap the clip's
  // time range so each can be rendered and highlighted individually.
  function transcriptCuesForClip(clip) {
    if (!clip || !transcriptCues.length) return [];
    const start = clip.start_seconds ?? 0;
    const end = clip.end_seconds ?? 0;
    return transcriptCues
      .filter((cue) => cue.end > start && cue.start < end)
      .map((cue) => ({
        start: cue.start,
        end: cue.end,
        text: cue.text.replace(/<\/?[biu]\s*>/gi, '').replace(/\s+/g, ' ').trim(),
      }))
      .filter((cue) => cue.text);
  }

  // Rendered cue spans for the selected clip, each tagged with its timing so
  // the one under the playhead can be highlighted live.
  let transcriptCueEls = [];
  let activeCueIndex = -1;

  // Paint the floating transcript for the selected clip. Each cue is its own
  // span so the cue currently under the preview playhead can be highlighted.
  function renderTranscript(clip) {
    if (!transcriptPanel) return;
    transcriptPanel.replaceChildren();
    transcriptCueEls = [];
    activeCueIndex = -1;

    const title = document.createElement('p');
    title.className = 'transcript-panel-title';
    title.textContent = clip ? `#${clip.rank} · Transcript` : 'Transcript';
    transcriptPanel.append(title);

    const cues = transcriptCuesForClip(clip);
    if (!cues.length) {
      const empty = document.createElement('p');
      empty.className = 'transcript-panel-empty';
      empty.textContent = clip
        ? 'No transcript lines fall within this clip.'
        : 'Select a clip to see its transcript.';
      transcriptPanel.append(empty);
      return;
    }

    const body = document.createElement('p');
    body.className = 'transcript-panel-body';
    cues.forEach((cue, index) => {
      const span = document.createElement('span');
      span.className = 'transcript-cue';
      span.textContent = cue.text;
      span.dataset.start = String(cue.start);
      span.dataset.end = String(cue.end);
      body.append(span);
      transcriptCueEls.push(span);
      if (index < cues.length - 1) body.append(document.createTextNode(' '));
    });
    transcriptPanel.append(body);
    highlightCurrentCue();
  }

  // Toggle the `current` class on the cue span that spans the playhead time.
  // Only touches the DOM when the active cue changes to avoid fighting the
  // user's own scrolling on every playhead tick.
  function highlightCurrentCue() {
    if (!transcriptCueEls.length) return;
    const now = video.currentTime;
    let active = -1;
    for (let i = 0; i < transcriptCueEls.length; i += 1) {
      const el = transcriptCueEls[i];
      const start = Number(el.dataset.start);
      const end = Number(el.dataset.end);
      if (now >= start && now < end) {
        active = i;
        break;
      }
      // Fall back to the most recent cue that has already started.
      if (now >= start) active = i;
    }
    if (active === activeCueIndex) return;
    if (activeCueIndex >= 0) transcriptCueEls[activeCueIndex]?.classList.remove('current');
    activeCueIndex = active;
    if (active >= 0) {
      const el = transcriptCueEls[active];
      el.classList.add('current');
      el.scrollIntoView({ block: 'nearest' });
    }
  }

  // Reflect `transcriptOpen` in the toggle + panel visibility. The panel only
  // shows when a clip is selected so it never floats over an empty preview.
  function updateTranscriptView() {
    if (!transcriptToggle || !transcriptPanel) return;
    transcriptToggle.setAttribute('aria-pressed', String(transcriptOpen));
    transcriptPanel.hidden = !transcriptOpen;
    if (transcriptOpen) renderTranscript(editingClip);
  }

  if (transcriptToggle && transcriptPanel) {
    // Restore the persisted state on mount.
    transcriptToggle.setAttribute('aria-pressed', String(transcriptOpen));
    transcriptToggle.addEventListener('click', () => {
      transcriptOpen = !transcriptOpen;
      try {
        localStorage.setItem(TRANSCRIPT_PREF_KEY, transcriptOpen ? '1' : '0');
      } catch {
        // Preference still applies for the session when storage is unavailable.
      }
      updateTranscriptView();
    });
  }

  function selectClip(clip, card) {
    // Re-clicking the already-selected clip deselects it and returns to the
    // original full-clip range.
    if (editingClip === clip) {
      deselectClip();
      startTime = 0;
      endTime = duration;
      clampRange();
      setRange(startTime, endTime);
      fitAll();
      setStatus(`${videoName} · ${formatTime(duration)}`);
      return;
    }
    editingClip = clip;
    for (const el of clipsContainer.querySelectorAll('.loop-clip')) {
      const isSel = el === card;
      el.classList.toggle('selected', isSel);
      el.setAttribute('aria-pressed', String(isSel));
    }
    setRange(clip.start_seconds, clip.end_seconds);
    if (!loopToggle.checked) loopToggle.checked = true;
    fitSelection();
    setStatus(`Clip ${clip.rank}: ${clip.title}`);
    updateTranscriptView();
    captionHooks?.updateCaptionPreview();
    // A clip is now selected — enable Export selection.
    updateSaveState();
    // Bring the preview into view: the clip cards sit below the timeline, so
    // selecting one scrolls back up to the video so the user sees the result.
    stageEl?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // Paint the detected clips as little ticks on the overview minimap.
  function renderOverviewClips() {
    overviewClips.replaceChildren();
    if (!duration) return;
    clips.forEach((clip) => {
      const tick = document.createElement('span');
      tick.className = 'loop-overview-clip';
      const left = fullPct(clip.start_seconds);
      const width = Math.max(0.4, fullPct(clip.end_seconds) - left);
      tick.style.left = `${left}%`;
      tick.style.width = `${width}%`;
      tick.title = `#${clip.rank} ${clip.title}`;
      overviewClips.append(tick);
    });
  }

  function renderClips(list) {
    clipsContainer.replaceChildren();
    list.forEach((clip) => {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'loop-clip';
      card.dataset.clipId = clip.clip_id;
      card.setAttribute('aria-pressed', 'false');

      const range = `${formatTime(clip.start_seconds)} – ${formatTime(clip.end_seconds)}`;
      const dur = `${(clip.duration_seconds ?? clip.end_seconds - clip.start_seconds).toFixed(1)}s`;
      const overall = clip.scores?.overall;

      card.innerHTML = `
        <div class="loop-clip-head">
          <span class="loop-clip-rank">#${clip.rank}</span>
          <strong class="loop-clip-title"></strong>
          ${overall != null ? `<span class="loop-clip-score">${pct(overall)}</span>` : ''}
        </div>
        <div class="loop-clip-time">
          <span class="loop-clip-range">${range}</span>
          <span class="loop-clip-dur">${dur}</span>
        </div>
        <p class="loop-clip-summary"></p>
        <div class="loop-clip-tags">
          <span title="Interestingness">INT ${pct(clip.scores?.interestingness)}</span>
          <span title="Self-contained">SLF ${pct(clip.scores?.self_contained)}</span>
          <span title="Boundary quality">BND ${pct(clip.scores?.boundary_quality)}</span>
        </div>
      `;
      // Set text via textContent to avoid injecting untrusted summary markup.
      card.querySelector('.loop-clip-title').textContent = clip.title;
      card.querySelector('.loop-clip-summary').textContent = clip.summary;

      card.addEventListener('click', () => selectClip(clip, card));
      clipsContainer.append(card);
    });
    clipsContainer.hidden = list.length === 0;
    // The transcript toggle only makes sense once there are clips to inspect.
    if (transcriptToggle) transcriptToggle.hidden = list.length === 0;
    renderOverviewClips();
    updateSaveState();
  }

  // --- Batch generate -------------------------------------------------------

  // Per-clip filename: <video>-<rank>-<title-slug>-<stamp>.mp4. The stamp is
  // shared across one batch run so a generated set sorts together.
  function clipFileName(clip, stamp) {
    const stem = videoName.replace(/\.mp4$/i, '') || 'clip';
    const rank = String(clip.rank ?? 0).padStart(2, '0');
    const titleSlug = slugify(clip.title || `clip-${rank}`).slice(0, 60);
    return `${stem}-${rank}-${titleSlug}-${stamp}.mp4`;
  }

  function joinPath(dir, name) {
    const sep = dir.includes('\\') && !dir.includes('/') ? '\\' : '/';
    return dir.replace(/[\\/]+$/, '') + sep + name;
  }

  // Build the caption burn-in fields for one clip's trim_video request, or an
  // empty object when captioning is off or there are no cues in range. Cues are
  // shifted to clip-local time (the trimmed clip starts at 0). The ASS canvas
  // matches the final frame: the portrait 1080×1920 when reframing, otherwise
  // the source video's own dimensions — so captions are positioned correctly.
  function captionFieldsForClip(clip) {
    if (!captioningEnabled() || !captionHooks) return {};
    // Shape cues fresh from the current transcription knobs so the export
    // honors the latest settings (the live preview doesn't reshape on change).
    const cues = captionHooks.exportCuesForClip(clip);
    if (!cues.length) return {};

    const portrait = isPortrait();
    const width = portrait ? 1080 : video.videoWidth;
    const height = portrait ? 1920 : video.videoHeight;
    if (!width || !height) return {};

    const style = captionStyle.styleState();
    const ass = createAss({ width, height, cues, style });
    return {
      ass_content: ass,
      working_directory: workingDirectory.path,
      font_path: style.fontPath || null,
      bundled_font_name: style.fontPath ? null : style.bundledFontFile,
      font_dir: SYSTEM_FONT_DIR,
    };
  }

  // Index of the clip currently being exported, so the shared trim-progress
  // channel can be turned into overall batch progress.
  let generateIndex = 0;
  function onGenerateClipProgress(fraction) {
    const total = clips.length || 1;
    const overall = (generateIndex + fraction) / total;
    setGenerateProgress(
      `Generating clip ${generateIndex + 1} of ${total} · ${Math.round(fraction * 100)}%`,
      overall,
    );
  }

  function setGenerateProgress(message, fraction) {
    generateProgressWrap.hidden = false;
    generateStatus.textContent = message;
    generateProgressBar.style.width = `${Math.max(0, Math.min(100, fraction * 100))}%`;
  }

  function setGenerateStage(stage, { failed = false } = {}) {
    const activeIndex = GENERATE_STAGES.indexOf(stage);
    for (const li of generateStagesEl.children) {
      const index = GENERATE_STAGES.indexOf(li.dataset.stage);
      li.classList.toggle('done', !failed && index < activeIndex);
      li.classList.toggle('active', !failed && index === activeIndex);
      li.classList.toggle('failed', failed && index === activeIndex);
    }
  }

  function startGenerateElapsed() {
    generateStartedAt = Date.now();
    generateElapsed.textContent = '0:00';
    clearInterval(generateElapsedTimer);
    generateElapsedTimer = setInterval(() => {
      generateElapsed.textContent = formatElapsed(Date.now() - generateStartedAt);
    }, 1000);
  }

  function stopGenerateElapsed() {
    clearInterval(generateElapsedTimer);
    generateElapsedTimer = null;
  }

  generateButton.addEventListener('click', async () => {
    if (generating || !duration || !videoPath || clips.length === 0) return;

    generatePhase.textContent = 'Preparing export';
    setGenerateStage('prepare');
    setGenerateProgress('Choose a folder for the generated clips', 0);

    let outputDir;
    try {
      const selected = await open({
        title: 'Choose a folder for the generated clips',
        directory: true,
        multiple: false,
      });
      outputDir = typeof selected === 'string' ? selected : undefined;
    } catch (error) {
      console.error(error);
      generatePhase.textContent = 'Failed';
      setGenerateStage('prepare', { failed: true });
      setGenerateProgress(`Could not choose folder: ${errorMessage(error)}`, 0);
      return;
    }
    if (!outputDir) {
      generateProgressWrap.hidden = true;
      return;
    }

    generating = true;
    updateSaveState();
    generateButton.querySelector('span').textContent = 'Generating…';
    generateRevealButton.hidden = true;
    lastGenerateDir = outputDir;
    generateSpinner.classList.add('spinning');
    startGenerateElapsed();
    generatePhase.textContent = 'Generating clips';
    setGenerateStage('generate');

    const total = clips.length;
    const failures = [];
    const batchStamp = timestamp();
    setGenerateProgress(`Generating ${total} clip${total === 1 ? '' : 's'}…`, 0);

    try {
      for (let i = 0; i < total; i += 1) {
        generateIndex = i;
        const clip = clips[i];
        const outputPath = joinPath(outputDir, clipFileName(clip, batchStamp));
        setGenerateProgress(`Generating clip ${i + 1} of ${total}…`, i / total);
        try {
          await invoke('trim_video', {
            request: withMusic({
              input_path: videoPath,
              output_path: outputPath,
              start_seconds: clip.start_seconds,
              end_seconds: clip.end_seconds,
              ...portraitFields(),
              ...captionFieldsForClip(clip),
            }),
          });
        } catch (error) {
          console.error(error);
          failures.push(`#${clip.rank}: ${errorMessage(error)}`);
        }
      }

      const ok = total - failures.length;
      if (failures.length === 0) {
        generatePhase.textContent = 'Complete';
        setGenerateStage('done');
        setGenerateProgress(`Generated ${ok} clip${ok === 1 ? '' : 's'}`, 1);
        setStatus(`Generated ${ok} clips`);
      } else {
        generatePhase.textContent = 'Complete with errors';
        setGenerateStage('done');
        setGenerateProgress(`Generated ${ok} of ${total} · ${failures.length} failed`, 1);
        setStatus(`Generated ${ok} of ${total} clips (${failures.length} failed)`);
      }
      generateRevealButton.hidden = false;
    } finally {
      generating = false;
      generateSpinner.classList.remove('spinning');
      stopGenerateElapsed();
      generateButton.querySelector('span').textContent = 'Generate all clips';
      updateSaveState();
    }
  });

  generateRevealButton.addEventListener('click', async () => {
    if (!lastGenerateDir) return;
    try {
      await invoke('reveal_in_finder', { path: lastGenerateDir });
    } catch (error) {
      console.error(error);
    }
  });

  async function loadVideo(path) {
    videoPath = path;
    videoName = baseName(path);
    await invoke('allow_media_file', { path });
    // Custom stream:// protocol with HTTP Range support, so audio plays back
    // reliably (the asset protocol drops audio on some MP4s).
    video.src = await invoke('media_stream_url', { path });
    video.hidden = false;
    emptyState.hidden = true;
    progressWrap.hidden = true;
    // Now that a clip is loaded, refresh the portrait preview (it stays hidden
    // unless Portrait is the selected format).
    syncPortraitPreview();
    lastOutputPath = '';
    revealButton.classList.add('disabled');
    revealButton.setAttribute('aria-disabled', 'true');
    revealButton.disabled = true;
    setStatus(`Loading ${videoName}…`);
  }

  // --- Captioning (subconfig of the Export stage) ---------------------------
  // Optional: transcribe each clip's words (reusing the identify transcript)
  // and burn styled captions into the exported MP4. The style controls +
  // transcription knobs live in collapsible blocks under a toggle that turns
  // the whole feature on/off. Preview shows the caption over the selected clip.

  const captionSection = $(`#${prefix}-caption-section`);
  const captionToggle = $(`#${prefix}-caption-toggle`);
  captionToggleEl = captionToggle;
  const captionBody = $(`#${prefix}-caption-body`);
  const captionStylePrefix = `${prefix}-caption-`;
  const captionPreviewEl = $(`#${prefix}-subtitle-preview`);

  // Transcription knobs for caption cue shaping.
  const capMaxDuration = $(`#${prefix}-caption-max-duration`);
  const capMaxDurationOutput = $(`#${prefix}-caption-max-duration-output`);
  const capMaxWords = $(`#${prefix}-caption-max-words`);
  const capRows = $(`#${prefix}-caption-rows`);
  const capPause = $(`#${prefix}-caption-pause`);
  const capBaseUrl = $(`#${prefix}-caption-base-url`);
  const capModel = $(`#${prefix}-caption-model`);
  const capTranscribeReset = $(`#${prefix}-caption-transcribe-reset`);

  if (captionSection && captionToggle && captionBody) {
    // The style controller drives both the live preview overlay and the ASS we
    // burn on export. It reads its controls from the mounted `clipit-caption-`
    // block (mounted by main.js's subtitle-style loop).
    captionStyle = createStyleController(
      {
        prefix: captionStylePrefix,
        video,
        stage: video.closest('.stage'),
        preview: captionPreviewEl,
        defaults: CAPTION_STYLE_DEFAULTS,
        onChange: () => {},
        // When Portrait is selected the burned caption is positioned against
        // the 1080×1920 canvas, so lay the preview out against the portrait
        // frame (which represents that canvas) instead of the letterboxed
        // source video. Returns null for Original, so the controller falls back
        // to the source-video canvas — matching the Original-format export.
        canvasMetrics: () => {
          if (!isPortrait() || !portraitFrame || portraitFrame.hidden) return null;
          const rect = portraitFrame.getBoundingClientRect();
          if (!rect.width || !rect.height) return null;
          return { rect, width: 1080, height: 1920 };
        },
      },
      {
        COLORS,
        FONTS,
        VARIATIONS,
        populateVariationSelect,
        ASS_MARGIN_X,
        ASS_TO_CSS_FONT_SCALE,
        openFontFile: async () => {
          try {
            const selected = await open({
              multiple: false,
              filters: [{ name: 'Font', extensions: ['ttf', 'otf', 'ttc'] }],
            });
            return typeof selected === 'string' ? selected : '';
          } catch (error) {
            console.error(error);
            return '';
          }
        },
      },
    );

    // Shape caption options from the knobs, falling back to the 4-words/1-row
    // defaults when a field is blank/invalid.
    function collectCaptionOptions() {
      return {
        maxDuration: Number(capMaxDuration.value) || CAPTION_CUE_DEFAULTS.maxDuration,
        maxWordsPerLine: Number(capMaxWords.value) || CAPTION_CUE_DEFAULTS.maxWordsPerLine,
        rows: Number(capRows.value) || CAPTION_CUE_DEFAULTS.rows,
        pause: Number(capPause.value) || CAPTION_CUE_DEFAULTS.pause,
      };
    }

    function resetCaptionKnobs() {
      capMaxDuration.value = String(CAPTION_CUE_DEFAULTS.maxDuration);
      capMaxDurationOutput.value = `${CAPTION_CUE_DEFAULTS.maxDuration.toFixed(1)}s`;
      capMaxWords.value = String(CAPTION_CUE_DEFAULTS.maxWordsPerLine);
      capRows.value = String(CAPTION_CUE_DEFAULTS.rows);
      capPause.value = String(CAPTION_CUE_DEFAULTS.pause);
      capBaseUrl.value = TRANSCRIBE_BASE_URL;
      capModel.value = TRANSCRIBE_MODEL;
    }

    // Shape whole-video caption cues from the raw transcription with a given
    // options set, or fall back to the already-parsed transcript cues when only
    // a supplied SRT is available.
    function shapeCaptionCues(options) {
      if (transcriptRawJson) {
        try {
          return parseSrt(jsonToSrt(JSON.parse(transcriptRawJson), options));
        } catch (error) {
          console.error('Could not shape caption cues:', error);
          return transcriptCues.slice();
        }
      }
      // No raw transcription (SRT was supplied): reuse its cues as-is.
      return transcriptCues.slice();
    }

    // (Re)build the caption cues that drive the live preview. These are shaped
    // once, when a transcript lands (Identify finishes or an SRT is supplied).
    // Later edits to the transcription knobs are deliberately NOT reflected
    // here — they apply only at export (see exportCuesForClip) so the preview
    // stays stable and matches what the user last saw.
    function rebuildCaptionCues() {
      captionCues = shapeCaptionCues(collectCaptionOptions());
      updateCaptionPreview();
    }

    // Slice full-video cues to a clip window and shift them to clip-local time
    // (the exported/trimmed clip starts at 0). Defaults to the preview cues.
    function captionCuesForClip(clip, source = captionCues) {
      if (!clip || !source.length) return [];
      const start = clip.start_seconds ?? 0;
      const end = clip.end_seconds ?? 0;
      return source
        .filter((cue) => cue.end > start && cue.start < end)
        .map((cue) => ({
          start: Math.max(0, cue.start - start),
          end: Math.max(0, cue.end - start),
          text: cue.text,
        }))
        .filter((cue) => cue.text && cue.end > cue.start);
    }

    // Cues for export: shaped fresh from the current transcription knobs so the
    // render honors the latest settings even though the preview doesn't.
    function exportCuesForClip(clip) {
      return captionCuesForClip(clip, shapeCaptionCues(collectCaptionOptions()));
    }

    // Cues for the currently-selected clip, used to drive the preview overlay.
    let clipCaptionCues = [];

    function updateCaptionPreview() {
      if (!captionStyle) return;
      const on = captioningEnabled();
      clipCaptionCues = on && editingClip ? captionCuesForClip(editingClip) : [];
      if (!on || !clipCaptionCues.length) {
        captionStyle.showCue(null);
        return;
      }
      // Recompute the overlay's position/size against the current canvas before
      // revealing a cue — otherwise the CSS position vars can be stale (or
      // unset on first show) and the caption renders in the wrong place.
      captionStyle.reflow();
      syncCaptionCue();
    }

    // Show the caption cue under the playhead, using clip-local time.
    function syncCaptionCue() {
      if (!captioningEnabled() || !clipCaptionCues.length || !editingClip) {
        captionStyle.showCue(null);
        return;
      }
      const local = video.currentTime - (editingClip.start_seconds ?? 0);
      const cue = clipCaptionCues.find((c) => local >= c.start && local <= c.end);
      captionStyle.showCue(cue || null);
    }

    // Reflect the on/off switch: collapse the whole body (subtitle style +
    // transcription settings) when off, grey it out, and clear the preview.
    function syncCaptionEnabled() {
      const on = captioningEnabled();
      captionBody.hidden = !on;
      captionBody.classList.toggle('section-disabled', !on);
      captionBody.setAttribute('aria-hidden', String(!on));
      updateCaptionPreview();
    }

    captionToggle.addEventListener('change', syncCaptionEnabled);
    // The transcription knobs only reshape cues at export time, so they just
    // keep their own output label current here — the live preview intentionally
    // doesn't rebuild (see rebuildCaptionCues / the "applied on export" note).
    capMaxDuration.addEventListener('input', () => {
      capMaxDurationOutput.value = `${Number(capMaxDuration.value).toFixed(1)}s`;
    });
    capTranscribeReset?.addEventListener('click', resetCaptionKnobs);

    // Keep the caption preview in sync with playback, scrubbing, and layout.
    video.addEventListener('timeupdate', syncCaptionCue);
    video.addEventListener('seeked', syncCaptionCue);
    window.addEventListener('resize', () => captionStyle.reflow());

    resetCaptionKnobs();
    syncCaptionEnabled();

    // Expose hooks the rest of the studio flow calls into.
    captionHooks = {
      rebuildCaptionCues,
      updateCaptionPreview,
      captionCuesForClip,
      exportCuesForClip,
      collectCaptionOptions,
      resetKnobs: resetCaptionKnobs,
      // Endpoint/model overrides for the identify-time transcription.
      transcribeEndpoint: () => ({
        baseUrl: capBaseUrl.value.trim(),
        model: capModel.value.trim(),
      }),
    };
  }

  showPresetHint();

  return {
    loadVideo,
    setSrtPath,
    deactivate() {
      video.pause();
    },
  };
}
