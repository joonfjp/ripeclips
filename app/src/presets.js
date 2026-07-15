export const COLORS = [
  ['White', '#ffffff'],
  ['Black', '#000000'],
  ['Red', '#ff0000'],
  ['Deep Sky Blue', '#00bfff'],
  ['Gold', '#ffd700'],
  ['Lime', '#00ff00'],
  ['Magenta', '#ff00ff'],
  ['Orange', '#ffa500'],
  ['Dodger Blue', '#1e90ff'],
  ['Mint', '#00ff88'],
  ['Amber', '#ffcc00'],
  ['Blue Violet', '#8a2be2'],
];

// Subtitle fonts are derived from the files in public/fonts/ at build time
// (see scripts/fonts-plugin.js). That directory is the single source of truth:
// drop a TTF/OTF in and it appears in every font picker automatically. Each
// entry is { label, family, cssFont, file, bold }.
import { FONTS, FONT_FACE_CSS } from 'virtual:fonts';

// Register the generated @font-face rules once so the CSS previews (and the
// live subtitle preview) can render every bundled family by name.
if (typeof document !== 'undefined' && !document.getElementById('generated-font-faces')) {
  const styleEl = document.createElement('style');
  styleEl.id = 'generated-font-faces';
  styleEl.textContent = FONT_FACE_CSS;
  document.head.append(styleEl);
}

export { FONTS };

// Family name of the default subtitle font. Resolved to an index at runtime so
// it stays correct regardless of the (alphabetical) order fonts are scanned in.
export const DEFAULT_FONT_FAMILY = 'Lato';

// Index of a font family in FONTS, falling back to 0 if it isn't present.
export function fontIndexByFamily(family) {
  const index = FONTS.findIndex((font) => font.family === family);
  return index === -1 ? 0 : index;
}

// Bucket a variation by its shape so the (long) list can be grouped in the UI.
function variationGroup([, spec]) {
  if (spec.box) return 'Box & background';
  if (spec.shadow > 0) return 'Shadow';
  return 'Outline';
}

// Populate a <select> with the VARIATIONS split into <optgroup>s, keeping each
// option's value as its original VARIATIONS index so lookups stay valid.
export function populateVariationSelect(select) {
  const groups = new Map();
  VARIATIONS.forEach((variation, index) => {
    const group = variationGroup(variation);
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push({ name: variation[0], index });
  });
  for (const [groupName, items] of groups) {
    const optgroup = document.createElement('optgroup');
    optgroup.label = groupName;
    for (const { name, index } of items) {
      optgroup.append(new Option(name, index));
    }
    select.append(optgroup);
  }
}

export const VARIATIONS = [
  ['No outline', { borderStyle: 1, outline: 0, shadow: 0 }],
  ['Thin outline', { borderStyle: 1, outline: 1, shadow: 0 }],
  ['Standard outline', { borderStyle: 1, outline: 2, shadow: 0 }],
  ['Thick outline', { borderStyle: 1, outline: 4, shadow: 0 }],
  ['Shadow only', { borderStyle: 1, outline: 0, shadow: 2 }],
  ['Thin outline + shadow', { borderStyle: 1, outline: 1, shadow: 2 }],
  ['Standard outline + shadow', { borderStyle: 1, outline: 2, shadow: 2 }],
  ['Medium shadow', { borderStyle: 1, outline: 2, shadow: 4 }],
  ['Heavy shadow', { borderStyle: 1, outline: 2, shadow: 7 }],
  [
    'Light translucent box',
    {
      borderStyle: 3,
      outline: 4,
      shadow: 0,
      box: true,
      backgroundColor: '#000000',
      backgroundOpacity: 0.35,
    },
  ],
  [
    'Translucent box',
    {
      borderStyle: 3,
      outline: 6,
      shadow: 0,
      box: true,
      backgroundColor: '#000000',
      backgroundOpacity: 0.5,
    },
  ],
  [
    'Dark translucent box',
    {
      borderStyle: 3,
      outline: 6,
      shadow: 0,
      box: true,
      backgroundColor: '#000000',
      backgroundOpacity: 0.75,
    },
  ],
  [
    'Solid box',
    {
      borderStyle: 3,
      outline: 6,
      shadow: 0,
      box: true,
      backgroundColor: '#000000',
      backgroundOpacity: 1,
    },
  ],
  [
    'Translucent box · extra padding',
    {
      borderStyle: 3,
      outline: 10,
      shadow: 0,
      box: true,
      backgroundColor: '#000000',
      backgroundOpacity: 0.5,
    },
  ],
  [
    'Box + shadow',
    {
      borderStyle: 3,
      outline: 6,
      shadow: 3,
      box: true,
      backgroundColor: '#000000',
      backgroundOpacity: 0.65,
    },
  ],
];

export const ASS_MARGIN_X = 50;
export const ASS_TO_CSS_FONT_SCALE = 0.74;
