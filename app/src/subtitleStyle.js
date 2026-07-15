// Single source of truth for the "Subtitle style" control block shared by the
// Burn (SRT Studio) tab and the Caption tab. Both tabs render the exact same
// controls; only the element ID prefix differs ('' for Burn, 'caption-' for
// Caption). Call `mountSubtitleStyle(el, prefix)` before initializing a tab so
// its script can query the generated elements by their prefixed IDs.

// Build the markup for one instance. `prefix` is prepended to every element id
// (e.g. '' -> "font-select", 'caption-' -> "caption-font-select").
function subtitleStyleMarkup(prefix) {
  return `
    <div class="subtitle-style-grid">
      <label class="stacked-field style-grid-cell">
        <span>Font</span>
        <select id="${prefix}font-select"></select>
      </label>

      <label class="stacked-field style-grid-cell">
        <span>Border style</span>
        <select id="${prefix}variation-select"></select>
      </label>

      <fieldset class="style-color-field">
        <legend>Text color</legend>
        <div id="${prefix}color-options" class="swatches"></div>
      </fieldset>

      <fieldset id="${prefix}outline-color-field" class="style-color-field">
        <legend>Outline color</legend>
        <div id="${prefix}outline-color-options" class="swatches"></div>
      </fieldset>

      <fieldset id="${prefix}background-color-field" class="style-color-field" hidden>
        <legend>Box background color</legend>
        <div id="${prefix}background-color-options" class="swatches"></div>
      </fieldset>

      <label class="range-field style-grid-cell">
        <span>Font size <output id="${prefix}font-size-output">72</output></span>
        <input id="${prefix}font-size" type="range" min="12" max="300" value="72" />
      </label>

      <label class="range-field style-grid-cell">
        <span>Vertical position <output id="${prefix}y-position-output">20%</output></span>
        <input id="${prefix}y-position" type="range" min="5" max="95" value="20" />
      </label>

      <label class="range-field style-grid-cell">
        <span>Letter spacing <output id="${prefix}letter-spacing-output">0</output></span>
        <input id="${prefix}letter-spacing" type="range" min="0" max="30" value="0" />
      </label>

      <div class="toggle-row style-toggle-row style-grid-cell" aria-label="Font emphasis">
        <label class="toggle-field">
          <input id="${prefix}bold-toggle" type="checkbox" />
          <span>Bold</span>
        </label>
        <label class="toggle-field">
          <input id="${prefix}italic-toggle" type="checkbox" />
          <span>Italic</span>
        </label>
      </div>

      <details class="custom-font-details style-grid-wide">
        <summary>Custom font file</summary>
        <div class="font-file">
          <button id="${prefix}font-button" type="button" class="font-file-button">Choose font file…</button>
          <button id="${prefix}font-clear" type="button" class="font-clear" hidden>Clear</button>
          <small id="${prefix}font-file-name">Uses bundled Google Fonts unless a file is selected</small>
        </div>
      </details>

      <button id="${prefix}reset-button" type="button" class="reset-button style-grid-wide">Reset style to defaults</button>
    </div>
  `;
}

// Render the subtitle-style controls into `mountEl` for the given id `prefix`.
export function mountSubtitleStyle(mountEl, prefix = '') {
  if (!mountEl) return;
  mountEl.innerHTML = subtitleStyleMarkup(prefix);
}

// Escape cue text, then re-enable the inline tags parseSrt preserves so the
// preview shows bold/italic/underline without allowing arbitrary markup.
function cueHtml(text) {
  const escaped = String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\n/g, '<br>');
  return escaped.replace(/&lt;(\/?[biu])\s*&gt;/gi, '<$1>');
}

