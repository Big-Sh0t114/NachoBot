"""Synthetic-only WASAPI routing self-test for explicitly named virtual devices.

The command never asks Windows for a default device. It requires an explicit
VAC playback/record pair and a separate virtual render/loopback pair, plays
deterministic tones, computes metrics in memory, and writes metadata only.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

from devices import VIRTUAL_MARKERS


class SelfTestError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ToneAnalyzer:
    def __init__(self, sample_rate: int, channels: int):
        self.sample_rate = sample_rate
        self.channels = channels
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self.reset(440)

    def reset(self, frequency: int) -> None:
        with self._lock:
            omega = 2.0 * math.pi * frequency / self.sample_rate
            self._coefficient = 2.0 * math.cos(omega)
            self._s1 = self._s2 = 0.0
            self._sum_square = 0.0
            self._count = 0
            self._frequency = frequency
            self._ready.clear()

    def feed(self, pcm: bytes) -> None:
        frame_width = self.channels * 2
        if len(pcm) % frame_width:
            return
        with self._lock:
            if self.channels == 1:
                values = (sample[0] for sample in struct.iter_unpack("<h", pcm))
            else:
                values = (round((left + right) / 2) for left, right in struct.iter_unpack("<hh", pcm))
            for sample in values:
                self._sum_square += sample * sample
                value = sample + self._coefficient * self._s1 - self._s2
                self._s2, self._s1 = self._s1, value
                self._count += 1
            if self._count:
                self._ready.set()

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            count = self._count
            power = max(0.0, self._s1 * self._s1 + self._s2 * self._s2 - self._coefficient * self._s1 * self._s2)
            rms = math.sqrt(self._sum_square / count) if count else 0.0
            tone_rms = math.sqrt(2.0 * power) / count if count else 0.0
            return {
                "sample_count": count,
                "duration_s": round(count / self.sample_rate, 4),
                "rms": round(rms, 2),
                "tone_rms": round(tone_rms, 2),
                "frequency_hz": self._frequency,
            }


def _load_audio() -> Any:
    for name in ("pyaudiowpatch", "pyaudio"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise SelfTestError("audio_dependency_unavailable")


def _devices(audio: Any) -> list[dict[str, Any]]:
    result = []
    for index in range(audio.get_device_count()):
        try:
            info = audio.get_device_info_by_index(index)
            host = audio.get_host_api_info_by_index(int(info.get("hostApi", -1)))
            name, host_api = info.get("name"), host.get("name")
            if not isinstance(name, str) or not isinstance(host_api, str):
                continue
            result.append(
                {
                    "index": int(info.get("index", index)),
                    "name": name,
                    "host_api": host_api,
                    "inputs": max(0, int(info.get("maxInputChannels", 0))),
                    "outputs": max(0, int(info.get("maxOutputChannels", 0))),
                    "loopback": info.get("isLoopbackDevice") is True,
                    "rate": int(round(float(info.get("defaultSampleRate", 0)))),
                }
            )
        except (TypeError, ValueError, OverflowError):
            continue
    return result


def _unique(
    devices: list[dict[str, Any]], name: str, predicate: Any, error_code: str
) -> dict[str, Any]:
    matches = [
        device for device in devices
        if device["name"].casefold() == name.casefold()
        and "wasapi" in device["host_api"].casefold()
        and predicate(device)
    ]
    if len(matches) != 1:
        raise SelfTestError(error_code if not matches else "endpoint_name_ambiguous")
    return matches[0]


def _is_virtual(name: str) -> bool:
    folded = name.casefold()
    return any(marker in folded for marker in VIRTUAL_MARKERS)


def _loopback_base(name: str) -> str:
    suffix = " [loopback]"
    return name[:-len(suffix)] if name.casefold().endswith(suffix) else name


def _make_capture_callback(module: Any, analyzer: ToneAnalyzer, failure: threading.Event) -> Any:
    def callback(data: bytes | None, frame_count: int, time_info: Any, status_flags: int) -> tuple[None, int]:
        del frame_count, time_info
        if status_flags & getattr(module, "paInputOverflow", 2):
            failure.set()
            return None, getattr(module, "paAbort", 2)
        if data:
            analyzer.feed(data)
        return None, getattr(module, "paContinue", 0)
    return callback


def _play_tone(
    module: Any, audio: Any, device: dict[str, Any], frequency: int,
    duration_s: float, deadline: float,
) -> dict[str, Any]:
    rate = device["rate"]
    channels = min(2, device["outputs"])
    if rate < 8_000 or not channels:
        raise SelfTestError("render_endpoint_unavailable")
    remaining = int(rate * duration_s)
    phase = 0
    complete = threading.Event()
    callback_error = threading.Event()
    step = 2.0 * math.pi * frequency / rate

    def callback(out_data: Any, frame_count: int, time_info: Any, status_flags: int) -> tuple[bytes, int]:
        del out_data, time_info, status_flags
        nonlocal remaining, phase
        if remaining <= 0:
            complete.set()
            return bytes(frame_count * channels * 2), getattr(module, "paComplete", 1)
        count = min(frame_count, remaining)
        samples = []
        for _ in range(count):
            sample = int(3_000 * math.sin(phase))
            phase = (phase + step) % (2 * math.pi)
            if channels == 1:
                samples.append(sample)
            else:
                samples.extend((sample, sample))
        remaining -= count
        data = struct.pack(f"<{len(samples)}h", *samples)
        if count < frame_count:
            data += bytes((frame_count - count) * channels * 2)
            complete.set()
            return data, getattr(module, "paComplete", 1)
        return data, getattr(module, "paContinue", 0)

    stream = None
    try:
        stream = audio.open(
            format=module.paInt16,
            channels=channels,
            rate=rate,
            output=True,
            output_device_index=device["index"],
            frames_per_buffer=max(1, round(rate * 0.02)),
            stream_callback=callback,
            start=False,
        )
        stream.start_stream()
        while time.monotonic() < deadline:
            if complete.is_set() and not stream.is_active():
                return {"callback_complete": True, "inactive_after_complete": True}
            if not stream.is_active() and not complete.is_set():
                callback_error.set()
                break
            time.sleep(0.005)
        if callback_error.is_set():
            raise SelfTestError("render_callback_stopped_early")
        raise SelfTestError("selftest_deadline")
    finally:
        if stream is not None:
            try:
                if stream.is_active():
                    stream.stop_stream()
            except Exception:
                pass
            stream.close()


def _cancel_check(module: Any, audio: Any, device: dict[str, Any], deadline: float) -> dict[str, Any]:
    channels, rate = min(2, device["outputs"]), device["rate"]
    cancellation = threading.Event()
    callback_returned = threading.Event()

    def callback(out_data: Any, frame_count: int, time_info: Any, status_flags: int) -> tuple[bytes, int]:
        del out_data, time_info, status_flags
        if cancellation.is_set():
            callback_returned.set()
            return bytes(frame_count * channels * 2), getattr(module, "paAbort", 2)
        return bytes(frame_count * channels * 2), getattr(module, "paContinue", 0)

    stream = audio.open(
        format=module.paInt16, channels=channels, rate=rate, output=True,
        output_device_index=device["index"], frames_per_buffer=max(1, round(rate * 0.02)),
        stream_callback=callback, start=False,
    )
    try:
        stream.start_stream()
        cancellation.set()
        while time.monotonic() < deadline:
            if callback_returned.is_set() and not stream.is_active():
                return {"callback_aborted": True, "inactive_after_abort": True}
            time.sleep(0.005)
        raise SelfTestError("cancel_deadline")
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _wait_for_capture(analyzer: ToneAnalyzer, sample_target: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        if analyzer.snapshot()["sample_count"] >= sample_target:
            return
        time.sleep(0.005)
    raise SelfTestError("capture_deadline")


def _resampler_check() -> dict[str, Any]:
    try:
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        from qqcall_pcm.backend import _Pcm16Resampler
    except Exception:
        raise SelfTestError("resampler_unavailable") from None
    samples = [int(8_000 * math.sin(2 * math.pi * 440 * index / 48_000)) for index in range(4_800)]
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    whole = _Pcm16Resampler(48_000, 16_000)
    expected = whole.process(pcm) + whole.flush()
    split = _Pcm16Resampler(48_000, 16_000)
    actual = split.process(pcm[: 1_234 * 2]) + split.process(pcm[1_234 * 2 :]) + split.flush()
    if actual != expected or len(actual) != 1_600 * 2:
        raise SelfTestError("resampler_validation_failed")
    aliased_input = [round(8_000 * math.sin(2 * math.pi * 12_000 * index / 48_000)) for index in range(4_800)]
    alias_sampler = _Pcm16Resampler(48_000, 16_000)
    alias_pcm = alias_sampler.process(struct.pack(f"<{len(aliased_input)}h", *aliased_input)) + alias_sampler.flush()
    alias_values = struct.unpack(f"<{len(alias_pcm)//2}h", alias_pcm)[100:-100]
    alias_rms = math.sqrt(sum(value * value for value in alias_values) / len(alias_values))
    if alias_rms > (8_000 / math.sqrt(2)) * 0.01:
        raise SelfTestError("resampler_alias_rejection_failed")
    return {"verified": True, "input_rate": 48_000, "output_rate": 16_000, "chunk_continuity": True,
            "above_nyquist_rejection_verified": True}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if sys.platform != "win32":
        raise SelfTestError("windows_wasapi_required")
    if args.max_seconds < 2.0 or args.max_seconds > 15.0:
        raise SelfTestError("max_seconds_invalid")
    started = time.monotonic()
    deadline = started + args.max_seconds
    module = _load_audio()
    audio = module.PyAudio()
    streams: list[Any] = []
    termination_safe = True
    try:
        devices = [item for item in _devices(audio) if "wasapi" in item["host_api"].casefold()]
        out_render = _unique(devices, args.outgoing_render, lambda d: d["outputs"] > 0, "outgoing_render_missing")
        out_capture = _unique(
            devices, args.outgoing_capture,
            lambda d: d["inputs"] > 0 and not d["loopback"], "outgoing_capture_missing",
        )
        in_render = _unique(devices, args.incoming_render, lambda d: d["outputs"] > 0, "incoming_render_missing")
        in_loopback = _unique(
            devices, args.incoming_loopback,
            lambda d: d["inputs"] > 0 and d["loopback"], "incoming_loopback_missing",
        )
        if not all(_is_virtual(item["name"]) for item in (out_render, out_capture, in_render, in_loopback)):
            raise SelfTestError("physical_endpoint_refused")
        if out_render["name"].casefold() == in_render["name"].casefold():
            raise SelfTestError("audio_routes_must_be_distinct")
        if _loopback_base(in_loopback["name"]).casefold() != in_render["name"].casefold():
            raise SelfTestError("incoming_loopback_mismatch")
        out_channels = min(2, out_capture["inputs"])
        loop_channels = min(2, in_loopback["inputs"])
        if not out_channels or not loop_channels:
            raise SelfTestError("capture_endpoint_unavailable")
        out_analyzer = ToneAnalyzer(out_capture["rate"], out_channels)
        in_analyzer = ToneAnalyzer(in_loopback["rate"], loop_channels)
        failure = threading.Event()
        out_stream = audio.open(
            format=module.paInt16, channels=out_channels, rate=out_capture["rate"], input=True,
            input_device_index=out_capture["index"],
            frames_per_buffer=max(1, round(out_capture["rate"] * 0.02)),
            stream_callback=_make_capture_callback(module, out_analyzer, failure), start=False,
        )
        streams.append(out_stream)
        loop_stream = audio.open(
            format=module.paInt16, channels=loop_channels, rate=in_loopback["rate"], input=True,
            input_device_index=in_loopback["index"],
            frames_per_buffer=max(1, round(in_loopback["rate"] * 0.02)),
            stream_callback=_make_capture_callback(module, in_analyzer, failure), start=False,
        )
        streams.append(loop_stream)
        out_stream.start_stream()
        loop_stream.start_stream()
        duration = min(0.45, (args.max_seconds - 1.0) / 3.0)
        if duration < 0.2:
            raise SelfTestError("max_seconds_invalid")

        out_analyzer.reset(440)
        in_analyzer.reset(440)
        out_drain = _play_tone(module, audio, out_render, 440, duration, deadline)
        _wait_for_capture(out_analyzer, int(out_capture["rate"] * duration * 0.75), deadline)
        _wait_for_capture(in_analyzer, int(in_loopback["rate"] * duration * 0.75), deadline)
        outgoing_signal = out_analyzer.snapshot()
        incoming_crosstalk = in_analyzer.snapshot()

        in_analyzer.reset(880)
        out_analyzer.reset(880)
        in_drain = _play_tone(module, audio, in_render, 880, duration, deadline)
        _wait_for_capture(in_analyzer, int(in_loopback["rate"] * duration * 0.75), deadline)
        _wait_for_capture(out_analyzer, int(out_capture["rate"] * duration * 0.75), deadline)
        incoming_signal = in_analyzer.snapshot()
        outgoing_crosstalk = out_analyzer.snapshot()
        if failure.is_set():
            raise SelfTestError("capture_overflow")
        if outgoing_signal["tone_rms"] < 200 or incoming_signal["tone_rms"] < 200:
            raise SelfTestError("virtual_route_signal_missing")
        if incoming_crosstalk["tone_rms"] > max(100, outgoing_signal["tone_rms"] * 0.2):
            raise SelfTestError("outgoing_audio_crosstalk")
        if outgoing_crosstalk["tone_rms"] > max(100, incoming_signal["tone_rms"] * 0.2):
            raise SelfTestError("incoming_audio_crosstalk")
        cancel = _cancel_check(module, audio, out_render, deadline)
        return {
            "status": "ok",
            "endpoint_routes_verified": True,
            "qq_route_selected_or_verified": False,
            "remote_playback_confirmed": False,
            "routes": {
                "outgoing_render": out_render["name"],
                "outgoing_capture": out_capture["name"],
                "incoming_render": in_render["name"],
                "incoming_loopback": in_loopback["name"],
            },
            "outgoing_tone_capture": outgoing_signal,
            "outgoing_tone_leak_on_inbound_route": incoming_crosstalk,
            "incoming_tone_capture": incoming_signal,
            "incoming_tone_leak_on_outgoing_route": outgoing_crosstalk,
            "render_drain": {"outgoing": out_drain, "incoming": in_drain},
            "cancel": cancel,
            "resampler": _resampler_check(),
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    finally:
        for stream in reversed(streams):
            try:
                if stream.is_active():
                    stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        try:
            audio.terminate()
        except Exception:
            termination_safe = False
        if not termination_safe:
            raise SelfTestError("audio_host_cleanup_failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify synthetic-tone routing between explicit virtual WASAPI endpoint pairs."
    )
    parser.add_argument("--outgoing-render", required=True, help="exact virtual render name feeding QQ mic")
    parser.add_argument("--outgoing-capture", required=True, help="exact paired virtual capture/input name")
    parser.add_argument("--incoming-render", required=True, help="exact separate virtual render receiving QQ speaker")
    parser.add_argument("--incoming-loopback", required=True, help="exact loopback name for incoming-render")
    parser.add_argument("--max-seconds", type=float, default=3.0, help="bounded total run time (2 to 15 seconds)")
    parser.add_argument("--report", type=Path, help="optional path for metadata-only JSON report")
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except SelfTestError as exc:
        report = {
            "status": "error",
            "error_code": exc.code,
            "endpoint_routes_verified": False,
            "qq_route_selected_or_verified": False,
            "remote_playback_confirmed": False,
        }
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 2
    serialized = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    if args.report is not None:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(serialized + "\n", encoding="utf-8")
        except OSError:
            print(json.dumps({"status": "error", "error_code": "report_write_failed"}))
            return 2
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
