import { invoke } from '@tauri-apps/api/core';
import { open } from '@tauri-apps/plugin-dialog';

// Persisted list of background-music directories and the last-used volume.
const DIRS_KEY = 'ripe-clips-music-dirs';
const VOLUME_KEY = 'ripe-clips-music-volume';

// Volume is a 0..1 gain. Default 10%, capped at 50% so the bed never overpowers
// the clip's own audio.
const DEFAULT_VOLUME = 0.1;
const MAX_VOLUME = 0.5;

const baseName = (path) => path.split(/[\\/]/).pop() || path;
const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));

function loadDirs() {
  try {
    const raw = JSON.parse(localStorage.getItem(DIRS_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter((d) => typeof d === 'string') : [];
  } catch {
    return [];
  }
}

function saveDirs(dirs) {
  try {
    localStorage.setItem(DIRS_KEY, JSON.stringify(dirs));
  } catch {
    // Directories still apply for the session when storage is unavailable.
  }
}

function loadVolume() {
  try {
    const value = Number(localStorage.getItem(VOLUME_KEY));
    if (Number.isFinite(value)) return clamp(value, 0, MAX_VOLUME);
  } catch {
    // Fall through to the default.
  }
  return DEFAULT_VOLUME;
}

function saveVolume(volume) {
  try {
    localStorage.setItem(VOLUME_KEY, String(volume));
  } catch {
    // Volume still applies for the session when storage is unavailable.
  }
}

/**
 * Shared background-music store. Owns the set of music directories (persisted
 * to localStorage), the scanned track list, the selected track, and the chosen
 * volume. Every Clip it / Trim / Burn popover binds to this one
 * instance so they stay in sync, and exporters read `selectionForExport()` to
 * mix the chosen bed into rendered/trimmed MP4s.
 */
export function createMusicStore() {
  let dirs = loadDirs();
  let tracks = [];
  let selectedPath = '';
  let volume = loadVolume();
  let loading = false;
  const listeners = new Set();

  const notify = () => {
    for (const fn of listeners) fn(snapshot());
  };

  function snapshot() {
    return {
      dirs: [...dirs],
      tracks: [...tracks],
      selectedPath,
      selectedTrack: tracks.find((t) => t.path === selectedPath) || null,
      volume,
      loading,
    };
  }

  function subscribe(fn) {
    listeners.add(fn);
    fn(snapshot());
    return () => listeners.delete(fn);
  }

  async function refreshTracks() {
    loading = true;
    notify();
    try {
      tracks = await invoke('list_audio_files', { directories: dirs });
    } catch (error) {
      console.error(error);
      tracks = [];
    }
    // Drop a stale selection if the file vanished from the folders.
    if (selectedPath && !tracks.some((t) => t.path === selectedPath)) {
      selectedPath = '';
    }
    loading = false;
    notify();
  }

  async function addDirectory() {
    let selected;
    try {
      selected = await open({
        title: 'Choose a folder of background music',
        directory: true,
        multiple: false,
      });
    } catch (error) {
      console.error(error);
      return;
    }
    if (typeof selected !== 'string') return;
    if (!dirs.includes(selected)) {
      dirs = [...dirs, selected];
      saveDirs(dirs);
      await refreshTracks();
    }
  }

  function removeDirectory(dir) {
    dirs = dirs.filter((d) => d !== dir);
    saveDirs(dirs);
    return refreshTracks();
  }

  function setVolume(next) {
    volume = clamp(Number(next) || 0, 0, MAX_VOLUME);
    saveVolume(volume);
    notify();
  }

  function selectTrack(path) {
    selectedPath = selectedPath === path ? '' : path;
    notify();
  }

  function clearSelection() {
    if (!selectedPath) return;
    selectedPath = '';
    notify();
  }

  // What an exporter needs: the chosen file path and the 0..1 gain, or null
  // when nothing is selected.
  function selectionForExport() {
    if (!selectedPath) return null;
    return { music_path: selectedPath, music_volume: volume };
  }

  /**
   * Resolve the music portion of an export started by a click. When a track is
   * selected, show the with/without-music chooser at the click point and honor
   * the answer. Returns:
   *   { cancelled: true }                 — user dismissed the chooser
   *   { cancelled: false, selection }     — selection is the export fields or null
   */
  async function pickForExport(event) {
    if (!selectedPath) return { cancelled: false, selection: null };
    const choice = await promptMusicChoice(event, snapshot().selectedTrack);
    if (choice == null) return { cancelled: true };
    return {
      cancelled: false,
      selection: choice === 'with' ? selectionForExport() : null,
    };
  }

  // Prime the track list from the saved directories.
  refreshTracks();

  return {
    subscribe,
    snapshot,
    refreshTracks,
    addDirectory,
    removeDirectory,
    setVolume,
    selectTrack,
    clearSelection,
    selectionForExport,
    pickForExport,
    get hasSelection() {
      return Boolean(selectedPath);
    },
    MAX_VOLUME,
    DEFAULT_VOLUME,
  };
}

// Build the music button + popover DOM for one preview tab. Returns the root
// element plus the inner references the controller needs. No IDs are used so
// the control can be mounted on several tabs at once.
function buildControlDom() {
  const root = document.createElement('div');
  root.className = 'music-control';
  root.innerHTML = `
    <button class="music-button" type="button" aria-haspopup="true" aria-expanded="false" title="Background music">
      <span class="music-button-icon" aria-hidden="true">♪</span>
      <span class="music-button-label">Music</span>
    </button>
    <div class="music-popover" role="dialog" aria-label="Background music" hidden>
      <div class="music-popover-head">
        <span class="label">Background music</span>
        <button class="music-stop" type="button" hidden>Clear</button>
      </div>
      <label class="range-field music-volume-field">
        <span>Volume <output class="music-volume-output">10%</output></span>
        <input class="music-volume" type="range" min="0" max="50" step="1" value="10" />
      </label>
      <div class="music-empty">
        <p>No background tracks loaded.</p>
        <button class="render-button music-add-inline" type="button">
          <span>Add a music folder</span>
          <span aria-hidden="true">+</span>
        </button>
      </div>
      <div class="music-track-list" hidden></div>
      <button class="reset-button music-settings-link" type="button">Manage folders in Settings</button>
    </div>
  `;
  return {
    root,
    button: root.querySelector('.music-button'),
    label: root.querySelector('.music-button-label'),
    popover: root.querySelector('.music-popover'),
    volumeSlider: root.querySelector('.music-volume'),
    volumeOutput: root.querySelector('.music-volume-output'),
    trackList: root.querySelector('.music-track-list'),
    emptyState: root.querySelector('.music-empty'),
    addButton: root.querySelector('.music-add-inline'),
    settingsLink: root.querySelector('.music-settings-link'),
    stopButton: root.querySelector('.music-stop'),
  };
}

/**
 * Mount the music popover into `mount` (a preview header) and wire it to the
 * shared store. The dropdown has a volume slider on top and the loaded tracks
 * below, with an empty-state shortcut to add a folder. While a track is
 * selected it plays a looping live preview synced to `video`.
 */
export function initMusicPopover(store, { video, mount }) {
  const dom = buildControlDom();
  mount.append(dom.root);
  const {
    button, label, popover, volumeSlider, volumeOutput,
    trackList, emptyState, addButton, settingsLink, stopButton,
  } = dom;

  // Hidden <audio> element that mirrors the selected bed during preview.
  const previewAudio = new Audio();
  previewAudio.loop = true;
  let previewPath = '';

  const pctLabel = (volume) => `${Math.round(volume * 100)}%`;

  function setOpen(open) {
    popover.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open) store.refreshTracks();
  }

  const isOpen = () => !popover.hidden;

  button.addEventListener('click', (event) => {
    event.stopPropagation();
    setOpen(popover.hidden);
  });

  // Close when clicking outside the popover/button.
  document.addEventListener('click', (event) => {
    if (!isOpen()) return;
    if (popover.contains(event.target) || button.contains(event.target)) return;
    setOpen(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && isOpen()) setOpen(false);
  });

  volumeSlider.max = String(Math.round(store.MAX_VOLUME * 100));
  volumeSlider.addEventListener('input', () => {
    store.setVolume(Number(volumeSlider.value) / 100);
  });

  addButton.addEventListener('click', () => store.addDirectory());
  settingsLink.addEventListener('click', () => {
    setOpen(false);
    settingsLink.dispatchEvent(new CustomEvent('music-settings', { bubbles: true }));
  });
  stopButton.addEventListener('click', () => store.clearSelection());

  // --- Live preview ---------------------------------------------------------

  async function ensurePreviewSource(path) {
    if (previewPath === path) return;
    try {
      previewAudio.src = await invoke('allow_audio_file', { path });
      previewPath = path;
    } catch (error) {
      console.error(error);
      previewPath = '';
    }
  }

  function syncPreviewPlayback(state) {
    // Map the 0..0.5 gain onto the element's 0..1 range so the preview loudness
    // tracks what the export will mix.
    previewAudio.volume = clamp(state.volume * 2, 0, 1);
    if (!state.selectedTrack) {
      previewAudio.pause();
      previewPath = '';
      previewAudio.removeAttribute('src');
      return;
    }
    ensurePreviewSource(state.selectedTrack.path).then(() => {
      if (store.snapshot().selectedPath !== state.selectedTrack.path) return;
      // Only the visible (playing) tab's preview should sound.
      if (!video.paused && !video.ended) {
        previewAudio.play().catch(() => {});
      }
    });
  }

  // Mirror the video's transport so the bed starts/stops/seeks with playback.
  video.addEventListener('play', () => {
    if (store.hasSelection) previewAudio.play().catch(() => {});
  });
  video.addEventListener('pause', () => previewAudio.pause());
  video.addEventListener('ended', () => previewAudio.pause());
  video.addEventListener('seeking', () => {
    if (previewAudio.duration) {
      previewAudio.currentTime = video.currentTime % previewAudio.duration;
    }
  });

  // --- Render ---------------------------------------------------------------

  function renderTracks(state) {
    trackList.replaceChildren();
    emptyState.hidden = state.tracks.length > 0 || state.loading;
    trackList.hidden = state.tracks.length === 0;

    state.tracks.forEach((track) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'music-track';
      item.dataset.path = track.path;
      const selected = track.path === state.selectedPath;
      item.classList.toggle('selected', selected);
      item.setAttribute('aria-pressed', String(selected));
      item.innerHTML = `
        <span class="music-track-icon" aria-hidden="true">${selected ? '♪' : '○'}</span>
        <span class="music-track-name"></span>
      `;
      item.querySelector('.music-track-name').textContent = track.name;
      item.addEventListener('click', () => store.selectTrack(track.path));
      trackList.append(item);
    });
  }

  store.subscribe((state) => {
    if (document.activeElement !== volumeSlider) {
      volumeSlider.value = String(Math.round(state.volume * 100));
    }
    volumeOutput.textContent = pctLabel(state.volume);
    button.classList.toggle('has-music', Boolean(state.selectedTrack));
    label.textContent = state.selectedTrack ? state.selectedTrack.name : 'Music';
    stopButton.hidden = !state.selectedTrack;
    renderTracks(state);
    syncPreviewPlayback(state);
  });

  return {
    close: () => setOpen(false),
    stopPreview: () => previewAudio.pause(),
  };
}