function colorWithOpacity(hex, opacity = 1) {
  const value = hex.replace('#', '');
  const red = Number.parseInt(value.slice(0, 2), 16);
  const green = Number.parseInt(value.slice(2, 4), 16);
  const blue = Number.parseInt(value.slice(4, 6), 16);
  const alpha = Number.isFinite(opacity) ? Math.max(0, Math.min(1, opacity)) : 1;
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

/**
 * Wire up a full subtitle-style control block (already mounted via
 * `mountSubtitleStyle`) and manage its live preview overlay. This is the shared
 * behavior behind the Burn and Caption tabs; the Clip it captioning section
 * uses it too. Returns an API the host tab drives.
 *
 * Dependencies are injected so the controller stays DOM-framework free:
 *   deps.COLORS, deps.FONTS, deps.VARIATIONS  — preset tables
 *   deps.populateVariationSelect              — fills the border-style <select>
 *   deps.ASS_MARGIN_X, deps.ASS_TO_CSS_FONT_SCALE
 *   deps.openFontFile()                       — returns a font path or ''
 *
 * config:
 *   prefix        — element id prefix used at mount time
 *   video         — <video> whose frame the preview aligns to
 *   stage         — positioned ancestor of the preview overlay
 *   preview       — the .subtitle-preview element
 *   defaults      — { colorIndex, fontIndex, variationIndex, outlineColorIndex,
 *                     fontSize, letterSpacing, yPosition }
 *   onChange()    — optional; called after any style change
 */
export function createStyleController(config, deps) {
  const {
    prefix = '',
    video,
    stage,
    preview,
    defaults,
    onChange = () => {},
  } = config;
  const {
    COLORS,
    FONTS,
    VARIATIONS,
    populateVariationSelect,
    ASS_MARGIN_X,
    ASS_TO_CSS_FONT_SCALE,
    openFontFile,
  } = deps;

  const $ = (id) => document.getElementById(`${prefix}${id}`);
  const baseName = (path) => path.split(/[\\/]/).pop() || path;

  const fontSelect = $('font-select');
  const variationSelect = $('variation-select');
  const boldToggle = $('bold-toggle');
  const italicToggle = $('italic-toggle');
  const fontButton = $('font-button');
  const fontClear = $('font-clear');
  const fontFileName = $('font-file-name');
  const outlineColorField = $('outline-color-field');
  const backgroundColorField = $('background-color-field');
  const fontSize = $('font-size');
  const fontSizeOutput = $('font-size-output');
  const letterSpacing = $('letter-spacing');
  const letterSpacingOutput = $('letter-spacing-output');
  const yPosition = $('y-position');
  const yPositionOutput = $('y-position-output');
  const resetButton = $('reset-button');

  let selectedColor = COLORS[defaults.colorIndex][1];
  let selectedOutlineColor = COLORS[defaults.outlineColorIndex][1];
  let selectedBackgroundColor = COLORS[1][1];
  let fontPath;
  // The cue currently shown in the preview overlay, so a resize/scroll can
  // re-lay it out without the host re-supplying it.
  let currentCue = null;

  function buildSwatches(container, groupName, defaultIndex, onPick) {
    if (!container) return;
    COLORS.forEach(([name, color], index) => {
      const label = document.createElement('label');
      label.className = 'swatch';
      label.title = name;
      label.innerHTML = `
        <input type="radio" name="${prefix}${groupName}" value="${color}" ${index === defaultIndex ? 'checked' : ''}>
        <span style="--swatch:${color}"></span>
        <em>${name}</em>
      `;
      label.querySelector('input').addEventListener('change', (event) => {
        onPick(event.target.value);
        updatePreview();
      });
      container.append(label);
    });
  }

  function updateFontSelectPreview() {
    const font = FONTS[Number(fontSelect.value)];
    syncStyleToggleAvailability(font, { boldToggle, italicToggle }, Boolean(fontPath));
    fontSelect.style.fontFamily = font ? font.cssFont : '';
  }

  function updateVariationControls() {
    const variation = VARIATIONS[Number(variationSelect.value)][1];
    if (outlineColorField) outlineColorField.hidden = Boolean(variation.box) || !variation.outline;
    if (backgroundColorField) backgroundColorField.hidden = !variation.box;
  }

  function styleState() {
    const font = FONTS[Number(fontSelect.value)];
    return {
      color: selectedColor,
      outlineColor: selectedOutlineColor,
      fontName: font.family,
      cssFont: font.cssFont,
      bundledFontFile: font.file,
      fontPath: fontPath || null,
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

  // Metrics of the reference canvas the caption is laid out against: its
  // displayed rectangle (viewport coords) plus the canvas pixel dimensions the
  // ASS is authored for. Defaults to the source video (the Original-format
  // export canvas). A host may inject `config.canvasMetrics()` to reproject the
  // overlay onto a different canvas — e.g. the portrait 9:16 frame (1080×1920)
  // — so the preview matches whatever ffmpeg will actually burn onto.
  function canvasMetrics() {
    const custom = config.canvasMetrics?.();
    if (custom && custom.width && custom.height && custom.rect?.width) return custom;
    if (!video || !video.videoWidth) return null;
    return { rect: video.getBoundingClientRect(), width: video.videoWidth, height: video.videoHeight };
  }

  function updatePreview() {
    if (!preview || !stage) return;
    const metrics = canvasMetrics();
    if (!metrics) return;
    const { rect: canvasRect, width: canvasW } = metrics;
    const { color, outlineColor, cssFont, fontSize: size, variation, yPosition: yPos, bold, italic, letterSpacing: spacing } =
      styleState();
    // Displayed-pixels per canvas-pixel. Font size, outline, shadow, spacing,
    // and the horizontal margin are all authored in canvas pixels, so this maps
    // them into on-screen pixels for whichever canvas we're previewing.
    const scale = canvasRect.width / canvasW;
    const assScale = ASS_TO_CSS_FONT_SCALE * scale;
    const stageRect = stage.getBoundingClientRect();
    const marginX = ASS_MARGIN_X * scale;
    const shadowColor = colorWithOpacity(variation.shadowColor || '#000000', variation.shadowOpacity ?? 1);
    const boxColor = colorWithOpacity(variation.backgroundColor || '#000000', variation.backgroundOpacity ?? 0.5);
    const shadowOffset = variation.shadow * assScale;

    preview.style.color = color;
    preview.style.fontFamily = cssFont;
    preview.style.fontWeight = bold ? '700' : '400';
    preview.style.fontStyle = italic ? 'italic' : 'normal';
    preview.style.letterSpacing = spacing ? `${spacing * assScale}px` : 'normal';
    preview.style.fontSize = `${size * assScale}px`;
    preview.style.webkitTextStroke =
      !variation.box && variation.outline ? `${variation.outline * assScale}px ${outlineColor}` : '0';
    preview.style.textShadow =
      !variation.box && variation.shadow ? `${shadowOffset}px ${shadowOffset}px 0 ${shadowColor}` : 'none';
    preview.style.background = variation.box ? boxColor : 'transparent';
    preview.style.padding = variation.box ? `${variation.outline * assScale}px` : '0';
    preview.style.boxShadow =
      variation.box && variation.shadow ? `${shadowOffset}px ${shadowOffset}px 0 ${boxColor}` : 'none';
    preview.style.setProperty('--subtitle-top', `${canvasRect.top - stageRect.top + canvasRect.height * yPos / 100}px`);
    preview.style.setProperty('--subtitle-left', `${canvasRect.left - stageRect.left + marginX}px`);
    preview.style.setProperty('--subtitle-width', `${Math.max(0, canvasRect.width - marginX * 2)}px`);
    preview.style.setProperty('--subtitle-center-x', `${canvasRect.left - stageRect.left + canvasRect.width / 2}px`);
    preview.classList.toggle('boxed', Boolean(variation.box));
    onChange();
  }

  // Show (or clear) the cue text in the overlay. Pass null to hide it.
  function showCue(cue) {
    currentCue = cue || null;
    if (!preview) return;
    preview.innerHTML = currentCue ? cueHtml(currentCue.text) : '';
    preview.classList.toggle('visible', Boolean(currentCue));
  }

  function reset() {
    selectedColor = COLORS[defaults.colorIndex][1];
    const swatch = document.querySelector(`#${prefix}color-options input[value="${selectedColor}"]`);
    if (swatch) swatch.checked = true;

    selectedOutlineColor = COLORS[defaults.outlineColorIndex][1];
    const outlineSwatch = document.querySelector(`#${prefix}outline-color-options input[value="${selectedOutlineColor}"]`);
    if (outlineSwatch) outlineSwatch.checked = true;

    selectedBackgroundColor = COLORS[1][1];
    const backgroundSwatch = document.querySelector(`#${prefix}background-color-options input[value="${selectedBackgroundColor}"]`);
    if (backgroundSwatch) backgroundSwatch.checked = true;

    fontSelect.value = String(defaults.fontIndex);
    variationSelect.value = String(defaults.variationIndex);
    boldToggle.checked = FONTS[defaults.fontIndex].bold;
    italicToggle.checked = false;

    fontSize.value = String(defaults.fontSize);
    fontSizeOutput.value = String(defaults.fontSize);
    letterSpacing.value = String(defaults.letterSpacing);
    letterSpacingOutput.value = String(defaults.letterSpacing);
    yPosition.value = String(defaults.yPosition);
    yPositionOutput.value = `${defaults.yPosition}%`;

    fontPath = undefined;
    if (fontFileName) fontFileName.textContent = 'Uses bundled Google Fonts unless a file is selected';
    if (fontClear) fontClear.hidden = true;

    updateFontSelectPreview();
    updateVariationControls();
    updatePreview();
  }

  // --- Wiring ---------------------------------------------------------------

  buildSwatches($('color-options'), 'color', defaults.colorIndex, (c) => { selectedColor = c; });
  buildSwatches($('outline-color-options'), 'outline-color', defaults.outlineColorIndex, (c) => { selectedOutlineColor = c; });
  buildSwatches($('background-color-options'), 'background-color', 1, (c) => { selectedBackgroundColor = c; });

  FONTS.forEach(({ label, cssFont }, index) => {
    const option = new Option(label, index);
    option.style.fontFamily = cssFont;
    fontSelect.add(option);
  });
  populateVariationSelect(variationSelect);

  fontSelect.addEventListener('change', () => {
    boldToggle.checked = FONTS[Number(fontSelect.value)].bold;
    updateFontSelectPreview();
    updatePreview();
  });
  variationSelect.addEventListener('change', () => {
    updateVariationControls();
    updatePreview();
  });
  boldToggle.addEventListener('change', updatePreview);
  italicToggle.addEventListener('change', updatePreview);
  fontSize.addEventListener('input', (event) => {
    fontSizeOutput.value = event.target.value;
    updatePreview();
  });
  letterSpacing.addEventListener('input', (event) => {
    letterSpacingOutput.value = event.target.value;
    updatePreview();
  });
  yPosition.addEventListener('input', (event) => {
    yPositionOutput.value = `${event.target.value}%`;
    updatePreview();
  });
  resetButton?.addEventListener('click', reset);

  fontButton?.addEventListener('click', async () => {
    const selected = await openFontFile();
    if (typeof selected === 'string' && selected) {
      fontPath = selected;
      if (fontFileName) fontFileName.textContent = baseName(selected);
      if (fontClear) fontClear.hidden = false;
      updateFontSelectPreview();
      updatePreview();
    }
  });
  fontClear?.addEventListener('click', () => {
    fontPath = undefined;
    if (fontFileName) fontFileName.textContent = 'Uses bundled Google Fonts unless a file is selected';
    if (fontClear) fontClear.hidden = true;
    updateFontSelectPreview();
    updatePreview();
  });

  reset();

  return {
    styleState,
    updatePreview,
    showCue,
    reset,
    // Re-lay the current cue out (e.g. on resize) without changing it.
    reflow: updatePreview,
  };
}

// Enable/disable the Bold and Italic toggles based on whether the selected
// family actually ships those faces. libass and the browser synthesize missing
// faces differently, so we only allow toggling styles that exist as real files.
// When a face is missing the toggle is forced off, disabled, and given a hint.
// A custom font file (fontPath set) can contain any face, so toggles stay live.
export function syncStyleToggleAvailability(font, { boldToggle, italicToggle }, hasCustomFont = false) {
  const apply = (toggle, available, missingLabel) => {
    if (!toggle) return;
    const allowed = hasCustomFont || available;
    if (!allowed && toggle.checked) toggle.checked = false;
    toggle.disabled = !allowed;
    const field = toggle.closest('.toggle-field');
    if (field) {
      field.classList.toggle('disabled', !allowed);
      field.title = allowed ? '' : `This font has no ${missingLabel} face`;
    }
  };
  apply(boldToggle, Boolean(font?.hasBold), 'bold');
  apply(italicToggle, Boolean(font?.hasItalic), 'italic');
}
