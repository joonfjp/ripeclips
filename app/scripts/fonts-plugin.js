import fs from 'node:fs';
import path from 'node:path';

// Single source of truth for subtitle fonts: the files in public/fonts/.
// This Vite plugin scans that directory at dev/build time and exposes a
// `virtual:fonts` module holding both the derived FONTS metadata (consumed by
// presets.js) and the generated @font-face CSS (injected at runtime). Drop a
// TTF/OTF into public/fonts and it shows up everywhere automatically.

const VIRTUAL_ID = 'virtual:fonts';
const RESOLVED_ID = '\0virtual:fonts';
const FONT_EXTENSIONS = new Set(['.ttf', '.otf', '.ttc']);

const FORMAT_BY_EXT = {
  '.ttf': 'truetype',
  '.ttc': 'truetype',
  '.otf': 'opentype',
};

// Decode a single `name` record's bytes into a JS string. Windows (platform 3)
// and Unicode (platform 0) records are UTF-16BE; Mac (platform 1) records are
// treated as Latin-1, which covers the ASCII family names these fonts use.
function decodeNameRecord(buffer, platformID, offset, length) {
  if (platformID === 3 || platformID === 0) {
    let out = '';
    for (let i = 0; i + 1 < length; i += 2) {
      out += String.fromCharCode(buffer.readUInt16BE(offset + i));
    }
    return out;
  }
  return buffer.toString('latin1', offset, offset + length);
}

// Read the real family name from a font's `name` table, preferring the
// typographic family (name ID 16) and falling back to the legacy family
// (name ID 1). This is the name libass/fontconfig match against at render time,
// so using it keeps the ASS `Fontname` (and the CSS preview) in lockstep with
// what actually gets burned in. Returns null if the file can't be parsed.
function readFamilyName(filePath) {
  let buffer;
  try {
    buffer = fs.readFileSync(filePath);
  } catch {
    return null;
  }

  // A TrueType Collection points at its first font's table directory at
  // offset 12; a plain sfnt starts its directory at offset 0.
  let sfntOffset = 0;
  const tag = buffer.toString('latin1', 0, 4);
  if (tag === 'ttcf') {
    sfntOffset = buffer.readUInt32BE(12);
  }

  const numTables = buffer.readUInt16BE(sfntOffset + 4);
  let nameTableOffset = -1;
  const recordsStart = sfntOffset + 12;
  for (let i = 0; i < numTables; i += 1) {
    const record = recordsStart + i * 16;
    if (buffer.toString('latin1', record, record + 4) === 'name') {
      nameTableOffset = buffer.readUInt32BE(record + 8);
      break;
    }
  }
  if (nameTableOffset === -1) return null;

  const count = buffer.readUInt16BE(nameTableOffset + 2);
  const stringStorage = nameTableOffset + buffer.readUInt16BE(nameTableOffset + 4);

  // Prefer a Windows/English record, then anything, for each of name ID 16 and
  // name ID 1. `pick` tracks the best candidate seen for a given name ID.
  const best = { 16: null, 1: null };
  for (let i = 0; i < count; i += 1) {
    const rec = nameTableOffset + 6 + i * 12;
    const platformID = buffer.readUInt16BE(rec);
    const languageID = buffer.readUInt16BE(rec + 4);
    const nameID = buffer.readUInt16BE(rec + 6);
    if (nameID !== 16 && nameID !== 1) continue;
    const length = buffer.readUInt16BE(rec + 8);
    const offset = stringStorage + buffer.readUInt16BE(rec + 10);
    const value = decodeNameRecord(buffer, platformID, offset, length).trim();
    if (!value) continue;
    const preferred = platformID === 3 && languageID === 0x409;
    const current = best[nameID];
    if (!current || (preferred && !current.preferred)) {
      best[nameID] = { value, preferred };
    }
  }

  return best[16]?.value || best[1]?.value || null;
}

// "PlayfairDisplay-Bold.ttf" -> family "Playfair Display", style "Bold".
function parseFontFile(filename, fontsDir) {
  const base = filename.replace(/\.(ttf|otf|ttc)$/i, '');
  const dash = base.indexOf('-');
  const familyToken = dash === -1 ? base : base.slice(0, dash);
  const styleToken = dash === -1 ? '' : base.slice(dash + 1);

  // Family name derived from the filename, used only for grouping variants and
  // as a fallback when the font's `name` table can't be read.
  const derivedFamily = familyToken
    // Split camelCase / TitleCase runs: "ChivoMono" -> "Chivo Mono".
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .trim();

  const style = styleToken.toLowerCase();
  const italic = style.includes('italic');
  const extraLight = style.includes('extralight');
  const bold = style.includes('bold');
  const isRegular = style === '' || style === 'regular';
  const weight = extraLight ? 200 : bold ? 700 : 400;

  // The font's real family name (what libass matches on). Fall back to the
  // filename-derived name if the table is unreadable.
  const realFamily = readFamilyName(path.join(fontsDir, filename)) || derivedFamily;

  return { filename, family: realFamily, derivedFamily, italic, bold, isRegular, weight };
}

