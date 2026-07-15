import './style.css';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { getCurrentWebview } from '@tauri-apps/api/webview';
import { open, save } from '@tauri-apps/plugin-dialog';
import { createAss, parseSrt } from './ass.js';
import { initLooper } from './looper.js';
import { initStudio } from './studio.js';
import { initGenerate } from './generate.js';
import { initCaption } from './caption.js';
import { createMusicStore, initMusicPopover, initMusicSettings } from './music.js';
import { mountSubtitleStyle, syncStyleToggleAvailability } from './subtitleStyle.js';
import {
  createWorkingDirectoryStore,
  initWorkingDirectorySettings,
} from './workingDirectory.js';
import { timestamp } from './datetime.js';
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

const $ = (selector) => document.querySelector(selector);

// Render shared subtitle-style controls before any tab queries or binds them.
for (const mount of document.querySelectorAll('.subtitle-style')) {
  mountSubtitleStyle(mount, mount.dataset.subtitleStylePrefix || '');
}

const videoButton = $('#video-button');
const srtButton = $('#srt-button');
const fontButton = $('#font-button');
const fontClear = $('#font-clear');
const resetButton = $('#reset-button');
const burnClearButton = $('#burn-clear-button');
const stage = $('#stage');
const sourceVideo = $('#source-video');
const subtitlePreview = $('#subtitle-preview');
const renderButton = $('#render-button');
const revealButton = $('#reveal-button');
const sourceSection = $('#burn-source-section');
const renderTracker = $('#render-tracker');
const renderSpinner = $('#render-spinner');
const renderPhase = $('#render-phase');
const renderElapsedEl = $('#render-elapsed');
const renderStagesEl = $('#render-stages');
const themeToggle = $('#theme-toggle');

// Ordered stages for the Burn SRT render tracker.
const RENDER_STAGES = ['prepare', 'render', 'done'];
let renderElapsedTimer = null;
let renderStartedAt = 0;
const fullscreenButton = $('#fullscreen-button');
const workingDirectory = await createWorkingDirectoryStore();

// Default subtitle style, reused on startup and by the reset button.
const DEFAULTS = {
  colorIndex: 7, // Orange
  fontIndex: fontIndexByFamily(DEFAULT_FONT_FAMILY), // Lato
  variationIndex: 2, // Standard outline
  outlineColorIndex: 1, // Black
  fontSize: 72,
  letterSpacing: 0,
  yPosition: 20, // 20% up from the bottom
};

// Absolute filesystem paths for the user-selected inputs.
let videoPath;
let videoName;
let srtPath;
let fontPath;
let cues = [];
let lastOutputPath;
let selectedColor = COLORS[DEFAULTS.colorIndex][1];
let selectedOutlineColor = COLORS[DEFAULTS.outlineColorIndex][1];
let selectedBackgroundColor = COLORS[1][1];
let rendering = false;

// Fonts referenced by name are resolved by libass against the macOS system
// font folder during burn, so a chosen custom font file can resolve its real
// family name. This is fixed (no longer user-configurable).
const SYSTEM_FONT_DIR = '/System/Library/Fonts';

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem('ripe-clips-theme', theme);
  } catch {
    // Theme still applies when storage is unavailable.
  }
  const isDark = theme === 'dark';
  themeToggle.querySelector('.theme-icon').textContent = isDark ? '☀' : '☾';
  themeToggle.querySelector('.theme-label').textContent = isDark ? 'Light' : 'Dark';
  themeToggle.setAttribute('aria-label', `Switch to ${isDark ? 'light' : 'dark'} mode`);
  document.querySelector('meta[name="theme-color"]').content = isDark ? '#111310' : '#f2efe8';
}

// Build a wrapping set of circular color chips into `container`. `groupName`
// keeps each radio set independent; `onPick` receives the chosen hex.
function buildSwatches(container, groupName, defaultIndex, onPick) {
  COLORS.forEach(([name, color], index) => {
    const label = document.createElement('label');
    label.className = 'swatch';
    label.title = name;
    label.innerHTML = `
      <input type="radio" name="${groupName}" value="${color}" ${index === defaultIndex ? 'checked' : ''}>
      <span style="--swatch:${color}"></span>
      <em>${name}</em>
    `;
    label.querySelector('input').addEventListener('change', (event) => {
      onPick(event.target.value);
      updatePreviewStyle();
    });
    container.append(label);
  });
}

