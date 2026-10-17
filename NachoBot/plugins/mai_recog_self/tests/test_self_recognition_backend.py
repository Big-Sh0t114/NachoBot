from __future__ import annotations

from pathlib import Path
import io
import hashlib
import json
import os
import subprocess
import sys
import time

from PIL import Image
import numpy as np
import pytest

from plugins.mai_recog_self.backend.control import _recover_or_reject_live_workers, _resume, _worker_command, import_folders, status
from plugins.mai_recog_self.backend.processes import process_identity
from plugins.mai_recog_self.backend.store import Store, atomic_json, digest
from plugins.mai_recog_self.backend.worker import _version_artifact


CORE_ROOT = Path(__file__).resolve().parents[3]
CLI = CORE_ROOT / "plugins" / "mai_recog_self" / "self_recognition.py"


def test_training_worker_launch_targets_plugin_backend_module(tmp_path):
    command = _worker_command("worker-python", "train", tmp_path)

    assert command == [
        "worker-python",
        "-X",
        "utf8",
        "-u",
        "-m",
        "plugins.mai_recog_self.backend.worker",
        "train",
        str(tmp_path.resolve()),
    ]


def image_bytes(seed: int) -> bytes:
    pixels = np.random.default_rng(seed).integers(0, 256, (48, 64, 3), dtype=np.uint8)
    image = Image.fromarray(pixels)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def add_image(store: Store, seed: int, label: str) -> str:
    source = store.root / "uploads" / f"{seed}-{time.time_ns()}.png"
    source.write_bytes(image_bytes(seed))
    return store.ingest(source, label)["id"]


def test_folder_import_preserves_sources_reports_duplicate_label_conflict_and_requires_explicit_label(tmp_path):
    data = tmp_path / "catalog"
    own = tmp_path / "self"
    others = tmp_path / "other"
    own.mkdir()
    others.mkdir()
    raw = image_bytes(301)
    source_a = own / "first.png"
    source_b = others / "same.png"
    source_a.write_bytes(raw)
    source_b.write_bytes(raw)

    result = import_folders(data, own, others)
    assert result["imported"] == 1
    assert result["label_conflicts"] == 1
    assert source_a.read_bytes() == raw and source_b.read_bytes() == raw
    conflict = next(item for item in result["items"] if item.get("import_status") == "duplicate_label_conflict")
    image_id = conflict["id"]
    store = Store(data, initialize=False, readonly=True)
    assert store.images()["rows"][0]["label"] == "self"

    completed = subprocess.run(
        [sys.executable, str(CLI), "--data-dir", str(data), "label", image_id, "--label", "other"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["label"] == "other"
    assert Store(data, initialize=False, readonly=True).images()["rows"][0]["label"] == "other"
    again = import_folders(data, own, others)
    assert again["label_conflicts"] == 1
    assert Store(data, initialize=False, readonly=True).images()["rows"][0]["label"] == "other"


def test_folder_import_rejects_oversized_static_image_without_consuming_source(tmp_path):
    root = tmp_path / "catalog"
    own = tmp_path / "self"
    others = tmp_path / "other"
    own.mkdir()
    others.mkdir()
    source = own / "oversized.png"
    payload = b"x" * (20 * 1024 * 1024 + 1)
    source.write_bytes(payload)

    result = import_folders(root, own, others)
    assert result["errors"] == 1
    assert result["imported"] == 0
    assert source.read_bytes() == payload
    assert Store(root, initialize=False, readonly=True).images()["total"] == 0
    assert list((root / "uploads").iterdir()) == []

def test_incremental_snapshot_includes_a_previously_trained_group_after_explicit_relabel(tmp_path):
    store = Store(tmp_path / "catalog")
    for seed in range(42):
        add_image(store, seed, "self" if seed < 21 else "other")
    first_job = "a" * 32
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        first = store.freeze(db, first_job, "fresh", "cpu", 224)
        (store.root / "jobs" / first_job).mkdir()
        (store.root / "jobs" / first_job / "snapshot.json").write_text(json.dumps(first), encoding="utf-8")
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (first_job, first_job, "complete", time.time(), digest(first)),
        )
        db.execute("INSERT INTO models (id,job_id,sha,metrics,created) VALUES (?,?,?,?,?)", (first_job, first_job, "0" * 64, "{}", time.time()))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)", (first_job,))
    trained = next(row for row in first["images"] if row["label"] == "self" and row["split"] == "train")
    store.label(trained["id"], "other")
    second_job = "b" * 32
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        second = store.freeze(db, second_job, "incremental", "cpu", 224)
    changed = next(row for row in second["images"] if row["id"] == trained["id"])
    assert changed["label"] == "other" and changed["split"] == "train"
    assert second["parent"] == first_job and second["mode"] == "incremental"