/**
 * Pop a small two-option menu ("With music" / "Without music") anchored at the
 * pointer that started an export. Resolves to 'with', 'without', or null when
 * dismissed. Used so a single Save always asks how to treat a selected bed.
 */
function promptMusicChoice(event, track) {
  return new Promise((resolve) => {
    const menu = document.createElement('div');
    menu.className = 'music-choice';
    menu.setAttribute('role', 'menu');
    menu.innerHTML = `
      <p class="music-choice-head"></p>
      <button type="button" class="music-choice-option" data-choice="with">
        <span class="music-choice-icon" aria-hidden="true">♪</span>
        <span>Save with music</span>
      </button>
      <button type="button" class="music-choice-option" data-choice="without">
        <span class="music-choice-icon" aria-hidden="true">○</span>
        <span>Save without music</span>
      </button>
    `;
    menu.querySelector('.music-choice-head').textContent =
      track ? `“${track.name}”` : 'Background music';
    document.body.append(menu);

    // Position near the click, then nudge back on-screen if it would overflow.
    const margin = 8;
    const x = event?.clientX ?? window.innerWidth / 2;
    const y = event?.clientY ?? window.innerHeight / 2;
    const rect = menu.getBoundingClientRect();
    const left = Math.min(x, window.innerWidth - rect.width - margin);
    const top = Math.min(y, window.innerHeight - rect.height - margin);
    menu.style.left = `${Math.max(margin, left)}px`;
    menu.style.top = `${Math.max(margin, top)}px`;

    let settled = false;
    const cleanup = (result) => {
      if (settled) return;
      settled = true;
      document.removeEventListener('pointerdown', onOutside, true);
      document.removeEventListener('keydown', onKey, true);
      menu.remove();
      resolve(result);
    };

    const onOutside = (e) => {
      if (!menu.contains(e.target)) cleanup(null);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') cleanup(null);
    };

    menu.querySelectorAll('.music-choice-option').forEach((btn) => {
      btn.addEventListener('click', () => cleanup(btn.dataset.choice));
    });

    // Defer listener registration so the originating click doesn't dismiss it.
    setTimeout(() => {
      document.addEventListener('pointerdown', onOutside, true);
      document.addEventListener('keydown', onKey, true);
    }, 0);
  });
}