function initializeControls() {
  buildSwatches($('#color-options'), 'color', DEFAULTS.colorIndex, (color) => {
    selectedColor = color;
  });
  buildSwatches($('#outline-color-options'), 'outline-color', DEFAULTS.outlineColorIndex, (color) => {
    selectedOutlineColor = color;
  });
  buildSwatches($('#background-color-options'), 'background-color', 1, (color) => {
    selectedBackgroundColor = color;
  });

  // Render each option in its own typeface so the dropdown previews the font.
  FONTS.forEach(({ label, cssFont }, index) => {
    const option = new Option(label, index);
    option.style.fontFamily = cssFont;
    $('#font-select').add(option);
  });
  populateVariationSelect($('#variation-select'));
  resetStyle();
}

// Render the collapsed font <select> in the currently selected font.
function updateFontSelectPreview() {
  const font = FONTS[Number($('#font-select').value)];
  // Disable Bold/Italic when the family has no such face (a custom font file
  // may contain any face, so keep them live in that case).
  syncStyleToggleAvailability(
    font,
    { boldToggle: $('#bold-toggle'), italicToggle: $('#italic-toggle') },
    Boolean(fontPath),
  );
  $('#font-select').style.fontFamily = font ? font.cssFont : '';
}

function updateVariationControls() {
  const variation = VARIATIONS[Number($('#variation-select').value)][1];
  $('#outline-color-field').hidden = Boolean(variation.box) || !variation.outline;
  $('#background-color-field').hidden = !variation.box;
}

// Restore every style control to its default value and refresh the preview.
function resetStyle() {
  selectedColor = COLORS[DEFAULTS.colorIndex][1];
  const swatch = document.querySelector(`#color-options input[value="${selectedColor}"]`);
  if (swatch) swatch.checked = true;

  selectedOutlineColor = COLORS[DEFAULTS.outlineColorIndex][1];
  const outlineSwatch = document.querySelector(`#outline-color-options input[value="${selectedOutlineColor}"]`);
  if (outlineSwatch) outlineSwatch.checked = true;

  selectedBackgroundColor = COLORS[1][1];
  const backgroundSwatch = document.querySelector(
    `#background-color-options input[value="${selectedBackgroundColor}"]`,
  );
  if (backgroundSwatch) backgroundSwatch.checked = true;

  $('#font-select').value = String(DEFAULTS.fontIndex);
  $('#variation-select').value = String(DEFAULTS.variationIndex);
  $('#bold-toggle').checked = FONTS[DEFAULTS.fontIndex].bold;
  $('#italic-toggle').checked = false;

  $('#font-size').value = String(DEFAULTS.fontSize);
  $('#font-size-output').value = String(DEFAULTS.fontSize);

  $('#letter-spacing').value = String(DEFAULTS.letterSpacing);
  $('#letter-spacing-output').value = String(DEFAULTS.letterSpacing);

  $('#y-position').value = String(DEFAULTS.yPosition);
  $('#y-position-output').value = `${DEFAULTS.yPosition}%`;

  updateFontSelectPreview();
  updateVariationControls();
  updatePreviewStyle();
}

function currentCue(time) {
  return cues.find((cue) => time >= cue.start && time <= cue.end);
}

function cueHtml(text) {
  // Escape everything, then re-enable the inline tags parseSrt preserves so
  // the preview shows bold/italic/underline without allowing arbitrary markup.
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\n/g, '<br>');
  return escaped.replace(/&lt;(\/?[biu])\s*&gt;/gi, '<$1>');
}

function updateSubtitle() {
  const cue = currentCue(sourceVideo.currentTime);
  subtitlePreview.innerHTML = cue ? cueHtml(cue.text) : '';
  subtitlePreview.classList.toggle('visible', Boolean(cue));
}

function styleState() {
  const font = FONTS[Number($('#font-select').value)];
  return {
    color: selectedColor,
    outlineColor: selectedOutlineColor,
    fontName: font.family,
    cssFont: font.cssFont,
    bundledFontFile: font.file,
    bold: $('#bold-toggle').checked,
    italic: $('#italic-toggle').checked,
    fontSize: Number($('#font-size').value),
    letterSpacing: Number($('#letter-spacing').value),
    variation: {
      ...VARIATIONS[Number($('#variation-select').value)][1],
      backgroundColor: selectedBackgroundColor,
    },
    yPosition: 100 - Number($('#y-position').value),
  };
}

