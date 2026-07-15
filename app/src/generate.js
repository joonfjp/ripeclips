import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open } from '@tauri-apps/plugin-dialog';

// Turns Parakeet verbose_json output into readable SRT cues (ported from the
// reference Python implementation). The "Generate SRT" tab drives the full
// pipeline:
//   1. extract the MP4's audio to a sibling MP3 (ffmpeg)
//   2. POST the MP3 to the local Parakeet transcription endpoint
//   3. convert the verbose_json into an SRT next to the MP4
//   4. delete the intermediate MP3 + JSON

// Defaults match the reference implementation.
export const GENERATE_DEFAULTS = {
  maxDuration: 4.0,
  maxWordsPerLine: 6,
  rows: 2,
  pause: 0.6,
};

const DEFAULT_BASE_URL = 'http://localhost:5092';
const DEFAULT_MODEL = 'parakeet-tdt-0.6b-v3';

// Ordered pipeline stages reflected in the tracker rail.
const STAGES = ['extract', 'transcribe', 'subtitles', 'done'];

// \b[\w]+(?:['’][\w]+)*\b — words, allowing internal apostrophes.
const WORD_RE = /[A-Za-z0-9_\u00C0-\uFFFF]+(?:['’][A-Za-z0-9_\u00C0-\uFFFF]+)*/gu;
// Sentence-ending punctuation, optionally followed by closing quotes/brackets.
const BREAK_RE = /[.!?]["')\]]*$/;

const baseName = (path) => path.split(/[\\/]/).pop() || path;
const errorMessage = (error) =>
  error instanceof Error ? error.message : typeof error === 'string' ? error : 'Unknown error';

// Parse an SRT timecode "HH:MM:SS,mmm" into seconds.
function srtTimeToSeconds(stamp) {
  const match = /(\d+):(\d+):(\d+)[,.](\d+)/.exec(stamp.trim());
  if (!match) return 0;
  const [, h, m, s, ms] = match;
  return Number(h) * 3600 + Number(m) * 60 + Number(s) + Number(ms) / 1000;
}

// Compact M:SS label for cue durations / totals.
function shortTime(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

// Parse a full SRT document into cue objects for the viewer.
function parseSrtCues(srt) {
  const cues = [];
  for (const block of srt.trim().split(/\r?\n\r?\n/)) {
    const lines = block.split(/\r?\n/);
    if (lines.length < 2) continue;
    // Index line is optional/numeric; the timing line contains "-->".
    const timingIndex = lines.findIndex((l) => l.includes('-->'));
    if (timingIndex === -1) continue;
    const index = Number(lines[timingIndex - 1] ?? lines[0]) || cues.length + 1;
    const [start, end] = lines[timingIndex].split('-->').map(srtTimeToSeconds);
    const text = lines.slice(timingIndex + 1).join('\n');
    cues.push({ index, start, end, text });
  }
  return cues;
}

function isAlnum(ch) {
  return /[\p{L}\p{N}]/u.test(ch);
}

function srtTime(seconds) {
  let ms = Math.max(0, Math.round(seconds * 1000));
  const hours = Math.floor(ms / 3_600_000);
  ms -= hours * 3_600_000;
  const minutes = Math.floor(ms / 60_000);
  ms -= minutes * 60_000;
  const secs = Math.floor(ms / 1000);
  ms -= secs * 1000;
  const pad = (n, w = 2) => String(n).padStart(w, '0');
  return `${pad(hours)}:${pad(minutes)}:${pad(secs)},${pad(ms, 3)}`;
}

// Align subword timing fragments to whole words in the complete transcript.
// Mirrors timed_words() in the Python script, including its strict checks.
function timedWords(data) {
  const fragments = data.words || [];
  const transcript = data.text || '';

  // One timing entry per alphanumeric character across the model tokens.
  const charTimes = [];
  for (const fragment of fragments) {
    const start = Number(fragment.start);
    const end = Number(fragment.end);
    const chars = [...String(fragment.word ?? '')].filter(isAlnum).map((c) => c.toLowerCase());
    if (!chars.length) continue;
    const step = (end - start) / chars.length;
    chars.forEach((char, i) => {
      charTimes.push([char, start + i * step, start + (i + 1) * step]);
    });
  }

  const result = [];
  let cursor = 0;
  for (const match of transcript.matchAll(WORD_RE)) {
    const text = match[0];
    const chars = [...text].filter(isAlnum).map((c) => c.toLowerCase());
    if (!chars.length) continue;

    const available = charTimes.slice(cursor, cursor + chars.length).map((e) => e[0]);
    if (available.length !== chars.length || available.some((c, i) => c !== chars[i])) {
      throw new Error(`Could not align transcript word "${text}" at character ${match.index}`);
    }

    const start = charTimes[cursor][1];
    const end = charTimes[cursor + chars.length - 1][2];
    cursor += chars.length;

    // Preserve trailing punctuation so sentence boundaries survive.
    const after = match.index + text.length;
    const rest = transcript.slice(after);
    const nextMatch = rest.match(/[A-Za-z0-9_\u00C0-\uFFFF]/u);
    const nextStart = nextMatch ? after + nextMatch.index : transcript.length;
    const suffix = transcript.slice(after, nextStart).trim();
    result.push({ text: text + suffix, start, end });
  }

  if (cursor !== charTimes.length) {
    throw new Error('Timed fragments contain text not present in transcript');
  }
  return result;
}

// Group timed words into cues. Mirrors make_cues().
function makeCues(words, { maxDuration, maxWordsPerLine, rows, pause }) {
  const cues = [];
  let current = [];
  const maxWordsPerCue = maxWordsPerLine * rows;

  for (const word of words) {
    const candidate = [...current, word];
    const duration = word.end - candidate[0].start;
    const gap = current.length ? word.start - current[current.length - 1].end : 0;

    if (
      current.length &&
      (duration > maxDuration || candidate.length > maxWordsPerCue || gap >= pause)
    ) {
      cues.push(current);
      current = [word];
    } else {
      current = candidate;
    }

    if (BREAK_RE.test(word.text)) {
      cues.push(current);
      current = [];
    }
  }

  if (current.length) cues.push(current);

  const formatLines = (cue) => {
    const lines = [];
    for (let start = 0; start < cue.length; start += maxWordsPerLine) {
      lines.push(cue.slice(start, start + maxWordsPerLine).map((w) => w.text).join(' '));
    }
    return lines.join('\n');
  };

  return cues
    .filter((cue) => cue.length)
    .map((cue) => ({
      start: cue[0].start,
      end: Math.max(cue[cue.length - 1].end, cue[0].start + 0.2),
      text: formatLines(cue),
    }));
}

// Build a full SRT document from a parsed verbose_json object.
export function jsonToSrt(data, options) {
  const settings = { ...GENERATE_DEFAULTS, ...options };
  if (settings.maxWordsPerLine < 1) throw new Error('Max words per line must be at least 1');
  if (settings.rows < 1) throw new Error('Rows must be at least 1');

  const cues = makeCues(timedWords(data), settings);
  return (
    cues
      .map(
        (cue, i) =>
          `${i + 1}\n${srtTime(cue.start)} --> ${srtTime(cue.end)}\n${cue.text}`,
      )
      .join('\n\n') + '\n'
  );
}

/**
 * Wire up the "Generate SRT" tab: drop or choose an MP4, configure the
 * cue-formatting knobs, then run extract → transcribe → convert → cleanup.
 */
export function initGenerate({ onStatus = () => {}, workingDirectory } = {}) {
  const $ = (selector) => document.querySelector(selector);

  const videoButton = $('#generate-video-button');
  const videoName = $('#generate-video-name');
  const videoMeta = $('#generate-video-meta');

  const maxDuration = $('#generate-max-duration');
  const maxDurationOutput = $('#generate-max-duration-output');
  const maxWords = $('#generate-max-words');
  const rows = $('#generate-rows');
  const pause = $('#generate-pause');
  const baseUrlInput = $('#generate-base-url');
  const modelInput = $('#generate-model');

  const resetButton = $('#generate-reset');
  const clearButton = $('#generate-clear-button');
  const runButton = $('#generate-run-button');
  const revealButton = $('#generate-reveal-button');

  const tracker = $('#generate-tracker');
  const spinner = $('#generate-spinner');
  const phaseEl = $('#generate-phase');
  const elapsedEl = $('#generate-elapsed');
  const stagesEl = $('#generate-stages');
  const progressBar = $('#generate-progress-bar');
  const status = $('#generate-status');

  const emptyState = $('#generate-empty-state');
  const srtViewer = $('#generate-srt-viewer');
  const srtCuesEl = $('#generate-srt-cues');
  const cueCountEl = $('#generate-cue-count');
  const totalDurEl = $('#generate-total-dur');
  const wordCountEl = $('#generate-word-count');
  const copyButton = $('#generate-copy-button');

  let videoPath = '';
  let running = false;
  let lastOutputPath = '';
  let lastSrt = '';
  let elapsedTimer = null;
  let startedAt = 0;
  // Live fraction (0..1) from FFmpeg while the extract stage is running.
  let extracting = false;

  function formatElapsed(ms) {
    const total = Math.floor(ms / 1000);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
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

  // Mark a stage active (everything before it done); `failed` flips the current
  // stage to an error state.
  function setStage(stage, { failed = false } = {}) {
    const activeIndex = STAGES.indexOf(stage);
    for (const li of stagesEl.children) {
      const index = STAGES.indexOf(li.dataset.stage);
      li.classList.toggle('failed', failed && index === activeIndex);
      li.classList.toggle('done', !failed && index < activeIndex);
      li.classList.toggle('active', !failed && index === activeIndex);
    }
  }

  function setPhase(text) {
    phaseEl.textContent = text;
  }

  function setStatus(message, fraction) {
    tracker.hidden = false;
    status.textContent = message;
    if (typeof fraction === 'number') {
      progressBar.style.width = `${Math.max(0, Math.min(100, fraction * 100))}%`;
    }
  }

  function updateRunState() {
    runButton.disabled = !videoPath || running;
  }

  function setRevealEnabled(enabled) {
    revealButton.hidden = !enabled;
    revealButton.disabled = !enabled;
  }

  // Render the finished SRT as a stack of cue cards in the right column.
  function renderSrt(srt) {
    const cues = parseSrtCues(srt);
    const totalSpoken = cues.reduce((sum, c) => sum + Math.max(0, c.end - c.start), 0);
    const words = cues.reduce(
      (sum, c) => sum + (c.text.trim() ? c.text.trim().split(/\s+/).length : 0),
      0,
    );

    cueCountEl.textContent = String(cues.length);
    totalDurEl.textContent = shortTime(totalSpoken);
    wordCountEl.textContent = String(words);

    srtCuesEl.replaceChildren();
    cues.forEach((cue, i) => {
      const card = document.createElement('article');
      card.className = 'srt-cue';
      // Stagger the entrance animation slightly for a cascade effect.
      card.style.animationDelay = `${Math.min(i, 20) * 0.02}s`;
      card.innerHTML = `
        <div class="srt-cue-index">
          <small>cue</small>
          ${cue.index}
        </div>
        <div class="srt-cue-body">
          <div class="srt-cue-time">
            <span>${formatStamp(cue.start)}</span>
            <span class="srt-arrow">→</span>
            <span>${formatStamp(cue.end)}</span>
            <span class="srt-cue-dur">${(cue.end - cue.start).toFixed(1)}s</span>
          </div>
          <div class="srt-cue-text"></div>
        </div>
      `;
      // Use textContent so transcript text can never inject markup.
      card.querySelector('.srt-cue-text').textContent = cue.text;
      srtCuesEl.append(card);
    });

    emptyState.hidden = true;
    srtViewer.hidden = false;
  }

  // Full HH:MM:SS.mmm-ish stamp for a cue edge.
  function formatStamp(seconds) {
    const total = Math.max(0, seconds);
    const m = Math.floor(total / 60);
    const s = Math.floor(total % 60);
    const ms = Math.round((total % 1) * 1000);
    return `${m}:${String(s).padStart(2, '0')}.${String(ms).padStart(3, '0')}`;
  }

  copyButton.addEventListener('click', async () => {
    if (!lastSrt) return;
    try {
      await navigator.clipboard.writeText(lastSrt);
      const label = copyButton.querySelector('span');
      const original = label.textContent;
      copyButton.classList.add('copied');
      label.textContent = 'Copied';
      setTimeout(() => {
        copyButton.classList.remove('copied');
        label.textContent = original;
      }, 1500);
    } catch (error) {
      console.error(error);
    }
  });

  // The whole run spans 0..1; each stage occupies a slice of that range.
  // Extract: 0–0.30, Transcribe: 0.30–0.85, Subtitles: 0.85–1.0.
  const EXTRACT_SPAN = [0, 0.3];
  const lerp = ([lo, hi], f) => lo + (hi - lo) * Math.max(0, Math.min(1, f));

  // FFmpeg reports extraction progress as a 0..1 fraction on this channel.
  listen('extract-audio-progress', (event) => {
    if (!extracting) return;
    const f = Number(event.payload);
    if (Number.isFinite(f)) {
      setStatus(`Extracting audio · ${Math.round(f * 100)}%`, lerp(EXTRACT_SPAN, f));
    }
  }).catch((error) => console.error(error));

  async function setVideoPath(path) {
    videoPath = path;
    videoName.textContent = baseName(path);
    videoMeta.textContent = 'Ready to generate';
    onStatus('Ready to generate');
    setRevealEnabled(false);
    updateRunState();
  }

  videoButton.addEventListener('click', async () => {
    try {
      const selected = await open({
        multiple: false,
        filters: [{ name: 'MP4 video', extensions: ['mp4'] }],
      });
      if (typeof selected === 'string') await setVideoPath(selected);
    } catch (error) {
      console.error(error);
      setStatus(`Could not open video: ${errorMessage(error)}`);
    }
  });

  maxDuration.addEventListener('input', () => {
    maxDurationOutput.value = `${Number(maxDuration.value).toFixed(1)}s`;
  });

  function resetKnobs() {
    maxDuration.value = String(GENERATE_DEFAULTS.maxDuration);
    maxDurationOutput.value = `${GENERATE_DEFAULTS.maxDuration.toFixed(1)}s`;
    maxWords.value = String(GENERATE_DEFAULTS.maxWordsPerLine);
    rows.value = String(GENERATE_DEFAULTS.rows);
    pause.value = String(GENERATE_DEFAULTS.pause);
    baseUrlInput.value = DEFAULT_BASE_URL;
    modelInput.value = DEFAULT_MODEL;
  }

  resetButton.addEventListener('click', resetKnobs);

  function collectOptions() {
    return {
      maxDuration: Number(maxDuration.value) || GENERATE_DEFAULTS.maxDuration,
      maxWordsPerLine: Number(maxWords.value) || GENERATE_DEFAULTS.maxWordsPerLine,
      rows: Number(rows.value) || GENERATE_DEFAULTS.rows,
      pause: Number(pause.value) || GENERATE_DEFAULTS.pause,
    };
  }

  // Sibling path with a timestamp suffix so repeated runs don't collide,
  // e.g. clip.mp4 -> clip-20260628-153012.srt.
  function timestampedPath(ext) {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const stamp =
      `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
      `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    return videoPath.replace(/\.mp4$/i, `-${stamp}.${ext}`);
  }

  runButton.addEventListener('click', async () => {
    if (running || !videoPath) return;
    running = true;
    updateRunState();
    setRevealEnabled(false);
    runButton.querySelector('span').textContent = 'Generating…';
    spinner.classList.add('spinning');
    startElapsed();

    const srtPath = timestampedPath('srt');
    let audioPath = '';
    let audioCreated = false;

    try {
      // 1. Extract audio.
      setStage('extract');
      setPhase('Extracting audio');
      setStatus('Extracting audio…', EXTRACT_SPAN[0]);
      extracting = true;
      audioPath = await workingDirectory.allocateFile(
        videoPath.split(/[\\/]/).pop()?.replace(/\.mp4$/i, '') || 'audio',
        'mp3',
      );
      await invoke('extract_audio', {
        request: { input_path: videoPath, output_path: audioPath },
      });
      extracting = false;
      audioCreated = true;

      // 2. Transcribe via Parakeet.
      setStage('transcribe');
      setPhase('Transcribing audio');
      setStatus('Transcribing audio · this can take a while…', 0.32);
      const raw = await invoke('transcribe_audio', {
        request: {
          audio_path: audioPath,
          base_url: baseUrlInput.value.trim() || DEFAULT_BASE_URL,
          model: modelInput.value.trim() || DEFAULT_MODEL,
        },
      });

      // 3. Convert verbose_json to SRT.
      setStage('subtitles');
      setPhase('Building subtitles');
      setStatus('Building subtitles…', 0.88);
      const data = JSON.parse(raw);
      const srt = jsonToSrt(data, collectOptions());
      const cueCount = (srt.match(/-->/g) || []).length;
      await invoke('save_text_file', { path: srtPath, contents: srt });
      lastOutputPath = srtPath;
      lastSrt = srt;
      renderSrt(srt);

      setStage('done');
      setPhase('Complete');
      setStatus(
        `Done in ${formatElapsed(Date.now() - startedAt)} · ${cueCount} cues → ${baseName(srtPath)}`,
        1,
      );
      onStatus(`Saved ${baseName(srtPath)}`);
      setRevealEnabled(true);
    } catch (error) {
      console.error(error);
      // Flag whichever stage was in flight.
      const failedStage = extracting ? 'extract' : audioCreated ? 'transcribe' : 'extract';
      setStage(failedStage, { failed: true });
      setPhase('Failed');
      setStatus(`Generate failed: ${errorMessage(error)}`, 0);
    } finally {
      extracting = false;
      spinner.classList.remove('spinning');
      stopElapsed();
      // 4. Clean up the working-directory intermediate. JSON stays in memory.
      if (audioPath) {
        try {
          await invoke('delete_file', { path: audioPath });
        } catch (error) {
          console.error('Could not delete audio:', error);
        }
      }
      running = false;
      runButton.querySelector('span').textContent = 'Generate SRT';
      updateRunState();
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

  // Full reset: drop the loaded video, clear the generated SRT viewer, restore
  // every knob to its default, and reset the run tracker to idle.
  function clearAll() {
    if (running) return;

    videoPath = '';
    lastOutputPath = '';
    lastSrt = '';
    videoName.textContent = 'Choose a video';
    videoMeta.textContent = 'No file selected';
    onStatus('Waiting for a video');

    // SRT viewer back to the empty state.
    srtViewer.hidden = true;
    emptyState.hidden = false;
    srtCuesEl.replaceChildren();
    cueCountEl.textContent = '0';
    totalDurEl.textContent = '0:00';
    wordCountEl.textContent = '0';

    // Knobs to defaults.
    resetKnobs();

    // Run tracker back to idle.
    stopElapsed();
    spinner.classList.remove('spinning');
    setStage('extract');
    setPhase('Ready');
    elapsedEl.textContent = '0:00';
    status.textContent = 'Ready';
    progressBar.style.width = '0%';
    tracker.hidden = true;
    setRevealEnabled(false);

    updateRunState();
  }

  clearButton?.addEventListener('click', clearAll);

  resetKnobs();

  return { setVideoPath };
}
