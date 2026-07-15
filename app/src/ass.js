import { ASS_MARGIN_X } from './presets.js';

export function parseTimestamp(value) {
  const parts = value.trim().replace(',', '.').split(':').map(Number);
  return parts[0] * 3600 + parts[1] * 60 + parts[2];
}

export function parseSrt(text) {
  return text
    .replace(/^\uFEFF/, '')
    .trim()
    .split(/\r?\n\r?\n+/)
    .map((block) => {
      const lines = block.split(/\r?\n/);
      const timingIndex = lines.findIndex((line) => line.includes('-->'));
      if (timingIndex < 0) return null;
      const [start, end] = lines[timingIndex].split('-->');
      return {
        start: parseTimestamp(start),
        end: parseTimestamp(end),
        // Keep inline bold/italic/underline tags so they survive into both the
        // preview and the burned ASS. Any other tag (e.g. <font>) is dropped.
        text: lines
          .slice(timingIndex + 1)
          .join('\n')
          .replace(/<(?!\/?[biu]\s*>)[^>]*>/gi, ''),
      };
    })
    .filter((cue) => cue && Number.isFinite(cue.start) && Number.isFinite(cue.end));
}

export function assColor(hex, alpha = '00') {
  const value = hex.replace('#', '');
  const red = value.slice(0, 2);
  const green = value.slice(2, 4);
  const blue = value.slice(4, 6);
  return `&H${alpha}${blue}${green}${red}`;
}

export function assAlpha(opacity = 1) {
  const normalized = Number.isFinite(opacity) ? Math.max(0, Math.min(1, opacity)) : 1;
  return Math.round((1 - normalized) * 255)
    .toString(16)
    .padStart(2, '0')
    .toUpperCase();
}

export function assTime(seconds) {
  const centiseconds = Math.max(0, Math.round(seconds * 100));
  const hours = Math.floor(centiseconds / 360000);
  const minutes = Math.floor((centiseconds % 360000) / 6000);
  const secs = Math.floor((centiseconds % 6000) / 100);
  const fraction = centiseconds % 100;
  return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}.${String(fraction).padStart(2, '0')}`;
}

export function assText(text) {
  return text
    .replace(/\\/g, '\\\\')
    .replace(/{/g, '\\{')
    .replace(/}/g, '\\}')
    .replace(/\r?\n/g, '\\N')
    // Inline SRT styling tags -> ASS override codes. These are added after the
    // brace/backslash escaping above so the override braces stay literal.
    .replace(/<b\s*>/gi, '{\\b1}')
    .replace(/<\/b\s*>/gi, '{\\b0}')
    .replace(/<i\s*>/gi, '{\\i1}')
    .replace(/<\/i\s*>/gi, '{\\i0}')
    .replace(/<u\s*>/gi, '{\\u1}')
    .replace(/<\/u\s*>/gi, '{\\u0}');
}

export function createAss({ width, height, cues, style }) {
  const { color, outlineColor, fontName, fontSize, variation, yPosition, bold, italic, letterSpacing } = style;
  const y = Math.round(height * yPosition / 100);
  // Color routing depends on the border style:
  //   BorderStyle 3 (opaque box): libass fills the box with OutlineColour and
  //     uses BackColour only for the shadow. So the box background color must
  //     go in the OutlineColour field, not BackColour — otherwise the picked
  //     box color is written to a field libass ignores for the fill and the box
  //     renders in the (hidden) outline color instead.
  //   BorderStyle 1 (outline + shadow): OutlineColour is the text outline and
  //     BackColour is the shadow.
  const shadowColor = assColor(
    variation.shadowColor || '#000000',
    assAlpha(variation.shadowOpacity ?? 1),
  );
  const boxColor = assColor(
    variation.backgroundColor || '#000000',
    assAlpha(variation.backgroundOpacity ?? 0.5),
  );
  const outlineColour = variation.box ? boxColor : assColor(outlineColor || '#000000');
  const backColour = shadowColor;
  const assStyle = [
    fontName,
    fontSize,
    assColor(color),
    '&H00000000',
    outlineColour,
    backColour,
    bold ? -1 : 0,
    italic ? -1 : 0,
    0,
    0,
    100,
    100,
    letterSpacing || 0,
    0,
    variation.borderStyle,
    variation.outline,
    variation.shadow,
    5,
    ASS_MARGIN_X,
    ASS_MARGIN_X,
    0,
    1,
  ].join(',');

  const dialogues = cues.map((cue) =>
    `Dialogue: 0,${assTime(cue.start)},${assTime(cue.end)},Default,,0,0,0,,{\\pos(${Math.round(width / 2)},${y})}${assText(cue.text)}`,
  );

  return `[Script Info]
ScriptType: v4.00+
PlayResX: ${width}
PlayResY: ${height}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,${assStyle}

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
${dialogues.join('\n')}
`;
}