// Best fallback generic so the brief pre-load flash matches the face's shape.
// Purely cosmetic — the bundled face itself always loads over the top.
function genericFor(family) {
  const f = family.toLowerCase();
  if (/(mono|silkscreen)/.test(f)) return 'monospace';
  if (/(hand|marker|bungee)/.test(f)) return 'cursive';
  if (/(serif|slab|baskerville|playfair|neuton|bree|neuton)/.test(f)) return 'serif';
  return 'sans-serif';
}

function scanFonts(fontsDir) {
  const files = fs
    .readdirSync(fontsDir)
    .filter((name) => FONT_EXTENSIONS.has(path.extname(name).toLowerCase()))
    .sort();

  // Group every variant file under its (filename-derived) family. Grouping on
  // the filename keeps sibling variants together even when their internal
  // family names differ (e.g. an "ExtraLight" face reports a distinct name).
  const families = new Map();
  for (const filename of files) {
    const info = parseFontFile(filename, fontsDir);
    if (!families.has(info.derivedFamily)) families.set(info.derivedFamily, []);
    families.get(info.derivedFamily).push(info);
  }

  const fonts = [...families.entries()]
    .map(([derivedFamily, variants]) => {
      // Primary face: prefer a Regular/plain file, otherwise the first variant
      // (e.g. Lato ships only a Bold face). The primary's weight decides whether
      // the Bold toggle defaults on.
      const primary =
        variants.find((v) => v.isRegular && !v.italic) ||
        variants.find((v) => v.isRegular) ||
        variants.find((v) => !v.italic) ||
        variants[0];
      // The real family name from the primary face is what the ASS `Fontname`
      // and the CSS @font-face use, so the preview matches the burned render.
      const family = primary.family;
      const generic = genericFor(family);
      const quoted = family.includes(' ') ? `"${family}"` : family;
      // Whether the family actually ships a true bold / italic face. Used by the
      // UI to disable those toggles instead of letting the browser (preview) or
      // libass (render) synthesize them differently.
      const hasBold = variants.some((v) => v.weight >= 700 && !v.italic);
      const hasItalic = variants.some((v) => v.italic);
      return {
        label: family,
        family,
        cssFont: `${quoted}, ${generic}`,
        file: primary.filename,
        bold: primary.weight === 700,
        hasBold,
        hasItalic,
        variants,
      };
    })
    .sort((a, b) => a.label.localeCompare(b.label));

  return fonts;
}

function faceCss(fonts) {
  const rules = [];
  for (const font of fonts) {
    for (const variant of font.variants) {
      const format = FORMAT_BY_EXT[path.extname(variant.filename).toLowerCase()] || 'truetype';
      const lines = [
        `  font-family: "${font.family}";`,
        `  src: url("/fonts/${variant.filename}") format("${format}");`,
      ];
      if (variant.weight !== 400) lines.push(`  font-weight: ${variant.weight};`);
      if (variant.italic) lines.push('  font-style: italic;');
      rules.push(`@font-face {\n${lines.join('\n')}\n}`);
    }
  }
  return rules.join('\n\n');
}

export function fontsPlugin() {
  let fontsDir;
  return {
    name: 'ripe-clips-fonts',
    configResolved(config) {
      fontsDir = path.resolve(config.root, 'public/fonts');
    },
    resolveId(id) {
      if (id === VIRTUAL_ID) return RESOLVED_ID;
      return null;
    },
    load(id) {
      if (id !== RESOLVED_ID) return null;
      const fonts = scanFonts(fontsDir);
      // Drop the internal `variants` detail from the runtime FONTS export.
      const runtimeFonts = fonts.map(({ variants, ...rest }) => rest);
      return [
        `export const FONTS = ${JSON.stringify(runtimeFonts, null, 2)};`,
        `export const FONT_FACE_CSS = ${JSON.stringify(faceCss(fonts))};`,
      ].join('\n\n');
    },
    // Rebuild when a font is added/removed during dev.
    configureServer(server) {
      const watchDir = path.resolve(server.config.root, 'public/fonts');
      server.watcher.add(watchDir);
      const invalidate = () => {
        const mod = server.moduleGraph.getModuleById(RESOLVED_ID);
        if (mod) {
          server.moduleGraph.invalidateModule(mod);
          server.ws.send({ type: 'full-reload' });
        }
      };
      server.watcher.on('add', (file) => {
        if (file.startsWith(watchDir)) invalidate();
      });
      server.watcher.on('unlink', (file) => {
        if (file.startsWith(watchDir)) invalidate();
      });
    },
  };
}
