#!/usr/bin/env python3
"""Run an explicit UniversalVC audio-service session from the adapter checkout.

The example builds its request from command-line arguments and environment
variables. It does not load the legacy UniversalVC TOML configuration or any
capture, model, or native audio modules.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any


# Allow ``python examples/service_client.py`` from this checkout or from the
# examples directory without importing the legacy adapter/config modules.
_ADAPTER_ROOT = Path(__file__).resolve().parents[1]
if str(_ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_ROOT))

from universalvc_service.client import (  # noqa: E402
    UniversalVCClient,
    UniversalVCClientError,
    UniversalVCDisconnected,
    UniversalVCServiceError,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Connect to the local UniversalVC audio service")
    parser.add_argument("--url", help="loopback service URL (default: NACHOBOT_UNIVERSALVC_URL or http://127.0.0.1:8016)")
    parser.add_argument("--platform", default="universal_vc", help="platform name sent with the session")
    parser.add_argument("--scope", default=f"service-client:{uuid.uuid4().hex}", help="opaque session scope, at most 256 characters")
    parser.add_argument("--process", default="", help="application/process name captured by the main source")
    parser.add_argument("--pid", type=int, help="optional exact target process ID")
    parser.add_argument("--system-capture-device", help="system input device; use instead of --process or --pid")
    parser.add_argument("--input-device", help="optional microphone device; adds a fixed-identity mic source")
    parser.add_argument("--push-to-talk", action="store_true", help="require push-to-talk for the optional microphone")
    parser.add_argument("--ptt-key", default="caps_lock", help="push-to-talk key for the optional microphone")
    parser.add_argument("--owner-id", default="owner", help="fixed speaker ID for the optional microphone")
    parser.add_argument("--owner-name", default="Owner", help="fixed speaker name for the optional microphone")
    parser.add_argument("--output-device", help="playback device; omit to request no local playback")
    parser.add_argument("--output-sample-rate", type=int, default=48_000)
    parser.add_argument("--denoise", action="store_true", help="enable denoising for this session")
    parser.add_argument("--speaker", action="store_true", help="enable speaker identification for this session")
    parser.add_argument("--speaker-log", action="store_true", help="enable per-session speaker result logging")
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--max-duration-seconds", type=float, default=60.0)
    return parser


def build_session_options(args: argparse.Namespace) -> dict[str, Any]:
    """Create a complete session request from explicit CLI values."""

    if len(args.scope) > 256:
        raise ValueError("scope must be at most 256 characters")
    if args.process and args.pid is not None:
        raise ValueError("choose either --process or --pid for process capture")
    has_process_selector = bool(args.process) or args.pid is not None
    has_system_device = bool(args.system_capture_device)
    if has_process_selector and has_system_device:
        raise ValueError("choose a process selector or --system-capture-device, not both")
    if not has_process_selector and not has_system_device and not args.input_device:
        raise ValueError("provide --process, --pid, --system-capture-device, or --input-device to select an input")
    if args.push_to_talk and not args.input_device:
        raise ValueError("--push-to-talk requires --input-device")
    if not 0 < args.max_duration_seconds <= 60:
        raise ValueError("max duration must be in (0, 60] seconds")
    if not 8_000 <= args.output_sample_rate <= 192_000:
        raise ValueError("output sample rate must be between 8000 and 192000 Hz")

    sources: list[dict[str, Any]] = []
    if has_process_selector or has_system_device:
        capture: dict[str, Any] = {}
        if args.process:
            capture["target_process_name"] = args.process
        if args.pid is not None:
            capture["target_pid"] = args.pid
        if args.system_capture_device:
            capture["system_capture_device"] = args.system_capture_device
        process_name = args.process or (f"PID {args.pid}" if args.pid is not None else args.system_capture_device)
        identity = (
            {"mode": "speaker"}
            if args.speaker
            else {"mode": "fixed", "id": "application", "name": process_name}
        )
        sources.append(
            {
                "id": "main",
                "format": "f32le",
                "sample_rate": 48_000,
                "channels": 2,
                "input": "process",
                "capture": capture,
                "identity": identity,
            }
        )
    if args.input_device:
        capture = {"device_name": args.input_device, "push_to_talk": bool(args.push_to_talk)}
        if args.push_to_talk:
            capture["ptt_key"] = args.ptt_key
        mic = {
            "id": "mic" if sources else "main",
            "format": "f32le",
            "sample_rate": 48_000,
            "channels": 1,
            "input": "microphone",
            "capture": capture,
            "identity": {"mode": "fixed", "id": args.owner_id, "name": args.owner_name},
        }
        sources.append(mic)

    return {
        "platform": args.platform,
        "scope": args.scope,
        "mode": "pipeline",
        "sources": sources,
        "output": {"mode": "device", "device_name": args.output_device, "sample_rate": args.output_sample_rate}
        if args.output_device
        else {"mode": "none", "sample_rate": args.output_sample_rate},
        "denoise": bool(args.denoise),
        "speaker": bool(args.speaker),
        "speaker_log": bool(args.speaker_log),
        "vad": {"threshold": args.vad_threshold},
        "max_duration_seconds": float(args.max_duration_seconds),
    }


def _print_event(event: dict[str, Any]) -> None:
    """Print useful metadata while keeping captured/returned audio private."""

    summary: dict[str, Any] = {
        "event": event.get("event"),
        "source_id": event.get("source_id"),
        "turn_id": event.get("turn_id"),
    }
    if event.get("event") == "speech_result":
        for key in ("speaker_id", "speaker_name"):
            if isinstance(event.get(key), str):
                summary[key] = event[key][:128]
        transcript = event.get("precomputed_asr_text") or event.get("transcript")
        if isinstance(transcript, str):
            summary["transcript"] = transcript[:240]
    elif event.get("event") == "error":
        error = event.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            summary["error_code"] = error["code"][:64]
    print(json.dumps(summary, ensure_ascii=False))


async def _run(args: argparse.Namespace) -> int:
    client = UniversalVCClient(args.url)
    session = None
    try:
        session = await client.create_session(build_session_options(args))
        print(f"Connected to UniversalVC session {session.session_id}.")
        async for event in session.events():
            _print_event(event)
        return 0
    except UniversalVCServiceError as exc:
        print(f"UniversalVC rejected the request ({exc.code}).", file=sys.stderr)
        return 2
    except UniversalVCDisconnected:
        print("UniversalVC session disconnected.", file=sys.stderr)
        return 1
    except UniversalVCClientError as exc:
        print(f"UniversalVC client error ({type(exc).__name__}).", file=sys.stderr)
        return 1
    finally:
        try:
            if session is not None:
                await session.close()
        finally:
            await client.close()


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
