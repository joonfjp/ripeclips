// Generates a minimal 1024x1024 PNG app icon with no external dependencies.
// Dark rounded background with a green caption-bar motif over a preview stage.
import { writeFileSync } from 'node:fs';
import { deflateSync } from 'node:zlib';

const SIZE = 1024;
const channels = 4;

function rgba(r, g, b, a = 255) {
  return [r, g, b, a];
}

const bg = rgba(17, 19, 16);
const panel = rgba(8, 9, 7);
const accent = rgba(0, 255, 0);

const pixels = Buffer.alloc(SIZE * SIZE * channels);

function set(x, y, [r, g, b, a]) {
  const i = (y * SIZE + x) * channels;
  pixels[i] = r;
  pixels[i + 1] = g;
  pixels[i + 2] = b;
  pixels[i + 3] = a;
}

const radius = 180;
function inRoundedRect(x, y, x0, y0, x1, y1, rad) {
  if (x < x0 || x > x1 || y < y0 || y > y1) return false;
  const cx = Math.min(Math.max(x, x0 + rad), x1 - rad);
  const cy = Math.min(Math.max(y, y0 + rad), y1 - rad);
  const dx = x - cx;
  const dy = y - cy;
  return dx * dx + dy * dy <= rad * rad;
}

for (let y = 0; y < SIZE; y++) {
  for (let x = 0; x < SIZE; x++) {
    if (!inRoundedRect(x, y, 0, 0, SIZE - 1, SIZE - 1, radius)) {
      set(x, y, rgba(0, 0, 0, 0));
      continue;
    }
    set(x, y, bg);
  }
}

// Centered preview "stage" panel.
const px0 = 180, py0 = 220, px1 = 844, py1 = 700;
for (let y = py0; y <= py1; y++) {
  for (let x = px0; x <= px1; x++) {
    if (inRoundedRect(x, y, px0, py0, px1, py1, 40)) set(x, y, panel);
  }
}

// Subtitle caption bar near the bottom of the stage.
const cx0 = 280, cy0 = 600, cx1 = 744, cy1 = 656;
for (let y = cy0; y <= cy1; y++) {
  for (let x = cx0; x <= cx1; x++) {
    if (inRoundedRect(x, y, cx0, cy0, cx1, cy1, 16)) set(x, y, accent);
  }
}

// Second shorter caption line.
const dx0 = 360, dy0 = 540, dx1 = 664, dy1 = 584;
for (let y = dy0; y <= dy1; y++) {
  for (let x = dx0; x <= dx1; x++) {
    if (inRoundedRect(x, y, dx0, dy0, dx1, dy1, 16)) set(x, y, accent);
  }
}

function crc32(buf) {
  let c = ~0;
  for (let i = 0; i < buf.length; i++) {
    c ^= buf[i];
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return ~c >>> 0;
}

function chunk(type, data) {
  const typeBuf = Buffer.from(type, 'ascii');
  const lenBuf = Buffer.alloc(4);
  lenBuf.writeUInt32BE(data.length, 0);
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([lenBuf, typeBuf, data, crcBuf]);
}

const ihdr = Buffer.alloc(13);
ihdr.writeUInt32BE(SIZE, 0);
ihdr.writeUInt32BE(SIZE, 4);
ihdr[8] = 8; // bit depth
ihdr[9] = 6; // RGBA
ihdr[10] = 0;
ihdr[11] = 0;
ihdr[12] = 0;

const raw = Buffer.alloc(SIZE * (SIZE * channels + 1));
for (let y = 0; y < SIZE; y++) {
  raw[y * (SIZE * channels + 1)] = 0; // filter type none
  pixels.copy(
    raw,
    y * (SIZE * channels + 1) + 1,
    y * SIZE * channels,
    (y + 1) * SIZE * channels,
  );
}

const png = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  chunk('IHDR', ihdr),
  chunk('IDAT', deflateSync(raw)),
  chunk('IEND', Buffer.alloc(0)),
]);

writeFileSync(new URL('../app-icon.png', import.meta.url), png);
console.log('Wrote app-icon.png');
