"""Create an actual-alpha host overlay from the original generated portrait."""

from __future__ import annotations

from pathlib import Path

from rembg import new_session, remove


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "xingyu-host-v1.png"
DESTINATION = ROOT / "assets" / "xingyu-host-alpha-rembg-v1.png"


def main() -> None:
    # silueta is substantially smaller than the default model and gives a
    # suitable foreground mask for this clean single-character illustration.
    session = new_session("silueta")
    DESTINATION.write_bytes(
        remove(
            SOURCE.read_bytes(),
            session=session,
            alpha_matting=True,
            alpha_matting_foreground_threshold=240,
            alpha_matting_background_threshold=10,
            alpha_matting_erode_size=8,
        )
    )
    print(DESTINATION)


if __name__ == "__main__":
    main()