function previewScale() {
  if (!sourceVideo.videoWidth) return 1;
  return sourceVideo.clientWidth / sourceVideo.videoWidth;
}

function colorWithOpacity(hex, opacity = 1) {
  const value = hex.replace('#', '');
  const red = Number.parseInt(value.slice(0, 2), 16);
  const green = Number.parseInt(value.slice(2, 4), 16);
  const blue = Number.parseInt(value.slice(4, 6), 16);
  const alpha = Number.isFinite(opacity) ? Math.max(0, Math.min(1, opacity)) : 1;
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

function updatePreviewStyle() {
  const {
    color,
    outlineColor,
    cssFont,
    fontSize,
    variation,
    yPosition,
    bold,
    italic,
    letterSpacing,
  } = styleState();
  const scale = previewScale();
  const assScale = ASS_TO_CSS_FONT_SCALE * scale;
  const stageRect = $('#stage').getBoundingClientRect();
  const videoRect = sourceVideo.getBoundingClientRect();
  const marginX = ASS_MARGIN_X * scale;
  const shadowColor = colorWithOpacity(
    variation.shadowColor || '#000000',
    variation.shadowOpacity ?? 1,
  );
  const boxColor = colorWithOpacity(
    variation.backgroundColor || '#000000',
    variation.backgroundOpacity ?? 0.5,
  );
  const shadowOffset = variation.shadow * assScale;

  subtitlePreview.style.color = color;
  subtitlePreview.style.fontFamily = cssFont;
  subtitlePreview.style.fontWeight = bold ? '700' : '400';
  subtitlePreview.style.fontStyle = italic ? 'italic' : 'normal';
  subtitlePreview.style.letterSpacing = letterSpacing ? `${letterSpacing * assScale}px` : 'normal';
  subtitlePreview.style.fontSize = `${fontSize * assScale}px`;
  subtitlePreview.style.webkitTextStroke =
    !variation.box && variation.outline
      ? `${variation.outline * assScale}px ${outlineColor}`
      : '0';
  subtitlePreview.style.textShadow = !variation.box && variation.shadow
    ? `${shadowOffset}px ${shadowOffset}px 0 ${shadowColor}`
    : 'none';
  subtitlePreview.style.background = variation.box
    ? boxColor
    : 'transparent';
  subtitlePreview.style.padding = variation.box
    ? `${variation.outline * assScale}px`
    : '0';
  subtitlePreview.style.boxShadow = variation.box && variation.shadow
    ? `${shadowOffset}px ${shadowOffset}px 0 ${boxColor}`
    : 'none';
  subtitlePreview.style.setProperty('--subtitle-top', `${videoRect.top - stageRect.top + videoRect.height * yPosition / 100}px`);
  subtitlePreview.style.setProperty('--subtitle-left', `${videoRect.left - stageRect.left + marginX}px`);
  subtitlePreview.style.setProperty('--subtitle-width', `${Math.max(0, videoRect.width - marginX * 2)}px`);
  subtitlePreview.style.setProperty('--subtitle-center-x', `${videoRect.left - stageRect.left + videoRect.width / 2}px`);
  subtitlePreview.classList.toggle('boxed', Boolean(variation.box));
}

function updateReadyState() {
  const ready = Boolean(videoPath && srtPath && cues.length && sourceVideo.videoWidth);
  renderButton.disabled = !ready || rendering;
  fullscreenButton.disabled = !sourceVideo.videoWidth;
  $('#preview-status').textContent = ready ? `${cues.length} subtitle cues loaded` : 'Waiting for files';
}

// Collapse/expand the source-files section, mirroring the Clip it tab.
function setSourceCollapsed(collapsed) {
  if (!sourceSection) return;
  sourceSection.classList.toggle('collapsed', collapsed);
  const toggle = sourceSection.querySelector('.studio-section-toggle');
  if (toggle) {
    toggle.setAttribute('aria-expanded', String(!collapsed));
    toggle.setAttribute('aria-label', collapsed ? 'Expand section' : 'Collapse section');
  }
}

sourceSection?.querySelector('.studio-section-toggle')?.addEventListener('click', () => {
  setSourceCollapsed(!sourceSection.classList.contains('collapsed'));
});

function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement;
}

