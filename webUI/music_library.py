"""Build the WebUI BGM playlist from compact loop assets.

The resources directory supports two kinds of tracks:

* song.mp3: the whole short loop and should repeat forever.
* in_song.mp3 plus lp_song.mp3: play the intro once, then repeat the loop
  segment forever.

Only logical tracks are returned to the browser. The individual in_ and lp_
files are intentionally kept out of the user-facing playlist.
"""

from pathlib import Path
from urllib.parse import quote


AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".ogg", ".flac"})


def build_music_playlist(resources_dir: Path) -> list[dict[str, str]]:
    """Return playable logical BGM tracks discovered in resources_dir.

    Matching segment names is case-insensitive so the convention also works
    across filesystems with different case-sensitivity rules. An orphaned
    lp_ segment is still useful as a normal loop track; an orphaned in_
    segment is omitted because it has no repeatable continuation.
    """
    if not resources_dir.exists():
        return []

    audio_files = sorted(
        (
            path
            for path in resources_dir.iterdir()
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
        ),
        key=lambda path: path.name.casefold(),
    )

    regular_tracks: list[dict[str, str]] = []
    intros: dict[str, tuple[str, Path]] = {}
    loops: dict[str, tuple[str, Path]] = {}

    for path in audio_files:
        stem = path.stem
        normalized_stem = stem.casefold()

        if normalized_stem.startswith("in_") and len(stem) > 3:
            title = stem[3:]
            intros.setdefault(title.casefold(), (title, path))
        elif normalized_stem.startswith("lp_") and len(stem) > 3:
            title = stem[3:]
            loops.setdefault(title.casefold(), (title, path))
        else:
            regular_tracks.append(
                {
                    "name": stem,
                    "kind": "loop",
                    "loopUrl": _resource_url(path),
                }
            )

    segmented_tracks: list[dict[str, str]] = []
    for key in sorted(set(intros) | set(loops)):
        intro = intros.get(key)
        loop = loops.get(key)

        if intro and loop:
            segmented_tracks.append(
                {
                    "name": intro[0],
                    "kind": "intro-loop",
                    "introUrl": _resource_url(intro[1]),
                    "loopUrl": _resource_url(loop[1]),
                }
            )
        elif loop:
            segmented_tracks.append(
                {
                    "name": loop[0],
                    "kind": "loop",
                    "loopUrl": _resource_url(loop[1]),
                }
            )

    return sorted(
        [*regular_tracks, *segmented_tracks],
        key=lambda track: track["name"].casefold(),
    )


def _resource_url(path: Path) -> str:
    """Return a URL-safe static-resource path for an audio asset."""
    return f"/resources/{quote(path.name, safe='')}"
