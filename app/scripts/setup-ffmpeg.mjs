// Vendors the native FFmpeg sidecar that Tauri bundles.
//
// The binary is gitignored because it is large (~77 MB). Run this once after
// cloning. It copies a local FFmpeg that already includes libass and an H.264
// encoder, or you can point it at any compatible build with FFMPEG_SRC.
//
//   node scripts/setup-ffmpeg.mjs
//   FFMPEG_SRC=/path/to/ffmpeg node scripts/setup-ffmpeg.mjs
//
// For a public release, replace this with your own audited static build and
// comply with the FFmpeg LGPL/GPL obligations (libx264 makes the build GPL).

import { execFileSync } from 'node:child_process';
import { copyFileSync, chmodSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const TARGET_TRIPLE = 'aarch64-apple-darwin';
const here = dirname(fileURLToPath(import.meta.url));
const dest = resolve(here, `../src-tauri/binaries/ffmpeg-${TARGET_TRIPLE}`);

function locateSource() {
  if (process.env.FFMPEG_SRC) return process.env.FFMPEG_SRC;
  try {
    return execFileSync('which', ['ffmpeg'], { encoding: 'utf8' }).trim();
  } catch {
    return undefined;
  }
}

const source = locateSource();
if (!source || !existsSync(source)) {
  console.error(
    'No FFmpeg binary found. Install one (e.g. `brew install ffmpeg`) or set\n' +
    'FFMPEG_SRC to a static build that includes libass and libx264, then rerun.',
  );
  process.exit(1);
}

// Fail before copying an incompatible binary. Caption burning specifically
// needs the `subtitles` filter, which is supplied by libass.
try {
  const filters = execFileSync(source, ['-hide_banner', '-filters'], { encoding: 'utf8' });
  const encoders = execFileSync(source, ['-hide_banner', '-encoders'], { encoding: 'utf8' });
  const hasSubtitles = /^ .{2} subtitles\s/m.test(filters);
  const hasLibx264 = /^ V..... libx264\s/m.test(encoders);
  if (!hasSubtitles || !hasLibx264) {
    const missing = [!hasSubtitles && 'the subtitles filter (libass)', !hasLibx264 && 'the libx264 encoder']
      .filter(Boolean)
      .join(' and ');
    throw new Error(`missing ${missing}`);
  }
} catch (error) {
  console.error(`FFmpeg at ${source} is incompatible: ${error.message}`);
  console.error('Use a build configured with --enable-libass and --enable-libx264 via FFMPEG_SRC.');
  process.exit(1);
}

mkdirSync(dirname(dest), { recursive: true });
copyFileSync(source, dest);
chmodSync(dest, 0o755);
// A copied Mach-O can be killed by macOS code-signing enforcement even when
// its source runs correctly (notably after Homebrew updates). Give the
// development sidecar a fresh local ad-hoc signature; Tauri applies the
// release signature when packaging the app.
execFileSync('codesign', ['--force', '--sign', '-', dest], { stdio: 'inherit' });
console.log(`Vendored ${source} -> ${dest}`);