function isStageFullscreen() {
  return fullscreenElement() === stage;
}

async function toggleStageFullscreen() {
  if (!sourceVideo.videoWidth) return;
  if (isStageFullscreen()) {
    const exitFullscreen = document.exitFullscreen || document.webkitExitFullscreen;
    await exitFullscreen.call(document);
    return;
  }
  const requestFullscreen = stage.requestFullscreen || stage.webkitRequestFullscreen;
  await requestFullscreen.call(stage);
}

function updateFullscreenState() {
  const active = isStageFullscreen();
  fullscreenButton.textContent = active ? '×' : '⛶';
  fullscreenButton.title = active ? 'Exit fullscreen preview' : 'Fullscreen preview';
  fullscreenButton.setAttribute('aria-label', fullscreenButton.title);
  requestAnimationFrame(updatePreviewStyle);
}

function baseName(path) {
  return path.split(/[\\/]/).pop() || path;
}

async function loadVideoPath(path) {
  videoPath = path;
  videoName = baseName(path);
  // A fresh source selection re-expands the section if it was collapsed.
  setSourceCollapsed(false);
  // Extend the asset-protocol scope to just this file before previewing it,
  // so the webview can read the selected video and nothing else.
  await invoke('allow_media_file', { path });
  // Stream through our custom protocol (full HTTP Range support) so audio
  // tracks initialize reliably; the asset protocol drops audio on some MP4s.
  sourceVideo.src = await invoke('media_stream_url', { path });
  sourceVideo.hidden = false;
  $('#empty-state').hidden = true;
  $('#video-name').textContent = videoName;
  $('#video-meta').textContent = 'Loading…';
  sourceVideo.addEventListener('loadedmetadata', () => {
    $('#video-meta').textContent =
      `${sourceVideo.videoWidth} × ${sourceVideo.videoHeight} · ${formatDuration(sourceVideo.duration)}`;
    updatePreviewStyle();
    updateReadyState();
  }, { once: true });
  updateReadyState();
}

async function loadSrtPath(path) {
  // Reading goes through a narrow Rust command rather than the asset protocol.
  const text = await invoke('read_srt', { path });
  // A fresh source selection re-expands the section if it was collapsed.
  setSourceCollapsed(false);
  srtPath = path;
  cues = parseSrt(text);
  $('#srt-name').textContent = baseName(path);
  $('#srt-meta').textContent = `${cues.length} cues`;
  updateSubtitle();
  updateReadyState();
}

videoButton.addEventListener('click', async () => {
  try {
    const selected = await open({
      multiple: false,
      filters: [{ name: 'MP4 video', extensions: ['mp4'] }],
    });
    if (typeof selected === 'string') await loadVideoPath(selected);
  } catch (error) {
    console.error(error);
    setStatus(`Could not open video: ${errorMessage(error)}`, 0);
  }
});

srtButton.addEventListener('click', async () => {
  try {
    const selected = await open({
      multiple: false,
      filters: [{ name: 'SubRip subtitles', extensions: ['srt'] }],
    });
    if (typeof selected === 'string') await loadSrtPath(selected);
  } catch (error) {
    console.error(error);
    setStatus(`Could not open subtitles: ${errorMessage(error)}`, 0);
  }
});

fontButton.addEventListener('click', async () => {
  try {
    const selected = await open({
      multiple: false,
      filters: [{ name: 'Font', extensions: ['ttf', 'otf', 'ttc'] }],
    });
    if (typeof selected === 'string') {
      fontPath = selected;
      $('#font-file-name').textContent = baseName(selected);
      fontClear.hidden = false;
      // A custom font may carry its own bold/italic faces, so allow toggling.
      updateFontSelectPreview();
    }
  } catch (error) {
    console.error(error);
    setStatus(`Could not open font: ${errorMessage(error)}`, 0);
  }
});

fontClear.addEventListener('click', () => {
  fontPath = undefined;
  $('#font-file-name').textContent = 'Uses bundled Google Fonts unless a file is selected';
  fontClear.hidden = true;
  // Back to a bundled family: re-apply its real bold/italic availability.
  updateFontSelectPreview();
});

