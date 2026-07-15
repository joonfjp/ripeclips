#!/usr/bin/env python3
"""Flatten the bundled variable fonts to static instances.

libass (the FFmpeg `subtitles` filter) has poor variable-font support: it does
not honor a variable font's default named instance, so a burned caption using
one either renders the wrong master or silently falls back to a default face.
Montserrat and Roboto Slab ship as variable fonts, which is why selecting them
in Burn SRT appeared to do nothing.

This instances each at a fixed weight (Regular / 400), drops the variation
tables, and rewrites the name records to a clean static family. Filenames and
family names are unchanged, so nothing else in the app needs to change.

Idempotent: re-running on already-flattened files is a no-op.
"""

from pathlib import Path

from fontTools import ttLib
from fontTools.varLib.instancer import instantiateVariableFont

FONTS_DIR = Path(__file__).resolve().parent.parent / "public" / "fonts"

# Instance each variable font at this weight (400 = Regular).
TARGETS = {
    "Montserrat.ttf": {"wght": 400},
    "RobotoSlab.ttf": {"wght": 400},
}

# Clean static names for fonts whose variable default was not Regular, so the
# instanced file no longer advertises the old default (e.g. Montserrat "Thin").
# nameID -> value. 1/4/6 are legacy family/full/PostScript, 2 is subfamily,
# 16/17 are typographic family/subfamily.
NAME_FIXES = {
    "Montserrat.ttf": {
        1: "Montserrat",
        2: "Regular",
        4: "Montserrat",
        6: "Montserrat-Regular",
        16: "Montserrat",
        17: "Regular",
    },
}


def flatten(name: str, axes: dict) -> None:
    path = FONTS_DIR / name
    font = ttLib.TTFont(path)

    if "fvar" in font:
        instantiateVariableFont(font, axes, inplace=True)

    for name_id, value in NAME_FIXES.get(name, {}).items():
        for record in font["name"].names:
            if record.nameID == name_id:
                record.string = value.encode(
                    "utf-16-be" if record.platformID == 3 else "latin-1"
                )

    font.save(path)
    print(f"flattened {name} at {axes}")


def main() -> None:
    for name, axes in TARGETS.items():
        flatten(name, axes)


if __name__ == "__main__":
    main()
