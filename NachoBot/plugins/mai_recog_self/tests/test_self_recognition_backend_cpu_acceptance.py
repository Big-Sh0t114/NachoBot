"""Opt-in, real MobileNet lifecycle acceptance on synthetic temporary data."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image
import numpy as np
import pytest


CORE_ROOT = Path(__file__).resolve().parents[3]
CLI = CORE_ROOT / "plugins" / "mai_recog_self" / "self_recognition.py"
WORKER_PYTHON = os.environ.get("SELF_RECOGNITION_TEST_WORKER_PYTHON", "")


def _image(seed: int, label: str) -> bytes:
    rng = np.random.default_rng(seed)
    # Class-dependent color bias gives the classifier a learnable signal while
    # the independent noise keeps the images in distinct duplicate groups.
    pixels = rng.integers(0, 120, (64, 64, 3), dtype=np.uint8)
    pixels[:, :, 0 if label == "self" else 2] += 120
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, format="PNG")
    return output.getvalue()


def _cli(root: Path, *args: str, timeout: float = 900) -> dict:
    command = [sys.executable, str(CLI), "--data-dir", str(root), *args]
    completed = subprocess.run(
        command,
        cwd=CORE_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        pytest.fail(f"CLI failed ({completed.returncode}):\n{completed.stdout}\n{completed.stderr}")
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        pytest.fail(f"CLI returned no JSON: {completed.stdout!r} {completed.stderr!r} ({exc})")


def _write_group(folder: Path, seed: int, label: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"synthetic-{seed:03d}.png").write_bytes(_image(seed, label))


def _last_json(text: str) -> dict:
    return json.loads(text.strip().splitlines()[-1])


@pytest.mark.skipif(not WORKER_PYTHON, reason="set SELF_RECOGNITION_TEST_WORKER_PYTHON to a Torch worker interpreter")
def test_cpu_fresh_pause_resume_activate_infer_incremental_and_rollback(tmp_path):
    worker = Path(WORKER_PYTHON).resolve()
    if not worker.is_file():
        pytest.skip(f"CPU worker interpreter does not exist: {worker}")
    check = subprocess.run(
        [str(worker), "-c", "import torch, torchvision, PIL, psutil; print(torch.__version__)"],
        cwd=CORE_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if check.returncode:
        pytest.skip(f"worker interpreter is missing training dependencies: {check.stderr[-1200:]}")

    root = tmp_path / "catalog"
    self_dir = tmp_path / "raw" / "self"
    other_dir = tmp_path / "raw" / "other"
    for seed in range(20):
        _write_group(self_dir, seed, "self")
        _write_group(other_dir, seed + 100, "other")
    imported = _cli(root, "import", "--self", str(self_dir), "--other", str(other_dir))
    assert imported["imported"] == 40
    assert imported["errors"] == 0
    assert all((self_dir / f"synthetic-{seed:03d}.png").is_file() for seed in range(20))

    environment = os.environ.copy()
    overlay = os.environ.get("SELF_RECOGNITION_TEST_DEPS", "")
    python_paths = [str(CORE_ROOT)]
    if overlay:
        python_paths.insert(0, overlay)
    inherited = environment.get("PYTHONPATH")
    if inherited:
        python_paths.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    training = subprocess.Popen(
        [
            sys.executable,
            str(CLI),
            "--data-dir",
            str(root),
            "--worker-python",
            str(worker),
            "train",
            "--mode",
            "fresh",
            "--device",
            "cpu",
            "--input-size",
            "224",
        ],
        cwd=CORE_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        first_line = training.stdout.readline().strip()
        assert first_line, "training produced no progress output"
        first = json.loads(first_line)
        job_id = first["job_id"]
        assert len(job_id) == 32
        pause = subprocess.run(
            [sys.executable, str(CLI), "--data-dir", str(root), "pause", job_id],
            cwd=CORE_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            check=False,
        )
        assert pause.returncode == 0, pause.stderr
        paused = _last_json(pause.stdout)
        assert paused["state"] == "paused"
        stdout, stderr = training.communicate(timeout=120)
        assert training.returncode == 0, f"{stdout}\n{stderr}"
        assert _last_json(stdout)["job"]["state"] == "paused"
        assert (root / "jobs" / job_id / "resume.pt").is_file()

        resumed = _cli(
            root,
            "--worker-python",
            str(worker),
            "train",
            "--resume",
            job_id,
            timeout=1200,
        )
        assert resumed["job"]["state"] == "complete"
        first_report = _cli(root, "report", job_id)["report"]
        assert first_report["audit"]["passed"] is True
        assert len(first_report["trained_image_ids"]) > 0
        first_version = job_id
        first_model = _cli(root, "--worker-python", str(worker), "activate", first_version)
        assert first_model["status"] == "activated"
        image = tmp_path / "inference.png"
        image.write_bytes(_image(777, "self"))
        first_inference = _cli(root, "--worker-python", str(worker), "test", str(image), "--timeout", "180", timeout=240)
        assert first_inference["status"] == "ok"
        assert first_inference["loaded_version"] == first_version
        assert 0.0 <= first_inference["score"] <= 1.0

        for seed in range(20, 23):
            _write_group(self_dir, seed, "self")
            _write_group(other_dir, seed + 100, "other")
        added = _cli(root, "import", "--self", str(self_dir), "--other", str(other_dir))
        assert added["imported"] == 6
        new_image_ids = {
            item["id"] for item in added["items"] if item.get("id") and not item.get("already_exists")
        }
        assert len(new_image_ids) == 6
        incremental = _cli(
            root,
            "--worker-python",
            str(worker),
            "train",
            "--mode",
            "incremental",
            "--device",
            "cpu",
            "--input-size",
            "224",
            timeout=1200,
        )
        assert incremental["job"]["state"] == "complete"
        report = _cli(root, "report", incremental["job"]["id"])
        assert report["report"]["audit"]["passed"] is True
        assert set(report["report"]["trained_image_ids"]) == new_image_ids
        new_version = incremental["job"]["id"]
        assert new_version != first_version
        assert _cli(root, "--worker-python", str(worker), "activate", new_version)["status"] == "activated"
        rolled_back = _cli(root, "--worker-python", str(worker), "rollback")
        assert rolled_back["version"] == first_version
        final_inference = _cli(root, "--worker-python", str(worker), "test", str(image), "--timeout", "180", timeout=240)
        assert final_inference["status"] == "ok"
        assert final_inference["loaded_version"] == first_version
    finally:
        if training.poll() is None:
            training.kill()
        try:
            training.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