// Native drag-and-drop. The webview swallows HTML5 drag events, so Tauri's
// drag-drop event is used instead. It reports absolute paths, which suits our
// path-based model. Mirrors v1: load the first MP4 and first SRT dropped.
async function loadDroppedPaths(paths) {
  // The Edit tab only cares about a dropped MP4.
  if (activeTab === 'loop') {
    const clip = paths.find((p) => /\.mp4$/i.test(p));
    if (clip) {
      await looper.loadVideo(clip);
    } else {
      setStatus('Drop an MP4 to load it into the editor', 0);
    }
    return;
  }

  // The Clip it tab takes a dropped MP4 and, optionally, an SRT. When an SRT
  // is provided it skips the transcription step and identifies against it.
  if (activeTab === 'clipit') {
    const clip = paths.find((p) => /\.mp4$/i.test(p));
    const srt = paths.find((p) => /\.srt$/i.test(p));
    if (clip) {
      $('#clipit-video-name').textContent = clip.split(/[\\/]/).pop() || clip;
      await clipit.loadVideo(clip);
    }
    if (srt) await clipit.setSrtPath(srt);
    if (!clip && !srt) {
      setStatus('Drop an MP4 (and optionally an SRT) into Clip it', 0);
    }
    return;
  }

  // The Caption tab takes a dropped MP4 and, optionally, an SRT. With an SRT
  // it skips transcription; without one, transcription runs on Run.
  if (activeTab === 'caption') {
    const clip = paths.find((p) => /\.mp4$/i.test(p));
    const srt = paths.find((p) => /\.srt$/i.test(p));
    if (clip) await caption.loadVideo(clip);
    if (srt) await caption.setSrtPath(srt);
    if (!clip && !srt) {
      setStatus('Drop an MP4 (and optionally an SRT) into Caption', 0);
    }
    return;
  }

  // The Transcribe tab takes a dropped MP4.
  if (activeTab === 'generate') {
    const clip = paths.find((p) => /\.mp4$/i.test(p));
    if (clip) {
      await generate.setVideoPath(clip);
    } else {
      setStatus('Drop an MP4 to generate an SRT', 0);
    }
    return;
  }

  const mp4 = paths.find((p) => /\.mp4$/i.test(p));
  const srt = paths.find((p) => /\.srt$/i.test(p));
  const font = paths.find((p) => /\.(ttf|otf|ttc)$/i.test(p));

  if (mp4) await loadVideoPath(mp4);
  if (srt) await loadSrtPath(srt);
  if (font) {
    fontPath = font;
    $('#font-file-name').textContent = baseName(font);
    fontClear.hidden = false;
    // A custom font may carry its own bold/italic faces, so allow toggling.
    updateFontSelectPreview();
  }
  if (!mp4 && !srt && !font) {
    setStatus('Drop an MP4, SRT, or font file', 0);
  }
}

getCurrentWebview()
  .onDragDropEvent((event) => {
    const { type } = event.payload;
    if (type === 'enter' || type === 'over') {
      document.body.classList.add('dragging-files');
    } else if (type === 'leave') {
      document.body.classList.remove('dragging-files');
    } else if (type === 'drop') {
      document.body.classList.remove('dragging-files');
      loadDroppedPaths(event.payload.paths || []).catch((error) => {
        console.error(error);
        setStatus(`Drop failed: ${errorMessage(error)}`, 0);
      });
    }
  })
  .catch((error) => console.error(error));

