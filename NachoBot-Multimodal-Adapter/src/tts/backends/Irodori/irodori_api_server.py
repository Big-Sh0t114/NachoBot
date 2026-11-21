"""Private, stdlib HTTP bridge for one persistent Irodori inference runtime."""

from __future__ import annotations

import argparse
from io import BytesIO
import gc
import json
from pathlib import Path
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import wave

from config import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_SECONDS,
    MAX_TEXT_LENGTH,
    IrodoriConfigError,
    load_irodori_config,
    sampling_request_kwargs,
)

if __package__:
    from .runtime_loader import load_inference_runtime
else:
    from runtime_loader import load_inference_runtime


class IrodoriHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, handler, *, runtime, sampling_request_cls):
        super().__init__(address, handler)
        self.runtime = runtime
        self.sampling_request_cls = sampling_request_cls
        self.synthesis_lock = threading.Lock()


def _to_pcm16(audio) -> tuple[bytes, int]:
    """Convert one mono floating waveform to explicitly encoded PCM16."""

    import numpy as np

    tensor = audio.detach().to(device="cpu").float()
    array = tensor.numpy()
    if array.ndim == 2:
        if 1 not in array.shape:
            raise ValueError("Irodori output must be mono audio")
        array = array.reshape(-1)
    elif array.ndim != 1:
        raise ValueError("Irodori output has an unsupported audio shape")
    if array.size == 0 or not np.isfinite(array).all():
        raise ValueError("Irodori output audio is empty or non-finite")
    if array.size > MAX_AUDIO_SECONDS * 192_000:
        raise ValueError("Irodori output exceeds the audio sample budget")
    clipped = np.clip(array, -1.0, 1.0)
    pcm = (clipped * 32767.0).round().astype("<i2", copy=False).tobytes()
    if len(pcm) > MAX_AUDIO_BYTES:
        raise ValueError("Irodori output exceeds the audio byte budget")
    return pcm, int(array.size)


def _wav_bytes(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    if not isinstance(sample_rate, int) or sample_rate <= 0 or sample_rate > 192_000:
        raise ValueError("Irodori returned an invalid sample rate")
    if not pcm or len(pcm) % (channels * 2):
        raise ValueError("Irodori returned empty or misaligned PCM")
    if len(pcm) / (channels * 2 * sample_rate) > MAX_AUDIO_SECONDS:
        raise ValueError("Irodori output exceeds the audio duration budget")
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    payload = buffer.getvalue()
    if len(payload) > MAX_AUDIO_BYTES:
        raise ValueError("Irodori output exceeds the audio byte budget")
    return payload


def _log_cleanup_failure(operation: str, exc: Exception) -> None:
    try:
        print(f"[Irodori raw] {operation} failed: {type(exc).__name__}", flush=True)
    except Exception:
        pass


def _release_idle_cuda_memory(runtime, *, collect_garbage: bool = False) -> None:
    """Return unused CUDA allocator blocks after a completed request.

    Keep torch lazy for CPU-only runs and avoid initializing CUDA when a device
    has not already been initialized by the persistent runtime.
    """

    try:
        devices = tuple(
            device
            for device in (
                getattr(runtime, "model_device", None),
                getattr(runtime, "codec_device", None),
            )
            if getattr(device, "type", None) == "cuda"
            or str(device).strip().lower().startswith("cuda")
        )
        if not devices:
            return

        torch_module = sys.modules.get("torch")
        cuda = getattr(torch_module, "cuda", None)
        is_initialized = getattr(cuda, "is_initialized", None)
        if not callable(is_initialized) or not is_initialized():
            return

        if collect_garbage:
            try:
                gc.collect()
            except Exception as exc:
                _log_cleanup_failure("Python cleanup", exc)

        seen_devices: set[int] = set()
        for device in devices:
            try:
                resolved = torch_module.device(device)
                index = resolved.index
                if index is None:
                    index = cuda.current_device()
                index = int(index)
                if index in seen_devices:
                    continue
                seen_devices.add(index)
                with cuda.device(index):
                    cuda.empty_cache()
            except Exception as exc:
                # Cleanup is best-effort and must never replace a synthesis error.
                _log_cleanup_failure("CUDA cache cleanup", exc)
    except Exception as exc:
        _log_cleanup_failure("CUDA cache cleanup", exc)


def _synthesize_wav_payload(runtime, sample) -> tuple[bytes | None, str | None]:
    """Synthesize and encode one response, then drop request tensors and caches."""

    result = None
    pcm = None
    payload = None
    failure_type = None
    try:
        result = runtime.synthesize(sample)
        pcm, _sample_count = _to_pcm16(result.audio)
        payload = _wav_bytes(pcm, int(result.sample_rate))
    except Exception as exc:
        failure_type = type(exc).__name__
    finally:
        # The result may own CUDA tensors for alternate runtimes. Drop it before
        # collecting cycles and releasing only unused allocator blocks.
        result = None
        pcm = None
        _release_idle_cuda_memory(runtime, collect_garbage=failure_type is not None)
    return payload, failure_type


class IrodoriRequestHandler(BaseHTTPRequestHandler):
    server: IrodoriHTTPServer

    def log_message(self, fmt: str, *args) -> None:
        # Request text is never part of a URL. Keep raw access logs terse.
        print(f"[Irodori raw] {self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(404, {"error": "not found"})
            return
        self._json(
            200,
            {
                "status": "ok",
                "ready": True,
                "model_loaded": True,
                "native_streaming": False,
                "stream_capability": "buffered_pcm",
                "sample_rate": int(self.server.runtime.codec.sample_rate),
            },
        )

    def do_POST(self) -> None:
        if self.path != "/tts":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(400, {"error": "invalid request"})
            return
        if length <= 0 or length > 64 * 1024:
            self._json(413, {"error": "request body is outside the accepted size"})
            return
        self.connection.settimeout(10.0)
        try:
            raw = self.rfile.read(length)
            request = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "invalid request"})
            return
        if not isinstance(request, dict) or set(request) != {"text"}:
            self._json(400, {"error": "request must contain only text"})
            return
        text = request.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
            self._json(400, {"error": "text must contain 1 to 10000 characters"})
            return
        if not self.server.synthesis_lock.acquire(blocking=False):
            self._json(503, {"error": "Irodori inference is busy"})
            return
        lock_held = True
        try:
            sample = self.server.sampling_request_cls(
                text=text,
                **sampling_request_kwargs(self.server.irodori_config),
            )
            payload, failure_type = _synthesize_wav_payload(self.server.runtime, sample)
            self.server.synthesis_lock.release()
            lock_held = False
            if failure_type is not None:
                print(f"[Irodori raw] synthesis failed: {failure_type}", flush=True)
                self._json(500, {"error": "synthesis failed"})
                return
            if payload is None:
                raise RuntimeError("Irodori returned no WAV payload")
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # Generation and memory cleanup finish before response I/O, including
            # when a canceled client has already closed its socket.
            return
        except Exception as exc:
            print(f"[Irodori raw] synthesis failed: {type(exc).__name__}", flush=True)
            try:
                self._json(500, {"error": "synthesis failed"})
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            if lock_held:
                self.server.synthesis_lock.release()