def test_status_is_read_only_and_does_not_recover_an_active_training_row(tmp_path):
    root = tmp_path / "catalog"
    store = Store(root)
    job_id = "c" * 32
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (job_id, job_id, "running", time.time(), ""),
        )
    def logical_state():
        with store.db() as db:
            schema = [tuple(row) for row in db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
            jobs = [tuple(row) for row in db.execute("SELECT * FROM jobs ORDER BY id")]
            settings = [tuple(row) for row in db.execute("SELECT * FROM settings ORDER BY key")]
        return schema, jobs, settings

    before = logical_state()
    names_before = {path.name for path in root.iterdir()}
    result = status(root)
    after = logical_state()
    names_after = {path.name for path in root.iterdir()}
    assert next(job for job in result["jobs"] if job["id"] == job_id)["state"] == "running"
    assert before == after
    # SQLite may create WAL coordination sidecars for a read-only connection.
    assert names_after - names_before <= {"catalog.sqlite3-wal", "catalog.sqlite3-shm"}
    assert not (root / "jobs" / job_id / "cancel").exists()
    assert Store(root, initialize=False, readonly=True).job(job_id)["state"] == "running"


def test_stale_queued_job_without_published_pid_is_recovered_after_lease_expiry(tmp_path):
    root = tmp_path / "catalog"
    store = Store(root)
    job_id = "d" * 32
    created = time.time() - 200
    directory = root / "jobs" / job_id
    directory.mkdir()
    lease = directory / "lease"
    lease.touch()
    os.utime(lease, (created, created))
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (job_id, job_id, "queued", created, ""),
        )
    _recover_or_reject_live_workers(store)
    assert store.job(job_id)["state"] == "interrupted"


def test_stale_active_job_with_unreadable_worker_identity_fails_safe(tmp_path):
    root = tmp_path / "catalog"
    store = Store(root)
    job_id = "9" * 32
    created = time.time() - 200
    directory = root / "jobs" / job_id
    directory.mkdir()
    (directory / "worker.json").write_text("{invalid", encoding="utf-8")
    lease = directory / "lease"
    lease.touch()
    os.utime(lease, (created, created))
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (job_id, job_id, "running", created, ""),
        )
    with pytest.raises(RuntimeError, match="仍可能存活"):
        _recover_or_reject_live_workers(store)
    assert store.job(job_id)["state"] == "running"


def test_live_database_pid_cannot_be_hidden_by_previous_worker_identity(tmp_path, monkeypatch):
    import plugins.mai_recog_self.backend.control as control

    root = tmp_path / "catalog"
    store = Store(root)
    job_id = "8" * 32
    directory = root / "jobs" / job_id
    directory.mkdir()
    (directory / "worker.json").write_text(json.dumps({"pid": 123, "created": 1000.0}), encoding="utf-8")
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,?,'',?,?)",
            (job_id, job_id, "queued", 456, 2000.0, ""),
        )
    monkeypatch.setattr(control, "process_identity", lambda pid: {"pid": pid, "created": 2001.0})
    monkeypatch.setattr(control, "worker_is_same_process", lambda _identity: False)

    with pytest.raises(RuntimeError, match="仍可能存活"):
        _recover_or_reject_live_workers(store)
    assert store.job(job_id)["state"] == "queued"


def test_resume_invalidates_old_identity_and_fresh_lease_blocks_spawn_publication_gap(tmp_path, monkeypatch):
    import plugins.mai_recog_self.backend.control as control

    root = tmp_path / "catalog"
    store = Store(root)
    job_id = "7" * 32
    directory = root / "jobs" / job_id
    directory.mkdir()
    snapshot = {"job_id": job_id}
    snapshot_sha = digest(snapshot)
    (directory / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    checkpoint = directory / "resume.pt"
    checkpoint.write_bytes(b"verified-checkpoint")
    atomic_json(
        directory / "resume-audit.json",
        {"snapshot_sha": snapshot_sha, "sha": hashlib.sha256(checkpoint.read_bytes()).hexdigest()},
    )
    (directory / "worker.json").write_text(json.dumps({"pid": 123, "created": 1000.0}), encoding="utf-8")
    created = time.time() - 300
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (job_id, job_id, "paused", created, snapshot_sha),
        )

    def stop_before_launch(current_store, current_job_id, _python):
        assert current_job_id == job_id
        assert not (directory / "worker.json").exists()
        row = current_store.job(job_id)
        assert row["state"] == "queued" and row["pid"] is None
        assert time.time() - (directory / "lease").stat().st_mtime < 3
        with pytest.raises(RuntimeError, match="尚未发布worker身份"):
            _recover_or_reject_live_workers(current_store)
        raise RuntimeError("test stopped before worker launch")

    monkeypatch.setattr(control, "_spawn", stop_before_launch)
    with pytest.raises(RuntimeError, match="test stopped before worker launch"):
        _resume(store, job_id, "python")
    assert store.job(job_id)["state"] == "queued"


