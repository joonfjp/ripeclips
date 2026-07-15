import { invoke } from '@tauri-apps/api/core';
import { fetch } from '@tauri-apps/plugin-http';

// Pure (DOM-free) client for the local Highlights API. Shared by the "Clip it"
// tab and any other caller. Submits an SRT as multipart/form-data and polls the
// async job endpoint until the highlights payload is ready.

// Each preset expands into the three multipart fields the backend understands.
export const DISCOVERY_MODE_PRESETS = {
  best_only: {
    topic_policy: 'soft',
    admission_strictness: '0.85',
    min_candidates_per_window: '0',
  },
  topic_focused: {
    topic_policy: 'hard',
    admission_strictness: '0.75',
    min_candidates_per_window: '0',
  },
  topic_only: {
    topic_policy: 'only',
    admission_strictness: '0.85',
    min_candidates_per_window: '0',
  },
  coverage: {
    topic_policy: 'soft',
    admission_strictness: '0.35',
    min_candidates_per_window: '1',
  },
  research_sweep: {
    topic_policy: 'soft',
    admission_strictness: '0.25',
    min_candidates_per_window: '1',
  },
};

export const DEFAULT_BASE_URL = 'http://127.0.0.1:8000';
const POLL_INTERVAL_MS = 5000;

export function slugify(value) {
  const slug = String(value).replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '');
  return slug || 'highlights';
}

// Split comma-separated text into trimmed, non-empty topics.
export function parseTopics(text) {
  return String(text || '')
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean);
}

// Build a multipart/form-data body: text fields first, then the file part.
// Returns a Blob plus the matching Content-Type header (with boundary).
export function buildMultipart(fields, fileField, fileName, fileBytes) {
  const boundary = `----highlights-${crypto.randomUUID().replace(/-/g, '')}`;
  const encoder = new TextEncoder();
  const parts = [];

  for (const [name, value] of Object.entries(fields)) {
    parts.push(
      encoder.encode(
        `--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`,
      ),
    );
  }
  parts.push(
    encoder.encode(
      `--${boundary}\r\n` +
        `Content-Disposition: form-data; name="${fileField}"; filename="${fileName}"\r\n` +
        `Content-Type: application/x-subrip\r\n\r\n`,
    ),
  );
  parts.push(fileBytes);
  parts.push(encoder.encode(`\r\n--${boundary}--\r\n`));

  return {
    body: new Blob(parts),
    contentType: `multipart/form-data; boundary=${boundary}`,
  };
}

function urls(baseUrl) {
  const base = (baseUrl || DEFAULT_BASE_URL).trim().replace(/\/+$/, '') || DEFAULT_BASE_URL;
  return {
    sync: `${base}/v2/highlights/identify-srt`,
    job: `${base}/v2/highlights/srt-jobs`,
    jobsBase: `${base}/v2/highlights/jobs`,
  };
}

async function postMultipart(url, fields, srtPath, srtFileName) {
  // SRT is UTF-8 text; read it through the existing narrow command and encode.
  const text = await invoke('read_srt', { path: srtPath });
  const fileBytes = new TextEncoder().encode(text);
  const { body, contentType } = buildMultipart(fields, 'file', srtFileName, fileBytes);
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': contentType },
    body: await body.arrayBuffer(),
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`HTTP ${response.status}: ${detail || response.statusText}`);
  }
  return response.json();
}

async function getJson(url) {
  const response = await fetch(url, { method: 'GET' });
  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`HTTP ${response.status}: ${detail || response.statusText}`);
  }
  return response.json();
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const noop = () => {};

/**
 * Run an identification request and resolve with the highlights payload.
 *
 * Async job (default) submits, then polls until completion. The synchronous
 * path posts once and returns. Lifecycle callbacks let a UI reflect progress
 * without this module touching the DOM:
 *   onStage(stage)            'submit' | 'queue' | 'process' | 'done'
 *   onPhase(text)             human-readable phase label
 *   onStatus(text, fraction)  status line + 0..1 progress
 *   onWindows(done, total)    discovery-window counter
 */
export async function runIdentification({
  srtPath,
  srtFileName,
  fields,
  baseUrl,
  sync = false,
  onStage = noop,
  onPhase = noop,
  onStatus = noop,
  onWindows = noop,
}) {
  const { sync: syncUrl, job, jobsBase } = urls(baseUrl);

  if (sync) {
    onStage('process');
    onPhase('Synchronous request');
    onStatus('Submitting synchronous request…', 0.2);
    return postMultipart(syncUrl, fields, srtPath, srtFileName);
  }

  onStage('submit');
  onPhase('Submitting job');
  onStatus('Uploading SRT and creating job…', 0.05);
  let jobStatus = await postMultipart(job, fields, srtPath, srtFileName);
  const jobId = jobStatus.job_id;

  onStage('queue');
  onPhase('Queued');
  onStatus(`Job ${jobId} queued`, 0.08);
  onWindows(0, jobStatus.progress?.windows_total ?? 0);

  for (;;) {
    await sleep(POLL_INTERVAL_MS);
    jobStatus = await getJson(`${jobsBase}/${jobId}`);
    const progress = jobStatus.progress || {};
    const done = progress.windows_done ?? 0;
    const totalWindows = progress.windows_total ?? 0;
    const fraction = totalWindows ? done / totalWindows : 0.1;

    onWindows(done, totalWindows);
    if (jobStatus.status === 'processing' || done > 0) {
      onStage('process');
      onPhase(jobStatus.stage ? `Processing · ${jobStatus.stage}` : 'Processing');
    } else {
      onPhase(jobStatus.status === 'queued' ? 'Queued' : jobStatus.status || 'Working');
    }
    onStatus(
      totalWindows
        ? `${jobStatus.status} · ${done}/${totalWindows} windows`
        : `${jobStatus.status}${jobStatus.stage ? ` · ${jobStatus.stage}` : ''}`,
      0.08 + fraction * 0.88,
    );

    if (jobStatus.status === 'completed') {
      onStage('process');
      onPhase('Fetching result');
      return getJson(`${jobsBase}/${jobId}/result`);
    }
    if (jobStatus.status === 'failed') {
      throw new Error(jobStatus.error || 'Job failed.');
    }
  }
}
