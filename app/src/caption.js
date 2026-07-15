import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open, save } from '@tauri-apps/plugin-dialog';
import { createAss, parseSrt } from './ass.js';
import { jsonToSrt, GENERATE_DEFAULTS } from './generate.js';
import { syncStyleToggleAvailability } from './subtitleStyle.js';
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

// Default subtitle style (shared shape with the Burn SRT tab's defaults).
const STYLE_DEFAULTS = {
  colorIndex: 7, // Orange
  fontIndex: fontIndexByFamily(DEFAULT_FONT_FAMILY), // Lato
  variationIndex: 2, // Standard outline
  outlineColorIndex: 1, // Black
  fontSize: 72,
  letterSpacing: 0,
  yPosition: 20, // 20% up from the bottom
};

// libass resolves font names against the macOS system font folder during the
// burn, so a chosen custom font file can map to its real family name.
const SYSTEM_FONT_DIR = '/System/Library/Fonts';

const DEFAULT_TRANSCRIBE_URL = 'http://localhost:5092';
const DEFAULT_TRANSCRIBE_MODEL = 'parakeet-tdt-0.6b-v3';

const baseName = (path) => path.split(/[\\/]/).pop() || path;
const errorMessage = (error) =>
  error instanceof Error ? error.message : typeof error === 'string' ? error : 'Unknown error';

/**
 * Wire up the "Caption" tab: an end-to-end captioning flow. Drop an MP4, then
 * hit Run — if no SRT is supplied, the audio is transcribed locally first, then
 * the styled subtitles are burned into a new MP4. An attached SRT skips
 * transcription. When the SRT was generated (not supplied), a "Save SRT" button
 * lets the user keep the intermediate transcript.
 */
