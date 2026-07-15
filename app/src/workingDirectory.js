import { invoke } from '@tauri-apps/api/core';
import { open } from '@tauri-apps/plugin-dialog';

const STORAGE_KEY = 'ripe-clips-working-directory';

function savedPath() {
  try {
    return localStorage.getItem(STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

function persistPath(path) {
  try {
    if (path) localStorage.setItem(STORAGE_KEY, path);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // The selected path still applies for the current session.
  }
}

export async function createWorkingDirectoryStore() {
  const defaultPath = await invoke('default_working_directory');
  let path = defaultPath;
  const stored = savedPath();
  if (stored) {
    try {
      path = await invoke('ensure_working_directory', { path: stored });
    } catch (error) {
      console.error('Saved working directory is unavailable:', error);
      persistPath('');
    }
  }

  const listeners = new Set();
  const notify = () => {
    for (const listener of listeners) listener({ path, defaultPath });
  };

  async function setPath(nextPath) {
    path = await invoke('ensure_working_directory', { path: nextPath });
    persistPath(path === defaultPath ? '' : path);
    notify();
    return path;
  }

  async function choose() {
    const selected = await open({
      title: 'Choose RipeClips working directory',
      directory: true,
      multiple: false,
      defaultPath: path,
    });
    if (typeof selected === 'string') await setPath(selected);
  }

  async function reset() {
    await setPath(defaultPath);
  }

  async function allocateFile(prefix, extension) {
    return invoke('allocate_work_file', {
      workingDirectory: path,
      prefix,
      extension,
    });
  }

  return {
    choose,
    reset,
    setPath,
    allocateFile,
    subscribe(listener) {
      listeners.add(listener);
      listener({ path, defaultPath });
      return () => listeners.delete(listener);
    },
    get path() {
      return path;
    },
    get defaultPath() {
      return defaultPath;
    },
  };
}

export function initWorkingDirectorySettings(
  store,
  { chooseButton, resetButton, pathElement },
) {
  chooseButton.addEventListener('click', () => {
    store.choose().catch((error) => {
      console.error(error);
      pathElement.textContent = `Could not change directory: ${error}`;
    });
  });
  resetButton.addEventListener('click', () => {
    store.reset().catch((error) => {
      console.error(error);
      pathElement.textContent = `Could not reset directory: ${error}`;
    });
  });
  store.subscribe(({ path, defaultPath }) => {
    pathElement.textContent = path;
    pathElement.title = path;
    resetButton.hidden = path === defaultPath;
  });
}
