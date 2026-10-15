"""Metadata-only listing for explicitly named virtual WASAPI endpoints.

This command constructs a PortAudio enumerator and reads its device metadata.
It never opens an audio stream, consults a default device, or stores PortAudio
indexes. Names returned here are useful for configuration but are not stable IDs.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from typing import Any


VIRTUAL_MARKERS = (
    "virtual audio cable",
    "vb-audio",
    "voicemeeter",
    "steelseries sonar",
    " sonar",
    "mixline",
    "steam streaming",
    "oculus virtual audio",
)


def enumerate_metadata(audio: Any) -> list[dict[str, Any]]:
    """Return device names and capabilities without exposing ephemeral indexes."""
    rows = []
    for index in range(audio.get_device_count()):
        try:
            device = audio.get_device_info_by_index(index)
            host = audio.get_host_api_info_by_index(int(device.get("hostApi", -1)))
            name = device.get("name")
            host_name = host.get("name")
            if not isinstance(name, str) or not isinstance(host_name, str):
                continue
            rows.append(
                {
                    "name": name,
                    "host_api": host_name,
                    "input_channels": int(device.get("maxInputChannels", 0)),
                    "output_channels": int(device.get("maxOutputChannels", 0)),
                    "loopback": device.get("isLoopbackDevice") is True,
                    "sample_rate": int(round(float(device.get("defaultSampleRate", 0)))),
                    "virtual_named": any(marker in name.casefold() for marker in VIRTUAL_MARKERS),
                }
            )
        except (TypeError, ValueError, OverflowError):
            continue
    return rows


def _load_audio() -> Any:
    for module_name in ("pyaudiowpatch", "pyaudio"):
        try:
            return importlib.import_module(module_name)
        except ImportError:
            continue
    raise RuntimeError("audio_dependency_unavailable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List WASAPI audio endpoint metadata; never opens streams.")
    parser.add_argument("--json", action="store_true", help="print compact JSON instead of a table")
    args = parser.parse_args(argv)
    try:
        module = _load_audio()
        audio = module.PyAudio()
    except Exception:
        print("audio_dependency_or_host_unavailable", file=sys.stderr)
        return 2
    try:
        rows = enumerate_metadata(audio)
    except Exception:
        print("audio_device_enumeration_failed", file=sys.stderr)
        return 2
    finally:
        try:
            audio.terminate()
        except Exception:
            pass
    wasapi = [row for row in rows if "wasapi" in row["host_api"].casefold()]
    if args.json:
        print(json.dumps(wasapi, ensure_ascii=False, separators=(",", ":")))
    else:
        for row in wasapi:
            directions = "/".join(
                direction
                for direction, present in (
                    ("input", row["input_channels"] > 0),
                    ("output", row["output_channels"] > 0),
                    ("loopback", row["loopback"]),
                )
                if present
            )
            print(f"{row['name']} | {row['host_api']} | {directions} | {row['sample_rate']} Hz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
