from __future__ import annotations

import json
import threading
import time

from plugins.mai_recog_self.backend.runtime import InferenceService, _command


def test_inference_worker_launch_targets_plugin_backend_module(tmp_path):
    assert _command("worker-python", tmp_path) == [
        "worker-python",
        "-X",
        "utf8",
        "-u",
        "-m",
        "plugins.mai_recog_self.backend.worker",
        "serve",
        str(tmp_path),
    ]


class PartialWriter:
    def __init__(self, limit: int = 17):
        self.limit = limit
        self.value = bytearray()
        self.closed = False

    def write(self, data):
        if self.closed:
            raise BrokenPipeError
        chunk = bytes(data[: self.limit])
        self.value.extend(chunk)
        return len(chunk)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class ResponseReader:
    def __init__(self, response: bytes):
        self.response = response
        self.closed = False

    def readline(self):
        if self.closed:
            return b""
        return self.response

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, response: bytes = b'{"status":"ok"}\n'):
        self.stdin = PartialWriter()
        self.stdout = ResponseReader(response)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def test_exchange_completes_short_writes_for_payload_larger_than_pipe_buffer(tmp_path):
    service = InferenceService(tmp_path)
    try:
        process = FakeProcess()
        request = {"version": "v", "image": "x" * 180_000}
        result = service._exchange(process, request, 3.0)
        expected = (json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        assert result == {"status": "ok"}
        assert bytes(process.stdin.value) == expected
    finally:
        service.close()


def test_candidate_failure_is_cached_while_previous_loaded_version_keeps_serving(tmp_path, monkeypatch):
    service = InferenceService(tmp_path)
    process = FakeProcess()
    requested = []
    service._process = process
    service._ready = True
    service._loaded_version = "old-version"
    monkeypatch.setattr(service, "_desired", lambda: "bad-version")

    def exchange(_process, request, timeout):
        version = request["version"]
        requested.append(version)
        if version == "bad-version":
            return {"status": "model_error", "error": "audit rejected", "loaded_version": "old-version"}
        return {
            "status": "ok",
            "version": version,
            "loaded_version": version,
            "score": 0.3,
            "judgment": "不是自己",
        }

    monkeypatch.setattr(service, "_exchange", exchange)
    first = service.infer(b"image", timeout=2)
    second = service.infer(b"image", timeout=2)
    assert first["status"] == second["status"] == "ok"
    assert first["version"] == second["version"] == "old-version"
    assert requested == ["bad-version", "old-version", "old-version"]
    assert service.status()["desired_version"] == "bad-version"
    assert service.status()["loaded_version"] == "old-version"
    assert service.status()["failed_candidate_version"] == "bad-version"
    assert service.status()["last_error"] == "audit rejected"
    service.close()


def test_invalid_startup_candidate_is_not_reloaded_on_each_inference(tmp_path, monkeypatch):
    service = InferenceService(tmp_path)
    process = FakeProcess()
    service._process = process
    service._ready = True
    service._failed_candidate_version = "bad-version"
    service._last_error = "audit rejected"
    monkeypatch.setattr(service, "_desired", lambda: "bad-version")

    def forbidden_exchange(*_args, **_kwargs):
        raise AssertionError("cached invalid model was retried")

    monkeypatch.setattr(service, "_exchange", forbidden_exchange)
    result = service.infer(b"image", timeout=1)
    assert result["status"] == "model_error"
    assert result["loaded_version"] == ""
    assert result["desired_version"] == "bad-version"
    service.close()


def test_close_is_idempotent_and_infer_after_close_cannot_spawn(tmp_path, monkeypatch):
    service = InferenceService(tmp_path)
    monkeypatch.setattr(service, "_desired", lambda: "a" * 32)

    def forbidden_spawn():
        raise AssertionError("closed inference service tried to spawn a child")

    monkeypatch.setattr(service, "_spawn", forbidden_spawn)
    service.close()
    service.close()
    assert service.infer(b"image", timeout=1)["status"] == "stopped"
    assert service.status()["closed"] is True


def test_close_during_background_spawn_reaps_the_racing_worker_and_prevents_retry(tmp_path, monkeypatch):
    import plugins.mai_recog_self.backend.runtime as runtime

    monkeypatch.setattr(runtime, "_setting", lambda _root, key, default="": "a" * 32 if key == "active" else default)
    entered_spawn = threading.Event()
    release_spawn = threading.Event()
    processes = []

    def delayed_popen(*_args, **_kwargs):
        entered_spawn.set()
        assert release_spawn.wait(3)
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", delayed_popen)
    service = InferenceService(tmp_path)
    assert entered_spawn.wait(2)
    closing = threading.Thread(target=service.close)
    closing.start()
    deadline = time.monotonic() + 2
    while not service.status()["closed"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.status()["closed"] is True
    release_spawn.set()
    closing.join(2)
    assert not closing.is_alive()
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert service.infer(b"image", timeout=1)["status"] == "stopped"
    assert service.status()["warming"] is False


def test_exchange_timeout_stops_and_closes_only_its_worker(tmp_path):
    service = InferenceService(tmp_path)
    process = FakeProcess()
    gate = threading.Event()

    def blocked_readline():
        gate.wait(2)
        return b""

    process.stdout.readline = blocked_readline
    try:
        assert service._exchange(process, {"x": 1}, 0.02) is None
        assert process.poll() is not None
        assert process.stdin.closed and process.stdout.closed
    finally:
        gate.set()
        service.close()