sourceVideo.addEventListener('timeupdate', updateSubtitle);
sourceVideo.addEventListener('seeked', updateSubtitle);
window.addEventListener('resize', updatePreviewStyle);
document.addEventListener('fullscreenchange', updateFullscreenState);
document.addEventListener('webkitfullscreenchange', updateFullscreenState);
fullscreenButton.addEventListener('click', () => {
  toggleStageFullscreen().catch((error) => {
    console.error(error);
  });
});
$('#font-select').addEventListener('change', () => {
  // Default the bold toggle to the selected font's preset weight.
  $('#bold-toggle').checked = FONTS[Number($('#font-select').value)].bold;
  updateFontSelectPreview();
  updatePreviewStyle();
});
$('#bold-toggle').addEventListener('change', () => {
  updateFontSelectPreview();
  updatePreviewStyle();
});
$('#italic-toggle').addEventListener('change', () => {
  updateFontSelectPreview();
  updatePreviewStyle();
});
$('#variation-select').addEventListener('change', () => {
  updateVariationControls();
  updatePreviewStyle();
});
$('#font-size').addEventListener('input', (event) => {
  $('#font-size-output').value = event.target.value;
  updatePreviewStyle();
});
$('#letter-spacing').addEventListener('input', (event) => {
  $('#letter-spacing-output').value = event.target.value;
  updatePreviewStyle();
});
$('#y-position').addEventListener('input', (event) => {
  $('#y-position-output').value = `${event.target.value}%`;
  updatePreviewStyle();
});
resetButton.addEventListener('click', resetStyle);

// Full reset for the Burn tab: drop the loaded MP4/SRT, clear cues + preview,
// restore the subtitle style to defaults, and reset the render tracker.
function clearBurnAll() {
  if (rendering) return;

  // Source video + SRT.
  sourceVideo.pause();
  sourceVideo.removeAttribute('src');
  sourceVideo.load();
  sourceVideo.hidden = true;
  $('#empty-state').hidden = false;
  videoPath = undefined;
  videoName = undefined;
  srtPath = undefined;
  cues = [];
  lastOutputPath = undefined;
  updateSubtitle();

  // Source file buttons + section.
  $('#video-name').textContent = 'Choose a video';
  $('#video-meta').textContent = 'No file selected';
  $('#srt-name').textContent = 'Choose subtitles';
  $('#srt-meta').textContent = 'No file selected';
  setSourceCollapsed(false);

  // Restore subtitle style + custom font to defaults.
  fontPath = undefined;
  $('#font-file-name').textContent = 'Uses bundled Google Fonts unless a file is selected';
  fontClear.hidden = true;
  resetStyle();

  // Render tracker back to idle.
  stopRenderElapsed();
  renderSpinner.classList.remove('spinning');
  setRenderStage('prepare');
  setRenderPhase('Ready');
  renderElapsedEl.textContent = '0:00';
  $('#status').textContent = 'Ready';
  $('#progress-bar').style.width = '0%';
  renderTracker.hidden = true;
  revealButton.classList.add('disabled');
  revealButton.setAttribute('aria-disabled', 'true');
  revealButton.disabled = true;

  updateReadyState();
}

burnClearButton?.addEventListener('click', clearBurnAll);

function formatDuration(seconds) {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}

function setStatus(message, progress) {
  renderTracker.hidden = false;
  $('#status').textContent = message;
  $('#progress-bar').style.width = `${Math.max(0, Math.min(100, progress))}%`;
}

function formatRenderElapsed(ms) {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function startRenderElapsed() {
  renderStartedAt = Date.now();
  renderElapsedEl.textContent = '0:00';
  clearInterval(renderElapsedTimer);
  renderElapsedTimer = setInterval(() => {
    renderElapsedEl.textContent = formatRenderElapsed(Date.now() - renderStartedAt);
  }, 1000);
}

function stopRenderElapsed() {
  clearInterval(renderElapsedTimer);
  renderElapsedTimer = null;
}

function setRenderPhase(text) {
  renderPhase.textContent = text;
}

// Mark a stage active (everything before it done); `failed` flips the current
// stage into an error state.
function setRenderStage(stage, { failed = false } = {}) {
  const activeIndex = RENDER_STAGES.indexOf(stage);
  for (const li of renderStagesEl.children) {
    const index = RENDER_STAGES.indexOf(li.dataset.stage);
    li.classList.toggle('failed', failed && index === activeIndex);
    li.classList.toggle('done', !failed && index < activeIndex);
    li.classList.toggle('active', !failed && index === activeIndex);
  }
}

function errorMessage(error) {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === 'string' && error.trim()) return error;
  if (error && typeof error === 'object') {
    if (typeof error.message === 'string' && error.message.trim()) return error.message;
    if (typeof error.toString === 'function') {
      const text = error.toString();
      if (text && text !== '[object Object]') return text;
    }
    try {
      return JSON.stringify(error);
    } catch {
      return 'Unknown error';
    }
  }
  return 'Unknown error';
}