export function initCaption({ workingDirectory, music, onStatus = () => {} } = {}) {
  const $ = (selector) => document.querySelector(selector);

  // Source files.
  const videoButton = $('#caption-video-button');
  const videoName = $('#caption-video-name');
  const videoMeta = $('#caption-video-meta');
  const srtButton = $('#caption-srt-button');
  const srtName = $('#caption-srt-name');
  const srtMeta = $('#caption-srt-meta');
  const srtClear = $('#caption-srt-clear');
  const clearButton = $('#caption-clear-button');
  const sourceSection = $('#caption-source-section');

  // Transcription knobs (used only when no SRT is supplied).
  const maxDuration = $('#caption-max-duration');
  const maxDurationOutput = $('#caption-max-duration-output');
  const maxWords = $('#caption-max-words');
  const rows = $('#caption-rows');
  const pause = $('#caption-pause');
  const baseUrlInput = $('#caption-base-url');
  const modelInput = $('#caption-model');
  const transcribeReset = $('#caption-transcribe-reset');
  const transcribeCollapse = $('#caption-transcribe-collapse');
  const transcribeSettings = $('#caption-transcribe-settings');

  // Style controls.
  const fontSelect = $('#caption-font-select');
  const variationSelect = $('#caption-variation-select');
  const boldToggle = $('#caption-bold-toggle');
  const italicToggle = $('#caption-italic-toggle');
  const fontButton = $('#caption-font-button');
  const fontClear = $('#caption-font-clear');
  const fontFileName = $('#caption-font-file-name');
  const outlineColorField = $('#caption-outline-color-field');
  const backgroundColorField = $('#caption-background-color-field');
  const fontSize = $('#caption-font-size');
  const fontSizeOutput = $('#caption-font-size-output');
  const letterSpacing = $('#caption-letter-spacing');
  const letterSpacingOutput = $('#caption-letter-spacing-output');
  const yPosition = $('#caption-y-position');
  const yPositionOutput = $('#caption-y-position-output');
  const styleReset = $('#caption-reset-button');

  // Actions + staged tracker.
  const runButton = $('#caption-run-button');
  const saveSrtButton = $('#caption-save-srt-button');
  const revealButton = $('#caption-reveal-button');
  const tracker = $('#caption-tracker');
  const spinner = $('#caption-spinner');
  const phaseEl = $('#caption-phase');
  const elapsedEl = $('#caption-elapsed');
  const stagesEl = $('#caption-stages');
  const progressBar = $('#caption-progress-bar');
  const status = $('#caption-status');

  // Ordered tracker stages. The transcribe stage is hidden when an SRT is
  // supplied (nothing to transcribe).
  const STAGES = ['transcribe', 'burn', 'done'];

  // Preview.
  const stage = $('#caption-stage');
  const previewStatus = $('#caption-preview-status');
  const emptyState = $('#caption-empty-state');
  const video = $('#caption-video');
  const subtitlePreview = $('#caption-subtitle-preview');
  const fullscreenButton = $('#caption-fullscreen-button');

  // State.
  let videoPath = '';
  let videoBaseName = '';
  let srtPath = '';
  let cues = [];
  let selectedColor = COLORS[STYLE_DEFAULTS.colorIndex][1];
  let selectedOutlineColor = COLORS[STYLE_DEFAULTS.outlineColorIndex][1];
  let selectedBackgroundColor = COLORS[1][1];
  let fontPath;
  let running = false;
  let transcribing = false;
  let rendering = false;
  let lastOutputPath = '';
  // The generated SRT text + whether cues came from transcription (vs a
  // supplied file). Save SRT is only offered for generated transcripts.
  let generatedSrt = '';
  let cuesAreGenerated = false;
  let elapsedTimer = null;
  let startedAt = 0;

  // --- Style controls -------------------------------------------------------

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

  function initStyleControls() {
    buildSwatches($('#caption-color-options'), 'caption-color', STYLE_DEFAULTS.colorIndex, (color) => {
      selectedColor = color;
    });
    buildSwatches($('#caption-outline-color-options'), 'caption-outline-color', STYLE_DEFAULTS.outlineColorIndex, (color) => {
      selectedOutlineColor = color;
    });
    buildSwatches($('#caption-background-color-options'), 'caption-background-color', 1, (color) => {
      selectedBackgroundColor = color;
    });

    // Render each option in its own typeface so the dropdown previews the font
    // (the families are already loaded via @font-face in style.css).
    FONTS.forEach(({ label, cssFont }, index) => {
      const option = new Option(label, index);
      option.style.fontFamily = cssFont;
      fontSelect.add(option);
    });
    populateVariationSelect(variationSelect);
    resetStyle();
  }

  function resetStyle() {
    selectedColor = COLORS[STYLE_DEFAULTS.colorIndex][1];
    const swatch = document.querySelector(`#caption-color-options input[value="${selectedColor}"]`);
    if (swatch) swatch.checked = true;

    selectedOutlineColor = COLORS[STYLE_DEFAULTS.outlineColorIndex][1];
    const outlineSwatch = document.querySelector(`#caption-outline-color-options input[value="${selectedOutlineColor}"]`);
    if (outlineSwatch) outlineSwatch.checked = true;

    selectedBackgroundColor = COLORS[1][1];
    const backgroundSwatch = document.querySelector(
      `#caption-background-color-options input[value="${selectedBackgroundColor}"]`,
    );
    if (backgroundSwatch) backgroundSwatch.checked = true;

    fontSelect.value = String(STYLE_DEFAULTS.fontIndex);
    variationSelect.value = String(STYLE_DEFAULTS.variationIndex);
    boldToggle.checked = FONTS[STYLE_DEFAULTS.fontIndex].bold;
    italicToggle.checked = false;

    fontSize.value = String(STYLE_DEFAULTS.fontSize);
    fontSizeOutput.value = String(STYLE_DEFAULTS.fontSize);
    letterSpacing.value = String(STYLE_DEFAULTS.letterSpacing);
    letterSpacingOutput.value = String(STYLE_DEFAULTS.letterSpacing);
    yPosition.value = String(STYLE_DEFAULTS.yPosition);
    yPositionOutput.value = `${STYLE_DEFAULTS.yPosition}%`;

    updateFontSelectPreview();
    updateVariationControls();
    updatePreviewStyle();
  }

  // Render the collapsed <select> in the currently selected font.
  function updateFontSelectPreview() {
    const font = FONTS[Number(fontSelect.value)];
    // Disable Bold/Italic when the family has no such face (a custom font file
    // may contain any face, so keep them live in that case).
    syncStyleToggleAvailability(font, { boldToggle, italicToggle }, Boolean(fontPath));
    fontSelect.style.fontFamily = font ? font.cssFont : '';
  }

  function updateVariationControls() {
    const variation = VARIATIONS[Number(variationSelect.value)][1];
    outlineColorField.hidden = Boolean(variation.box) || !variation.outline;
    backgroundColorField.hidden = !variation.box;
  }

  function styleState() {
    const font = FONTS[Number(fontSelect.value)];
    return {
      color: selectedColor,
      outlineColor: selectedOutlineColor,
      fontName: font.family,
      cssFont: font.cssFont,
      bundledFontFile: font.file,
      bold: boldToggle.checked,
      italic: italicToggle.checked,
      fontSize: Number(fontSize.value),
      letterSpacing: Number(letterSpacing.value),
      variation: {
        ...VARIATIONS[Number(variationSelect.value)][1],
        backgroundColor: selectedBackgroundColor,
      },
      yPosition: 100 - Number(yPosition.value),
    };
  }

  function previewScale() {
    if (!video.videoWidth) return 1;
    return video.clientWidth / video.videoWidth;
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
      color, outlineColor, cssFont, fontSize: size, variation,
      yPosition: yPos, bold, italic, letterSpacing: spacing,
    } = styleState();
    const scale = previewScale();
    const assScale = ASS_TO_CSS_FONT_SCALE * scale;
    const stageRect = stage.getBoundingClientRect();
    const videoRect = video.getBoundingClientRect();
    const marginX = ASS_MARGIN_X * scale;
    const shadowColor = colorWithOpacity(variation.shadowColor || '#000000', variation.shadowOpacity ?? 1);
    const boxColor = colorWithOpacity(variation.backgroundColor || '#000000', variation.backgroundOpacity ?? 0.5);
    const shadowOffset = variation.shadow * assScale;

    subtitlePreview.style.color = color;
    subtitlePreview.style.fontFamily = cssFont;
    subtitlePreview.style.fontWeight = bold ? '700' : '400';
    subtitlePreview.style.fontStyle = italic ? 'italic' : 'normal';
    subtitlePreview.style.letterSpacing = spacing ? `${spacing * assScale}px` : 'normal';
    subtitlePreview.style.fontSize = `${size * assScale}px`;
    subtitlePreview.style.webkitTextStroke =
      !variation.box && variation.outline ? `${variation.outline * assScale}px ${outlineColor}` : '0';
    subtitlePreview.style.textShadow =
      !variation.box && variation.shadow ? `${shadowOffset}px ${shadowOffset}px 0 ${shadowColor}` : 'none';
    subtitlePreview.style.background = variation.box ? boxColor : 'transparent';
    subtitlePreview.style.padding = variation.box ? `${variation.outline * assScale}px` : '0';
    subtitlePreview.style.boxShadow =
      variation.box && variation.shadow ? `${shadowOffset}px ${shadowOffset}px 0 ${boxColor}` : 'none';
    subtitlePreview.style.setProperty('--subtitle-top', `${videoRect.top - stageRect.top + videoRect.height * yPos / 100}px`);
    subtitlePreview.style.setProperty('--subtitle-left', `${videoRect.left - stageRect.left + marginX}px`);
    subtitlePreview.style.setProperty('--subtitle-width', `${Math.max(0, videoRect.width - marginX * 2)}px`);
    subtitlePreview.style.setProperty('--subtitle-center-x', `${videoRect.left - stageRect.left + videoRect.width / 2}px`);
    subtitlePreview.classList.toggle('boxed', Boolean(variation.box));
  }

  function cueHtml(text) {
    const escaped = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\n/g, '<br>');
    return escaped.replace(/&lt;(\/?[biu])\s*&gt;/gi, '<$1>');
  }

  function currentCue(time) {
    return cues.find((cue) => time >= cue.start && time <= cue.end);
  }

  function updateSubtitle() {
    const cue = currentCue(video.currentTime);
    subtitlePreview.innerHTML = cue ? cueHtml(cue.text) : '';
    subtitlePreview.classList.toggle('visible', Boolean(cue));
  }

  function createCurrentAss() {
    return createAss({
      width: video.videoWidth,
      height: video.videoHeight,
      cues,
      style: styleState(),
    });
  }

  // --- Transcription knobs --------------------------------------------------

  function resetTranscribeKnobs() {
    maxDuration.value = String(GENERATE_DEFAULTS.maxDuration);
    maxDurationOutput.value = `${GENERATE_DEFAULTS.maxDuration.toFixed(1)}s`;
    maxWords.value = String(GENERATE_DEFAULTS.maxWordsPerLine);
    rows.value = String(GENERATE_DEFAULTS.rows);
    pause.value = String(GENERATE_DEFAULTS.pause);
    baseUrlInput.value = DEFAULT_TRANSCRIBE_URL;
    modelInput.value = DEFAULT_TRANSCRIBE_MODEL;
  }

  function collectTranscribeOptions() {
    return {
      maxDuration: Number(maxDuration.value) || GENERATE_DEFAULTS.maxDuration,
      maxWordsPerLine: Number(maxWords.value) || GENERATE_DEFAULTS.maxWordsPerLine,
      rows: Number(rows.value) || GENERATE_DEFAULTS.rows,
      pause: Number(pause.value) || GENERATE_DEFAULTS.pause,
    };
  }

  // --- Ready state + status -------------------------------------------------

  function formatElapsed(ms) {
    const total = Math.floor(ms / 1000);
    const m = Math.floor(total / 60);
    return `${m}:${String(total % 60).padStart(2, '0')}`;
  }

  function startElapsed() {
    startedAt = Date.now();
    elapsedEl.textContent = '0:00';
    clearInterval(elapsedTimer);
    elapsedTimer = setInterval(() => {
      elapsedEl.textContent = formatElapsed(Date.now() - startedAt);
    }, 1000);
  }

  function stopElapsed() {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }

  function setPhase(text) {
    phaseEl.textContent = text;
  }

  // Mark a stage active (everything before it done); `failed` flips the current
  // stage to an error state. Hidden stages (e.g. transcribe when an SRT is
  // supplied) are skipped so the rail stays honest.
  function setStage(stage, { failed = false } = {}) {
    const activeIndex = STAGES.indexOf(stage);
    for (const li of stagesEl.children) {
      if (li.hidden) continue;
      const index = STAGES.indexOf(li.dataset.stage);
      li.classList.toggle('failed', failed && index === activeIndex);
      li.classList.toggle('done', !failed && index < activeIndex);
      li.classList.toggle('active', !failed && index === activeIndex);
    }
  }

  function setStatus(message, fraction) {
    tracker.hidden = false;
    status.textContent = message;
    if (typeof fraction === 'number') {
      progressBar.style.width = `${Math.max(0, Math.min(100, fraction * 100))}%`;
    }
  }

  function setRevealEnabled(enabled) {
    revealButton.classList.toggle('disabled', !enabled);
    revealButton.setAttribute('aria-disabled', String(!enabled));
    revealButton.disabled = !enabled;
  }

  function updateRunState() {
    // Runnable once a video is loaded and its dimensions are known. Cues are
    // optional up front: they're generated at run time when no SRT is supplied.
    runButton.disabled = !videoPath || !video.videoWidth || running;
    fullscreenButton.disabled = !video.videoWidth;
    const hasCues = cues.length > 0;
    previewStatus.textContent = videoPath
      ? hasCues
        ? `${cues.length} subtitle cues loaded`
        : srtPath
          ? 'Reading subtitles…'
          : 'Ready to caption · transcribes on run'
      : 'Waiting for a video';
  }

  function setSourceCollapsed(collapsed) {
    sourceSection.classList.toggle('collapsed', collapsed);
    const toggle = sourceSection.querySelector('.studio-section-toggle');
    toggle.setAttribute('aria-expanded', String(!collapsed));
    toggle.setAttribute('aria-label', collapsed ? 'Expand section' : 'Collapse section');
  }

  // --- File loading ---------------------------------------------------------

  async function loadVideo(path) {
    videoPath = path;
    videoBaseName = baseName(path);
    await invoke('allow_media_file', { path });
    video.src = await invoke('media_stream_url', { path });
    video.hidden = false;
    emptyState.hidden = true;
    tracker.hidden = true;
    saveSrtButton.hidden = true;
    videoName.textContent = videoBaseName;
    videoMeta.textContent = 'Loading…';
    onStatus('Video loaded');
    setRevealEnabled(false);
    video.addEventListener(
      'loadedmetadata',
      () => {
        videoMeta.textContent = `${video.videoWidth} × ${video.videoHeight} · ${formatDuration(video.duration)}`;
        updatePreviewStyle();
        updateRunState();
      },
      { once: true },
    );
    updateRunState();
  }

  // Grey out (and disable) the transcription settings while a supplied SRT is
  // attached, since those knobs only shape cues during transcription — which is
  // skipped when the user brings their own SRT. Collapse it too so it reads as
  // clearly inactive.
  function syncTranscribeAvailability() {
    const supplied = Boolean(srtPath);
    transcribeSettings.classList.toggle('section-disabled', supplied);
    transcribeSettings.setAttribute('aria-hidden', String(supplied));
    if (supplied) transcribeSettings.open = false;
  }

  async function setSrtPath(path) {
    const text = await invoke('read_srt', { path });
    srtPath = path;
    cues = parseSrt(text);
    cuesAreGenerated = false;
    generatedSrt = '';
    saveSrtButton.hidden = true;
    srtName.textContent = baseName(path);
    srtMeta.textContent = `${cues.length} cues · skips transcription`;
    srtClear.hidden = false;
    syncTranscribeAvailability();
    updateSubtitle();
    updateRunState();
  }

  // Clearing a supplied SRT drops its cues; Run will transcribe instead. The
  // clear button is only shown while a supplied SRT is attached, so the cues
  // being cleared here always came from that file.
  function clearSrt() {
    srtPath = '';
    cues = [];
    cuesAreGenerated = false;
    generatedSrt = '';
    saveSrtButton.hidden = true;
    srtName.textContent = 'Optional subtitles';
    srtMeta.textContent = 'Auto-generated on run if not provided';
    srtClear.hidden = true;
    syncTranscribeAvailability();
    updateSubtitle();
    updateRunState();
  }

  // Clear everything and start over: drop the loaded video + SRT, reset the
  // style and transcription knobs to defaults, and restore the empty preview.
  // Mirrors the Clip it tab's Reset button.
  function resetAll() {
    if (running) return;

    // Video + preview.
    video.pause();
    video.removeAttribute('src');
    video.load();
    video.hidden = true;
    emptyState.hidden = false;
    videoPath = '';
    videoBaseName = '';
    videoName.textContent = 'Choose a video';
    videoMeta.textContent = 'No file selected';

    // Subtitles.
    clearSrt();

    // Custom font file.
    fontPath = undefined;
    fontFileName.textContent = 'Uses bundled Google Fonts unless a file is selected';
    fontClear.hidden = true;

    // Knobs + trackers.
    resetStyle();
    resetTranscribeKnobs();
    stopElapsed();
    tracker.hidden = true;
    spinner.classList.remove('spinning');
    saveSrtButton.hidden = true;
    setRevealEnabled(false);
    lastOutputPath = '';

    onStatus('Cleared');
    updateSubtitle();
    updateRunState();
  }

  function formatDuration(seconds) {
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
  }

  // --- Transcription (only when no SRT supplied) ----------------------------

  // FFmpeg reports audio-extraction progress on this channel; map it into the
  // first slice of the overall run.
  listen('extract-audio-progress', (event) => {
    if (!transcribing) return;
    const f = Number(event.payload);
    if (Number.isFinite(f)) setStatus(`Extracting audio · ${Math.round(f * 100)}%`, f * 0.15);
  }).catch((error) => console.error(error));

  // Generate cues from the video's audio via the local Parakeet endpoint.
  // Returns the SRT text, and stashes it for the Save SRT escape hatch.
  async function transcribeToCues() {
    transcribing = true;
    let audioPath = '';
    try {
      setPhase('Extracting audio');
      setStatus('Extracting audio…', 0.02);
      audioPath = await workingDirectory.allocateFile(
        videoBaseName.replace(/\.mp4$/i, '') || 'audio',
        'mp3',
      );
      await invoke('extract_audio', {
        request: { input_path: videoPath, output_path: audioPath },
      });

      setPhase('Transcribing audio');
      setStatus('Transcribing audio · this can take a while…', 0.24);
      const raw = await invoke('transcribe_audio', {
        request: {
          audio_path: audioPath,
          base_url: baseUrlInput.value.trim() || DEFAULT_TRANSCRIBE_URL,
          model: modelInput.value.trim() || DEFAULT_TRANSCRIBE_MODEL,
        },
      });

      setPhase('Building subtitles');
      setStatus('Building subtitles…', 0.34);
      const srt = jsonToSrt(JSON.parse(raw), collectTranscribeOptions());
      cues = parseSrt(srt);
      cuesAreGenerated = true;
      generatedSrt = srt;
      updateSubtitle();
      return srt;
    } finally {
      transcribing = false;
      if (audioPath) {
        try {
          await invoke('delete_file', { path: audioPath });
        } catch (error) {
          console.error('Could not delete intermediate audio:', error);
        }
      }
    }
  }

  // FFmpeg reports burn progress as a 0..1 fraction; map into the final slice.
  listen('render-progress', (event) => {
    if (!rendering) return;
    const f = Number(event.payload);
    if (Number.isFinite(f) && f >= 0 && f <= 1) {
      setStatus(`Burning captions · ${Math.round(f * 100)}%`, 0.4 + f * 0.6);
    }
  }).catch((error) => console.error(error));

  function defaultOutputName() {
    return `${videoBaseName.replace(/\.mp4$/i, '') || 'clip'}-captioned-${timestamp()}.mp4`;
  }

  // --- Run: transcribe (if needed) then burn --------------------------------

  runButton.addEventListener('click', async (event) => {
    if (running || !videoPath || !video.videoWidth) return;

    // Ask about the music bed (anchored at the click) before the save dialog.
    const choice = music ? await music.pickForExport(event) : { selection: null };
    if (choice.cancelled) return;

    const outputPath = await save({
      title: 'Save captioned MP4',
      defaultPath: defaultOutputName(),
      filters: [{ name: 'MP4 video', extensions: ['mp4'] }],
    });
    if (typeof outputPath !== 'string') return;

    const willTranscribe = !srtPath;
    // Hide the transcribe stage from the rail when an SRT is supplied.
    const transcribeStage = stagesEl.querySelector('[data-stage="transcribe"]');
    if (transcribeStage) transcribeStage.hidden = !willTranscribe;

    running = true;
    updateRunState();
    setRevealEnabled(false);
    saveSrtButton.hidden = true;
    runButton.querySelector('span').textContent = 'Working…';
    spinner.classList.add('spinning');
    startElapsed();

    try {
      // 1. Get cues. Supplied SRT is used as-is; otherwise transcribe now.
      if (willTranscribe) {
        setStage('transcribe');
        await transcribeToCues();
      } else if (!cues.length) {
        // SRT chosen but not yet parsed (shouldn't happen, but be safe).
        await setSrtPath(srtPath);
      }
      if (!cues.length) {
        throw new Error('No subtitle cues to burn.');
      }

      // 2. Burn the styled subtitles into the video.
      rendering = true;
      setStage('burn');
      setPhase('Burning captions');
      setStatus('Burning captions…', 0.4);
      const { bundledFontFile } = styleState();
      const result = await invoke('render_video', {
        request: {
          input_path: videoPath,
          output_path: outputPath,
          working_directory: workingDirectory.path,
          ass_content: createCurrentAss(),
          font_path: fontPath || null,
          bundled_font_name: fontPath ? null : bundledFontFile,
          font_dir: SYSTEM_FONT_DIR,
          duration_seconds: video.duration || null,
          ...(choice.selection || {}),
        },
      });
      rendering = false;

      lastOutputPath = result?.output_path || outputPath;
      setStage('done');
      setPhase('Complete');
      setStatus(`Done in ${formatElapsed(Date.now() - startedAt)} · ${baseName(lastOutputPath)}`, 1);
      onStatus('Caption complete');
      setRevealEnabled(true);
      // Offer the transcript only when we generated it this run.
      saveSrtButton.hidden = !cuesAreGenerated;
    } catch (error) {
      console.error(error);
      // Flag whichever stage was in flight.
      setStage(rendering ? 'burn' : 'transcribe', { failed: true });
      setPhase('Failed');
      setStatus(`Caption failed: ${errorMessage(error)}`, 0);
    } finally {
      running = false;
      rendering = false;
      transcribing = false;
      spinner.classList.remove('spinning');
      stopElapsed();
      runButton.querySelector('span').textContent = 'Render MP4';
      updateRunState();
    }
  });

  // --- Save SRT escape hatch (generated transcripts only) -------------------

  saveSrtButton.addEventListener('click', async () => {
    if (!generatedSrt) return;
    try {
      const target = await save({
        title: 'Save generated SRT',
        defaultPath: `${videoBaseName.replace(/\.mp4$/i, '') || 'transcript'}-${timestamp()}.srt`,
        filters: [{ name: 'SubRip subtitles', extensions: ['srt'] }],
      });
      if (typeof target !== 'string') return;
      await invoke('save_text_file', { path: target, contents: generatedSrt });
      onStatus(`Saved ${baseName(target)}`);
    } catch (error) {
      console.error(error);
      setStatus(`Could not save SRT: ${errorMessage(error)}`);
    }
  });

  revealButton.addEventListener('click', async () => {
    if (!lastOutputPath) return;
    try {
      await invoke('reveal_in_finder', { path: lastOutputPath });
    } catch (error) {
      console.error(error);
      setStatus(`Could not reveal file: ${errorMessage(error)}`);
    }
  });

  // --- Fullscreen preview ---------------------------------------------------

  function fullscreenElement() {
    return document.fullscreenElement || document.webkitFullscreenElement;
  }

  async function toggleFullscreen() {
    if (!video.videoWidth) return;
    if (fullscreenElement() === stage) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      await exit.call(document);
      return;
    }
    const request = stage.requestFullscreen || stage.webkitRequestFullscreen;
    await request.call(stage);
  }

  function updateFullscreenButton() {
    const active = fullscreenElement() === stage;
    fullscreenButton.textContent = active ? '×' : '⛶';
    fullscreenButton.title = active ? 'Exit fullscreen preview' : 'Fullscreen preview';
    fullscreenButton.setAttribute('aria-label', fullscreenButton.title);
    requestAnimationFrame(updatePreviewStyle);
  }

  // --- Event wiring ---------------------------------------------------------

  videoButton.addEventListener('click', async () => {
    try {
      const selected = await open({
        multiple: false,
        filters: [{ name: 'MP4 video', extensions: ['mp4'] }],
      });
      if (typeof selected === 'string') await loadVideo(selected);
    } catch (error) {
      console.error(error);
      setStatus(`Could not open video: ${errorMessage(error)}`);
    }
  });

  srtButton.addEventListener('click', async () => {
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

  srtClear.addEventListener('click', clearSrt);
  clearButton.addEventListener('click', resetAll);
  sourceSection.querySelector('.studio-section-toggle').addEventListener('click', () => {
    setSourceCollapsed(!sourceSection.classList.contains('collapsed'));
  });

  fontButton.addEventListener('click', async () => {
    try {
      const selected = await open({
        multiple: false,
        filters: [{ name: 'Font', extensions: ['ttf', 'otf', 'ttc'] }],
      });
      if (typeof selected === 'string') {
        fontPath = selected;
        fontFileName.textContent = baseName(selected);
        fontClear.hidden = false;
        // A custom font may carry its own bold/italic faces, so allow toggling.
        updateFontSelectPreview();
      }
    } catch (error) {
      console.error(error);
      setStatus(`Could not open font: ${errorMessage(error)}`);
    }
  });

  fontClear.addEventListener('click', () => {
    fontPath = undefined;
    fontFileName.textContent = 'Uses bundled Google Fonts unless a file is selected';
    fontClear.hidden = true;
    // Back to a bundled family: re-apply its real bold/italic availability.
    updateFontSelectPreview();
  });

  fontSelect.addEventListener('change', () => {
    boldToggle.checked = FONTS[Number(fontSelect.value)].bold;
    updateFontSelectPreview();
    updatePreviewStyle();
  });
  boldToggle.addEventListener('change', () => {
    updateFontSelectPreview();
    updatePreviewStyle();
  });
  italicToggle.addEventListener('change', () => {
    updateFontSelectPreview();
    updatePreviewStyle();
  });
  variationSelect.addEventListener('change', () => {
    updateVariationControls();
    updatePreviewStyle();
  });
  fontSize.addEventListener('input', (event) => {
    fontSizeOutput.value = event.target.value;
    updatePreviewStyle();
  });
  letterSpacing.addEventListener('input', (event) => {
    letterSpacingOutput.value = event.target.value;
    updatePreviewStyle();
  });
  yPosition.addEventListener('input', (event) => {
    yPositionOutput.value = `${event.target.value}%`;
    updatePreviewStyle();
  });
  maxDuration.addEventListener('input', () => {
    maxDurationOutput.value = `${Number(maxDuration.value).toFixed(1)}s`;
  });
  styleReset.addEventListener('click', resetStyle);
  transcribeReset.addEventListener('click', resetTranscribeKnobs);
  transcribeCollapse.addEventListener('click', () => {
    transcribeSettings.open = false;
  });

  video.addEventListener('timeupdate', updateSubtitle);
  video.addEventListener('seeked', updateSubtitle);
  window.addEventListener('resize', updatePreviewStyle);
  document.addEventListener('fullscreenchange', updateFullscreenButton);
  document.addEventListener('webkitfullscreenchange', updateFullscreenButton);
  fullscreenButton.addEventListener('click', () => {
    toggleFullscreen().catch((error) => console.error(error));
  });

  initStyleControls();
  resetTranscribeKnobs();

  return {
    loadVideo,
    setSrtPath,
    deactivate() {
      video.pause();
    },
  };
}