def _resolve_runtime(config: dict, repo_path: Path):
    if not repo_path.is_dir() or not (repo_path / "irodori_tts" / "inference_runtime.py").is_file():
        raise FileNotFoundError(f"Irodori source is incomplete: {repo_path}")
    if str(repo_path) not in sys.path:
        sys.path.insert(0, str(repo_path))
    import irodori_tts.inference_runtime as runtime_module

    runtime_config = config["runtime"]
    checkpoint = runtime_config["checkpoint"]
    if checkpoint is None:
        checkpoint = Path(runtime_module.download_hf_checkpoint(runtime_config["hf_checkpoint"]))
    if not checkpoint.is_file():
        raise FileNotFoundError(f"Irodori checkpoint does not exist: {checkpoint}")

    model_device = runtime_config["model_device"]
    codec_device = runtime_config["codec_device"]
    if model_device == "auto":
        model_device = runtime_module.default_runtime_device()
    if codec_device == "auto":
        codec_device = model_device
    model_precision = runtime_config["model_precision"]
    codec_precision = runtime_config["codec_precision"]
    if model_precision == "auto":
        model_precision = "bf16" if str(model_device).startswith("cuda") else "fp32"
    if codec_precision == "auto":
        codec_precision = "bf16" if str(codec_device).startswith("cuda") else "fp32"
    key = runtime_module.RuntimeKey(
        checkpoint=str(checkpoint),
        model_device=model_device,
        codec_repo=runtime_config["codec_repo"],
        model_precision=model_precision,
        codec_device=codec_device,
        codec_precision=codec_precision,
    )
    return load_inference_runtime(runtime_module, key), runtime_module.SamplingRequest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private managed Irodori-TTS raw API")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo-path", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Irodori raw API must bind to loopback")
    if not 1 <= args.port <= 65535 or args.port == 9880:
        raise SystemExit("Irodori raw API cannot bind the public 9880 port")

    config = load_irodori_config(args.config)
    if args.port != config["tts"]["port"]:
        # Supervisor port overrides apply to the client and child together.
        config["tts"]["port"] = args.port
    repo_path = args.repo_path.expanduser().resolve(strict=False)
    runtime, sampling_request_cls = _resolve_runtime(config, repo_path)
    server = IrodoriHTTPServer(
        (args.host, args.port),
        IrodoriRequestHandler,
        runtime=runtime,
        sampling_request_cls=sampling_request_cls,
    )
    server.irodori_config = config
    print(
        f"[Irodori raw] loaded persistent model on {runtime.model_device}; "
        f"listening on {args.host}:{args.port}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