function createCurrentAss() {
  return createAss({
    width: sourceVideo.videoWidth,
    height: sourceVideo.videoHeight,
    cues,
    style: styleState(),
  });
}

// FFmpeg progress is reported from Rust as a 0..1 fraction.
listen('render-progress', (event) => {
  if (!rendering) return;
  const progress = Number(event.payload);
  if (Number.isFinite(progress) && progress >= 0 && progress <= 1) {
    setRenderStage('render');
    setRenderPhase('Rendering MP4');
    setStatus(`Rendering MP4 · ${Math.round(progress * 100)}%`, 5 + progress * 94);
  }
});

function defaultOutputName() {
  return `${videoName.replace(/\.mp4$/i, '')}-subtitled-${timestamp()}.mp4`;
}

async function pickOutputPath() {
  const selected = await save({
    title: 'Save subtitled MP4',
    defaultPath: defaultOutputName(),
    filters: [{ name: 'MP4 video', extensions: ['mp4'] }],
  });
  return typeof selected === 'string' ? selected : undefined;
}

renderButton.addEventListener('click', async (event) => {
  if (rendering) return;

  // If a music bed is selected, ask whether to include it (anchored at the
  // click) before opening the native save dialog.
  const choice = await musicStore.pickForExport(event);
  if (choice.cancelled) return;

  const outputPath = await pickOutputPath();
  if (!outputPath) return;

  rendering = true;
  updateReadyState();
  // Collapse the source-files section to focus on the render, like Clip it.
  setSourceCollapsed(true);
  revealButton.classList.add('disabled');
  revealButton.setAttribute('aria-disabled', 'true');
  revealButton.disabled = true;
  renderTracker.hidden = false;
  renderSpinner.classList.add('spinning');
  setRenderStage('prepare');
  setRenderPhase('Preparing render');
  startRenderElapsed();
  setStatus('Preparing render…', 2);

  const { bundledFontFile } = styleState();

  try {
    const result = await invoke('render_video', {
      request: {
        input_path: videoPath,
        output_path: outputPath,
        working_directory: workingDirectory.path,
        ass_content: createCurrentAss(),
        font_path: fontPath || null,
        bundled_font_name: fontPath ? null : bundledFontFile,
        font_dir: SYSTEM_FONT_DIR,
        duration_seconds: sourceVideo.duration || null,
        ...(choice.selection || {}),
      },
    });
    lastOutputPath = result?.output_path || outputPath;
    revealButton.classList.remove('disabled');
    revealButton.setAttribute('aria-disabled', 'false');
    revealButton.disabled = false;
    setRenderStage('done');
    setRenderPhase('Done');
    setStatus('Render complete', 100);
  } catch (error) {
    console.error(error);
    setRenderStage('render', { failed: true });
    setRenderPhase('Failed');
    setStatus(`Render failed: ${errorMessage(error)}`, 0);
  } finally {
    rendering = false;
    renderSpinner.classList.remove('spinning');
    stopRenderElapsed();
    updateReadyState();
  }
});

revealButton.addEventListener('click', async () => {
  if (!lastOutputPath) return;
  try {
    await invoke('reveal_in_finder', { path: lastOutputPath });
  } catch (error) {
    console.error(error);
    setStatus(`Could not reveal file: ${errorMessage(error)}`, 100);
  }
});

themeToggle.addEventListener('click', () => {
  setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
});

setTheme(document.documentElement.dataset.theme || 'dark');
initializeControls();

// --- Edit tab ---------------------------------------------------------------

// Shared background-music store, used by every MP4-preview tab's popover (live
// preview + export selection) and the Settings page (folder management).
const musicStore = createMusicStore();

const looper = initLooper({
  music: musicStore,
});

// Clip it: takes an MP4 and generates the SRT itself (6 words/line, 4 rows)
// before running identification, or uses a supplied SRT to skip transcription.
const clipit = initStudio({
  music: musicStore,
  workingDirectory,
  prefix: 'clipit',
  mode: 'mp4',
  onStatus: (message) => {
    $('#clipit-video-meta').textContent = message;
  },
});

initMusicPopover(musicStore, {
  video: $('#clipit-video'),
  mount: document.querySelector('[data-music-mount="clipit"]'),
});

initMusicPopover(musicStore, {
  video: $('#loop-video'),
  mount: document.querySelector('[data-music-mount="loop"]'),
});

