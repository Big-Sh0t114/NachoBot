"""Turn a white/gray checkerboard baked into pixels into an alpha PNG."""

from __future__ import annotations

from pathlib import Path
import sys

from PIL import Image


def is_checkerboard_pixel(red: int, green: int, blue: int, alpha: int) -> bool:
    # The generated checkerboard is light and nearly neutral.  The threshold
    # deliberately includes the anti-aliased checkerboard fringe, but preserves
    # skin, clothing and blue hair because those carry more chroma.
    return alpha > 0 and max(red, green, blue) - min(red, green, blue) <= 38 and (red + green + blue) / 3 >= 205


def remove_checkerboard(source: Path, destination: Path) -> None:
    image = Image.open(source).convert("RGBA")
    width, height = image.size
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            if is_checkerboard_pixel(red, green, blue, alpha):
                pixels[x, y] = (red, green, blue, 0)

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, "PNG")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: remove_checkerboard_background.py SOURCE.png DESTINATION.png")
    remove_checkerboard(Path(sys.argv[1]), Path(sys.argv[2]))