/**
 * Wire the Settings page where the user manages their background-music folders.
 * Lists each directory with a track count and a remove button, plus an add
 * button. Reflects live changes from the shared store.
 */
export function initMusicSettings(store, { addButton, list }) {
  addButton.addEventListener('click', () => store.addDirectory());

  store.subscribe((state) => {
    list.replaceChildren();
    if (state.dirs.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'music-settings-empty';
      empty.textContent = 'No music folders yet. Add one to load background tracks.';
      list.append(empty);
      return;
    }

    state.dirs.forEach((dir) => {
      const count = state.tracks.filter((t) => t.dir === dir).length;
      const row = document.createElement('div');
      row.className = 'music-dir-row';
      row.innerHTML = `
        <div class="music-dir-info">
          <strong class="music-dir-name"></strong>
          <small class="music-dir-path"></small>
        </div>
        <span class="music-dir-count"></span>
        <button type="button" class="music-dir-remove reset-button">Remove</button>
      `;
      row.querySelector('.music-dir-name').textContent = baseName(dir);
      row.querySelector('.music-dir-path').textContent = dir;
      row.querySelector('.music-dir-count').textContent =
        `${count} track${count === 1 ? '' : 's'}`;
      row.querySelector('.music-dir-remove').addEventListener('click', () => {
        store.removeDirectory(dir);
      });
      list.append(row);
    });
  });
}
