#!/usr/bin/env python3
"""Generate Launch Home icons from the source app mark.

Source art: assets/icon-source.png — full-bleed pictogram (no baked-in title).
webOS already shows the app title under the tile, so the icon itself stays
visual-only and readable at 60–80px.

Outputs (versioned filenames so Home drops its path-keyed icon cache):
  icon.png / icon-80-v8.png (80)
  icon-mini.png / icon-60-v8.png (60)
  icon-large.png / icon-130-v8.png (130)

Bump ICON_TAG (and appinfo.json paths) whenever the art changes.
"""

from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SOURCE = ASSETS / "icon-source.png"

# Bump when art changes — must match appinfo.json icon paths.
ICON_TAG = "v8"

# Slight center crop so subject stays inside OS rounded-square masks.
SAFE_CROP = 0.04


def make_icon(size: int) -> Image.Image:
    src = Image.open(SOURCE).convert("RGBA")
    w, h = src.size
    side = min(w, h)
    # Center-crop square, then inset for mask safety.
    left = (w - side) // 2
    top = (h - side) // 2
    square = src.crop((left, top, left + side, top + side))

    inset = int(round(side * SAFE_CROP))
    if inset > 0 and side - 2 * inset >= 8:
        square = square.crop((inset, inset, side - inset, side - inset))

    icon = square.resize((size, size), Image.Resampling.LANCZOS)
    # Tiny unsharp for small tiles so the couch/TV edge stays crisp.
    if size <= 80:
        icon = icon.filter(ImageFilter.UnsharpMask(radius=0.6, percent=120, threshold=2))
    return icon.convert("RGB")


def render(name: str, size: int) -> None:
    output = ASSETS / name
    make_icon(size).save(output, "PNG", optimize=True)
    print(f"Wrote {output} ({size}x{size})")


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"Missing source icon: {SOURCE}")

    tag = ICON_TAG
    render("icon.png", 80)
    render(f"icon-80-{tag}.png", 80)
    render("icon-mini.png", 60)
    render(f"icon-60-{tag}.png", 60)
    render("icon-large.png", 130)
    render(f"icon-130-{tag}.png", 130)

    # Unversioned aliases for tooling / install size checks
    render("icon-80.png", 80)
    render("icon-60.png", 60)
    render("icon-130.png", 130)


if __name__ == "__main__":
    main()
