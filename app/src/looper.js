import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open, save } from '@tauri-apps/plugin-dialog';
import { timestamp } from './datetime.js';

// Minimum distance, in seconds, kept between the start and end markers so the
// loop range can never collapse to zero (or invert).
const MIN_RANGE = 0.1;
// Tightest zoom: the main timeline never shows less than this many seconds.
const MIN_VIEW_SPAN = 0.5;
// Candidate ruler intervals (seconds). The smallest one giving a sensible tick
// count for the current view span is chosen.
const TICK_STEPS = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];

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
 * Wire up the "Edit" tab: drop (or browse for) an MP4, scrub a zoomable
 * timeline, drag the start/end markers (or type timestamps) to set a range,
 * loop playback, optionally reframe to portrait 9:16, and export the selection
 * through the native FFmpeg sidecar. The preview stage doubles as a drop zone,
 * with source + export controls in the left column.
 */
export function initLooper({ onStatus, music } = {}) {
  const $ = (selector) => document.querySelector(selector);

  const video = $('#loop-video');
  // Portrait 9:16 preview frame + its muted, blurred background copy of the
  // clip (both preview-only; mirror the Clip it tab).
  const portraitFrame = $('#loop-portrait-frame');
  const portraitBg = $('#loop-video-bg');
  const emptyState = $('#loop-empty-state');
  const videoButton = $('#loop-video-button');
  const videoNameEl = $('#loop-video-name');
  const videoMetaEl = $('#loop-video-meta');
  const timeline = $('#loop-timeline');
  const grid = $('#loop-grid');
  const ruler = $('#loop-ruler');
  const rangeEl = $('#loop-range');
  const playhead = $('#loop-playhead');
  const startHandle = $('#loop-handle-start');
  const endHandle = $('#loop-handle-end');
  const hover = $('#loop-hover');
  const startInput = $('#loop-start-input');
  const endInput = $('#loop-end-input');
  const durationOutput = $('#loop-duration');
  const loopToggle = $('#loop-enabled');
  const resetButton = $('#loop-reset');
  const clearButton = $('#loop-clear-button');
  const saveButton = $('#loop-save-button');
  const saveButtonLabel = saveButton.querySelector('span');
  const revealButton = $('#loop-reveal-button');
  const exportSection = $('#loop-export-section');
  const bgField = $('#loop-bg-field');
  const status = $('#loop-status');
  const progressWrap = $('#loop-progress-wrap');
  const progressBar = $('#loop-progress-bar');
  const progressStatus = $('#loop-progress-status');
  const progressSpinner = $('#loop-progress-spinner');
  const progressPhase = $('#loop-progress-phase');
  const progressElapsed = $('#loop-progress-elapsed');
  const progressStagesEl = $('#loop-progress-stages');

  // Ordered stages for the save/render tracker.
  const RENDER_STAGES = ['prepare', 'render', 'done'];
  let renderElapsedTimer = null;
  let renderStartedAt = 0;

  // Zoom / overview elements.
  const zoomInBtn = $('#loop-zoom-in');
  const zoomOutBtn = $('#loop-zoom-out');
  const zoomSelBtn = $('#loop-zoom-sel');
  const zoomAllBtn = $('#loop-zoom-all');
  const zoomLevel = $('#loop-zoom-level');
  const zoomSlider = $('#loop-zoom-slider');
  const overviewSlider = $('#loop-overview-slider');
  const overview = $('#loop-overview');
  const overviewClips = $('#loop-overview-clips');
  const overviewRange = $('#loop-overview-range');
  const overviewPlayhead = $('#loop-overview-playhead');
  const overviewView = $('#loop-overview-view');

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
  let rafId = null;
  let saving = false;
  let lastOutputPath = '';

  const setStatus = (message) => {
    status.textContent = message;
    onStatus?.(message);
  };

  function setProgress(message, fraction) {
    progressWrap.hidden = false;
    progressStatus.textContent = message;
    progressBar.style.width = `${Math.max(0, Math.min(100, fraction * 100))}%`;
  }

  function formatRenderElapsed(ms) {
    const total = Math.floor(ms / 1000);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function startRenderElapsed() {
    renderStartedAt = Date.now();
    progressElapsed.textContent = '0:00';
    clearInterval(renderElapsedTimer);
    renderElapsedTimer = setInterval(() => {
      progressElapsed.textContent = formatRenderElapsed(Date.now() - renderStartedAt);
    }, 1000);
  }

  function stopRenderElapsed() {
    clearInterval(renderElapsedTimer);
    renderElapsedTimer = null;
  }

  function setRenderPhase(text) {
    progressPhase.textContent = text;
  }

  // Mark a stage active (everything before it done); `failed` flips the current
  // stage into an error state.
  function setRenderStage(stage, { failed = false } = {}) {
    const activeIndex = RENDER_STAGES.indexOf(stage);
    for (const li of progressStagesEl.children) {
      const index = RENDER_STAGES.indexOf(li.dataset.stage);
      li.classList.toggle('failed', failed && index === activeIndex);
      li.classList.toggle('done', !failed && index < activeIndex);
      li.classList.toggle('active', !failed && index === activeIndex);
    }
  }

  function updateSaveState() {
    const hasVideo = duration > 0;
    saveButton.disabled = !hasVideo || saving;
    // Grey out the whole export block (format pickers + button) until a source
    // video is loaded, matching the Clip it tab's gated export.
    if (exportSection) {
      exportSection.classList.toggle('section-disabled', !hasVideo);
      exportSection.setAttribute('aria-disabled', String(!hasVideo));
    }
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
  }

  function renderPlayhead() {
    if (!duration) return;
    const raw = viewPct(video.currentTime);
    playhead.style.left = `${clamp(raw, 0, 100)}%`;
    playhead.classList.toggle('hidden', raw < -2 || raw > 102);
    overviewPlayhead.style.left = `${fullPct(video.currentTime)}%`;
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
    // Keyboard: up/down nudges zoom.
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
    startTime = 0;
    endTime = duration;
    clampRange();
    fitAll();
    video.currentTime = startTime;
    renderAll();
  });

  // Full reset: drop the loaded video, clear the timeline/range, reset the
  // output format and render tracker, and return the tab to its empty state.
  function clearAll() {
    if (saving) return;

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
    duration = 0;
    startTime = 0;
    endTime = 0;
    viewStart = 0;
    viewEnd = 0;
    lastOutputPath = '';
    timeline.classList.remove('ready');
    overview.classList.remove('ready');
    overviewClips.replaceChildren();
    durationOutput.textContent = formatTime(0);
    if (startInput) startInput.value = formatTime(0);
    if (endInput) endInput.value = formatTime(0);
    loopToggle.checked = true;
    renderAll();

    // Output format back to Original (hides the background cards + preview).
    const originalFormat = document.querySelector('input[name="loop-format"][value="original"]');
    if (originalFormat) originalFormat.checked = true;
    syncFormatUI();

    // Source file button + status.
    videoNameEl.textContent = 'Choose a video';
    videoMetaEl.textContent = 'No file selected';
    setStatus('Waiting for a clip');

    // Render tracker.
    progressWrap.hidden = true;
    progressBar.style.width = '0%';
    stopRenderElapsed();
    progressSpinner.classList.remove('spinning');
    progressPhase.textContent = 'Ready';
    progressElapsed.textContent = '0:00';
    setRenderStage('prepare');
    progressStatus.textContent = 'Ready';
    revealButton.classList.add('disabled');
    revealButton.setAttribute('aria-disabled', 'true');
    revealButton.disabled = true;

    updateSaveState();
  }

  clearButton?.addEventListener('click', clearAll);

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
    videoMetaEl.textContent = video.videoWidth
      ? `${video.videoWidth} × ${video.videoHeight} · ${formatTime(duration)}`
      : formatTime(duration);
    setStatus(`${videoName} · ${formatTime(duration)}`);
    updateSaveState();
    // A clip just loaded (or swapped); refresh the 9:16 preview so it reflects
    // the current format choice against the new source.
    syncPortraitPreview();
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
    document.querySelector('input[name="loop-format"]:checked')?.value === 'portrait';
  const portraitBackground = () =>
    document.querySelector('input[name="loop-bg"]:checked')?.value || 'blur';

  // The preview stage that hosts the portrait frame.
  const stageEl = video.closest('.stage');

  // Mirror the chosen output format in the preview: letterbox the clip into a
  // 9:16 box with black bars, or a muted blurred copy behind it for blurred
  // fill. Preview-only — export still reframes via ffmpeg.
  function syncPortraitPreview() {
    if (!portraitFrame || !stageEl) return;
    const portrait = isPortrait();
    const blurred = portrait && portraitBackground() === 'blur';
    const hasVideo = !video.hidden && Boolean(video.getAttribute('src'));
    const show = portrait && hasVideo;

    stageEl.classList.toggle('portrait-active', show);
    portraitFrame.hidden = !show;
    portraitFrame.classList.toggle('blurred', blurred);

    if (blurred && portraitBg) {
      // Point the background copy at the same stream and keep it silent and
      // roughly in step with the foreground. Only (re)assign src when it
      // changes.
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
  // format in the save button label, and update the live portrait preview.
  function syncFormatUI() {
    const portrait = isPortrait();
    bgField.hidden = !portrait;
    saveButtonLabel.textContent = portrait ? 'Save as 9:16' : 'Save selection';
    syncPortraitPreview();
  }

  for (const input of document.querySelectorAll('input[name="loop-format"]')) {
    input.addEventListener('change', syncFormatUI);
  }
  for (const input of document.querySelectorAll('input[name="loop-bg"]')) {
    input.addEventListener('change', syncFormatUI);
  }
  syncFormatUI();
  // Grey the export block on load — no source video yet.
  updateSaveState();

  // Mirror foreground playback onto the blurred background copy.
  video.addEventListener('play', syncPortraitBg);
  video.addEventListener('pause', syncPortraitBg);
  video.addEventListener('seeked', syncPortraitBg);
  video.addEventListener('timeupdate', syncPortraitBg);

  // FFmpeg trim progress arrives as a 0..1 fraction on its own channel.
  listen('trim-progress', (event) => {
    if (!saving) return;
    const fraction = Number(event.payload);
    if (Number.isFinite(fraction) && fraction >= 0 && fraction <= 1) {
      const clipLen = formatTime(Math.max(0, endTime - startTime));
      const label = isPortrait()
        ? 'Reframing to 9:16 and encoding'
        : 'Encoding trimmed clip';
      setRenderStage('render');
      setRenderPhase(isPortrait() ? 'Rendering 9:16' : 'Rendering MP4');
      setProgress(
        `${label} · ${clipLen} clip · ${Math.round(fraction * 100)}%`,
        0.02 + fraction * 0.97,
      );
    }
  }).catch((error) => console.error(error));

  function defaultOutputName() {
    const stem = videoName.replace(/\.mp4$/i, '') || 'clip';
    const suffix = isPortrait() ? 'vertical' : 'trim';
    return `${stem}-${suffix}-${timestamp()}.mp4`;
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
    const startedAt = Date.now();
    progressWrap.hidden = false;
    progressSpinner.classList.add('spinning');
    setRenderStage('prepare');
    setRenderPhase(portrait ? 'Preparing 9:16 reframe' : 'Preparing trim');
    startRenderElapsed();
    setProgress(
      portrait ? 'Preparing 9:16 reframe…' : 'Preparing trim…',
      0.02,
    );

    try {
      const result = await invoke('trim_video', {
        request: {
          input_path: videoPath,
          output_path: outputPath,
          start_seconds: startTime,
          end_seconds: endTime,
          ...(portrait ? { portrait: true, background: portraitBackground() } : {}),
          ...(musicSelection || {}),
        },
      });
      lastOutputPath = result?.output_path || outputPath;
      revealButton.classList.remove('disabled');
      revealButton.setAttribute('aria-disabled', 'false');
      revealButton.disabled = false;
      const secs = ((Date.now() - startedAt) / 1000).toFixed(1);
      const what = portrait ? 'Saved 9:16 clip' : 'Saved trimmed clip';
      setRenderStage('done');
      setRenderPhase('Done');
      setProgress(`${what} in ${secs}s · ${baseName(lastOutputPath)}`, 1);
    } catch (error) {
      console.error(error);
      const what = portrait ? '9:16 export' : 'Trim';
      setRenderStage('render', { failed: true });
      setRenderPhase('Failed');
      setProgress(`${what} failed: ${errorMessage(error)}`, 0);
    } finally {
      saving = false;
      progressSpinner.classList.remove('spinning');
      stopRenderElapsed();
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

  // --- Source loading -------------------------------------------------------

  // Both the empty-state stage and the left-column source button browse.
  const browseClick = () => {
    browse().catch((error) => {
      console.error(error);
      setStatus(`Could not open video: ${errorMessage(error)}`);
    });
  };
  emptyState.addEventListener('click', browseClick);
  videoButton.addEventListener('click', browseClick);

  async function browse() {
    const selected = await open({
      multiple: false,
      filters: [{ name: 'MP4 video', extensions: ['mp4'] }],
    });
    if (typeof selected === 'string') await loadVideo(selected);
  }

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
    lastOutputPath = '';
    overviewClips.replaceChildren();
    revealButton.classList.add('disabled');
    revealButton.setAttribute('aria-disabled', 'true');
    revealButton.disabled = true;
    videoNameEl.textContent = videoName;
    videoMetaEl.textContent = 'Loading…';
    setStatus(`Loading ${videoName}…`);
  }

  return {
    loadVideo,
    browse,
    deactivate() {
      video.pause();
      // Pause the blurred background copy too when leaving the tab.
      portraitBg?.pause();
    },
  };
}