def test_confirmed_gone_workers_with_expired_lease_are_recovered(tmp_path, monkeypatch):
    import plugins.mai_recog_self.backend.control as control

    root = tmp_path / "catalog"
    store = Store(root)
    job_id = "6" * 32
    created = time.time() - 200
    directory = root / "jobs" / job_id
    directory.mkdir()
    (directory / "worker.json").write_text(json.dumps({"pid": 123, "created": created}), encoding="utf-8")
    lease = directory / "lease"
    lease.touch()
    os.utime(lease, (created, created))
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,?,'',?,?)",
            (job_id, job_id, "queued", 456, created, ""),
        )
    monkeypatch.setattr(control, "process_identity", lambda _pid: None)
    monkeypatch.setattr(control, "worker_is_same_process", lambda _identity: False)

    _recover_or_reject_live_workers(store)
    assert store.job(job_id)["state"] == "interrupted"


def test_live_worker_identity_blocks_mutation_without_killing_process(tmp_path):
    root = tmp_path / "catalog"
    store = Store(root)
    job_id = "e" * 32
    identity = process_identity(os.getpid())
    directory = root / "jobs" / job_id
    directory.mkdir()
    (directory / "worker.json").write_text(json.dumps(identity), encoding="utf-8")
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,?,'',?,?)",
            (job_id, job_id, "running", os.getpid(), time.time(), ""),
        )
    with pytest.raises(RuntimeError, match="仍可能存活"):
        _recover_or_reject_live_workers(store)
    assert store.job(job_id)["state"] == "running"
    assert process_identity(os.getpid()) is not None


def test_model_artifact_hash_and_audit_are_verified_before_cpu_load(tmp_path):
    root = tmp_path / "catalog"
    store = Store(root)
    version = "f" * 32
    artifact = root / "models" / version / "weights.pt"
    artifact.parent.mkdir(parents=True)
    content = b"synthetic-test-artifact"
    artifact.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    atomic_json(artifact.parent / "audit.json", {"passed": True, "sha256": sha})
    with store.db() as db:
        db.execute("INSERT INTO models (id,job_id,sha,metrics,created) VALUES (?,?,?,?,?)", (version, version, sha, "{}", time.time()))

    assert _version_artifact(Store(root, initialize=False, readonly=True), version) == (artifact, sha)
    artifact.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="哈希或审计"):
        _version_artifact(Store(root, initialize=False, readonly=True), version)


def test_cli_help_and_readonly_empty_status_work_from_an_arbitrary_directory(tmp_path):
    help_result = subprocess.run(
        [sys.executable, str(CLI), "--help"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert help_result.returncode == 0
    assert "import,label,train,status,report,activate,rollback,pause,cancel,test" in help_result.stdout
    missing = tmp_path / "does-not-exist"
    status_result = subprocess.run(
        [sys.executable, str(CLI), "--data-dir", str(missing), "status"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status_result.returncode == 0
    assert json.loads(status_result.stdout)["active_version"] == ""
    assert not missing.exists()


def test_cli_test_without_active_model_fails_immediately_without_creating_root(tmp_path):
    image = tmp_path / "valid.png"
    image.write_bytes(image_bytes(808))
    missing = tmp_path / "does-not-exist"
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(CLI), "--data-dir", str(missing), "test", str(image)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert completed.returncode == 2
    result = json.loads(completed.stderr)
    assert result["ok"] is False
    assert result["status"] == "no_model"
    assert "没有启用的模型" in result["error"]
    assert elapsed < 3
    assert not missing.exists()


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_cli_test_rejects_non_finite_timeout(tmp_path, timeout):
    image = tmp_path / "valid.png"
    image.write_bytes(image_bytes(809))
    completed = subprocess.run(
        [sys.executable, str(CLI), "--data-dir", str(tmp_path / "missing"), "test", str(image), f"--timeout={timeout}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 2
    result = json.loads(completed.stderr)
    assert result["ok"] is False
    assert "有限且大于0" in result["error"]
    assert not (tmp_path / "missing").exists()


def test_package_import_does_not_import_torch(tmp_path):
    code = "import sys; from plugins.mai_recog_self.backend import InferenceService; assert 'torch' not in sys.modules; print('torch-free')"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=CORE_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "torch-free"