initMusicPopover(musicStore, {
  video: sourceVideo,
  mount: document.querySelector('[data-music-mount="burn"]'),
});

initMusicSettings(musicStore, {
  addButton: $('#settings-music-add'),
  list: $('#settings-music-list'),
});

initWorkingDirectorySettings(workingDirectory, {
  chooseButton: $('#settings-working-choose'),
  resetButton: $('#settings-working-reset'),
  pathElement: $('#settings-working-path'),
});

// The popover's "Manage folders in Settings" link jumps to the Settings tab.
document.addEventListener('music-settings', () => setActiveTab('settings'));

const generate = initGenerate({
  workingDirectory,
  onStatus: (message) => {
    $('#generate-result-status').textContent = message;
  },
});

// Caption: end-to-end flow. Transcribes on run when no SRT is supplied, then
// burns the styled captions into a new MP4.
const caption = initCaption({
  workingDirectory,
  music: musicStore,
  onStatus: (message) => {
    $('#caption-preview-status').textContent = message;
  },
});

initMusicPopover(musicStore, {
  video: $('#caption-video'),
  mount: document.querySelector('[data-music-mount="caption"]'),
});

let activeTab = 'clipit';

const TABS = {
  loop: { button: $('#tab-loop'), panel: $('#panel-loop') },
  clipit: { button: $('#tab-clipit'), panel: $('#panel-clipit') },
  caption: { button: $('#tab-caption'), panel: $('#panel-caption') },
  generate: { button: $('#tab-generate'), panel: $('#panel-generate') },
  settings: { button: $('#tab-settings'), panel: $('#panel-settings') },
};

// One-sentence description of each manual tool, shown under the nav so the
// currently selected tool explains itself at a glance. "Clip it" is the
// primary AI flow and needs no description.
const TOOL_DESCRIPTIONS = {
  clipit: 'Find highlights in a video, refine each clip, then export it captioned and reframed.',
  loop: 'Trim an MP4 to a range and optionally reframe it to vertical 9:16.',
  generate: 'Transcribe an MP4 to an SRT subtitle file on-device.',
  caption: 'Style and preview captions, then burn them into an MP4.',
};

const toolDescription = $('#tool-description');

// Render the description for the initially-active tab (setActiveTab early-
// returns for the current tab, so the first paint needs this).
{
  const initialDescription = TOOL_DESCRIPTIONS[activeTab];
  toolDescription.textContent = initialDescription || '';
  toolDescription.hidden = !initialDescription;
}

function setActiveTab(tab) {
  if (tab === activeTab) return;
  // Pause any video preview when leaving its tab so audio doesn't bleed across.
  if (activeTab === 'loop') looper.deactivate();
  if (activeTab === 'clipit') clipit.deactivate();
  if (activeTab === 'caption') caption.deactivate();

  activeTab = tab;
  for (const [name, { button, panel }] of Object.entries(TABS)) {
    const selected = name === tab;
    button.setAttribute('aria-selected', String(selected));
    button.classList.toggle('active', selected);
    panel.hidden = !selected;
  }

  // Describe the selected manual tool under the nav. The primary "Clip it"
  // flow has no description.
  const description = TOOL_DESCRIPTIONS[tab];
  toolDescription.textContent = description || '';
  toolDescription.hidden = !description;
}

TABS.loop.button.addEventListener('click', () => setActiveTab('loop'));
TABS.clipit.button.addEventListener('click', () => setActiveTab('clipit'));
TABS.caption.button.addEventListener('click', () => setActiveTab('caption'));
TABS.generate.button.addEventListener('click', () => setActiveTab('generate'));
TABS.settings.button.addEventListener('click', () => setActiveTab('settings'));

$('#clipit-video-button').addEventListener('click', async () => {
  try {
    const selected = await open({
      multiple: false,
      filters: [{ name: 'MP4 video', extensions: ['mp4'] }],
    });
    if (typeof selected === 'string') {
      $('#clipit-video-name').textContent = selected.split(/[\\/]/).pop() || selected;
      await clipit.loadVideo(selected);
    }
  } catch (error) {
    console.error(error);
    $('#clipit-status').textContent = `Could not open video: ${errorMessage(error)}`;
  }
});
