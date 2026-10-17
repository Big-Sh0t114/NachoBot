from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import numpy as np
import pytest

from plugins.mai_recog_self.backend.control import import_named_folders
from plugins.mai_recog_self.backend.labels import (
    metadata_digest,
    ordered_class_names,
    validate_class_name,
    validate_multiclass_metadata,
)
from plugins.mai_recog_self.backend.store import Store, digest
from plugins.mai_recog_self.backend.worker import _validate_model_metadata

CORE_ROOT = Path(__file__).resolve().parents[3]
CLI = CORE_ROOT / "plugins" / "mai_recog_self" / "self_recognition.py"
WORKER_PYTHON = os.environ.get("SELF_RECOGNITION_TEST_WORKER_PYTHON", "")


def _png(seed: int, label: str = "") -> bytes:
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 128, (48, 64, 3), dtype=np.uint8)
    if label == "other":
        pixels += 80
    else:
        channel = {"NachoBot": 0, "ShizukuBot": 1, "Moli": 2}.get(label, seed % 3)
        pixels[:, :, channel] += 128
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, format="PNG")
    return output.getvalue()


def _add(store: Store, seed: int, label: str) -> str:
    source = store.root / "uploads" / f"{seed}-{label}.png"
    source.write_bytes(_png(seed, label))
    return store.ingest(source, label)["id"]


def _catalog(store: Store, labels: list[str]) -> None:
    with store.db() as db:
        store.register_catalog(db, "multiclass", labels)


def _cli(root: Path, *arguments: str, environment: dict | None = None, timeout: float = 1800) -> dict:
    completed = subprocess.run(
        [sys.executable, str(CLI), "--data-dir", str(root), *arguments],
        cwd=CORE_ROOT,
        env=environment,
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
    (folder / f"synthetic-{seed:03d}.png").write_bytes(_png(seed, label))


def test_named_import_preserves_sources_and_registry_survives_missing_directory(tmp_path):
    source = tmp_path / "raw"
    first = source / "しずく"
    background = source / "other"
    first.mkdir(parents=True)
    background.mkdir()
    image_a = first / "nested.png"
    image_b = background / "bg.png"
    nested = first / "nested"
    nested.mkdir()
    image_a.unlink(missing_ok=True)
    image_a = nested / "one.png"
    image_a.write_bytes(_png(10, "Moli"))
    image_b.write_bytes(_png(11, "other"))

    root = tmp_path / "catalog"
    result = import_named_folders(root, source)
    assert result["task"] == "multiclass"
    assert result["class_names"] == ["しずく", "other"]
    assert result["imported"] == 2
    assert image_a.is_file() and image_b.is_file()

    # The second import omits the previously registered character directory.
    second_source = tmp_path / "second"
    (second_source / "Moli").mkdir(parents=True)
    (second_source / "other").mkdir()
    (second_source / "Moli" / "two.png").write_bytes(_png(12, "Moli"))
    (second_source / "other" / "two.png").write_bytes(_png(13, "other"))
    second = import_named_folders(root, second_source)
    assert "しずく" in second["class_names"]
    assert Store(root, initialize=False).registered_class_names() == ["Moli", "しずく", "other"]


@pytest.mark.parametrize("name", ["self", "skip", "unlabelled", "../bad", "CON", "x" * 65, "a\u202eb"])
def test_class_labels_reject_reserved_or_unsafe_names(name):
    with pytest.raises(ValueError):
        validate_class_name(name)


def test_catalog_rejects_multiclass_import_after_removed_legacy_rows(tmp_path):
    root = tmp_path / "catalog"
    store = Store(root)
    with store.db() as db:
        store.register_catalog(db, "binary")
    image_id = _add(store, 1, "self")
    store.set_removed(image_id, True)

    source = tmp_path / "named"
    (source / "Moli").mkdir(parents=True)
    (source / "other").mkdir()
    with pytest.raises(ValueError, match="--data-dir"):
        import_named_folders(root, source)


def test_training_readiness_requires_other(tmp_path):
    store = Store(tmp_path / "catalog")
    _catalog(store, ["Moli"])
    for seed in range(20):
        _add(store, seed, "Moli")
    readiness = store.training_readiness()
    assert readiness["groups"] == {"Moli": 20, "other": 0}
    assert not readiness["can_train"]


def test_snapshot_pins_and_validates_canonical_metadata_digest(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("torchvision")
    from plugins.mai_recog_self.backend.model import multiclass_metadata

    store = Store(tmp_path / "catalog")
    _catalog(store, ["Moli", "other"])
    for seed in range(20):
        _add(store, seed, "Moli")
        _add(store, seed + 100, "other")
    job_id = "a" * 32
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        snapshot = store.freeze(db, job_id, "fresh", "cpu", 224)
    assert snapshot["class_names"] == ["Moli", "other"]
    metadata = multiclass_metadata(snapshot)
    assert snapshot["metadata_sha256"] == metadata_digest(metadata)
    corrupted = dict(snapshot, score_threshold=0.7)
    with pytest.raises(ValueError, match="摘要"):
        multiclass_metadata(corrupted)


def test_multiclass_decisions_and_metrics_use_mapping_names():
    pytest.importorskip("torch")
    pytest.importorskip("torchvision")
    from plugins.mai_recog_self.backend.model import multiclass_decision, multiclass_metrics

    names = ["Moli", "NachoBot", "other"]
    uncertain = multiclass_decision([0.48, 0.46, 0.06], names, 0.6, 0.15)
    assert uncertain["predicted_class"] == "Moli"
    assert uncertain["character_id"] is None
    assert uncertain["judgment"] == "不确定"
    other = multiclass_decision([0.1, 0.2, 0.7], names, 0.6, 0.15)
    assert other["is_other"] and not other["recognized"]
    metrics = multiclass_metrics(["Moli", "NachoBot", "other"], [[0.8, 0.1, 0.1], [0.6, 0.3, 0.1], [0.1, 0.1, 0.8]], names)
    assert metrics["confusion_matrix"] == [[1, 0, 0], [1, 0, 0], [0, 0, 1]]
    assert set(metrics["per_class"]) == set(names)
    with pytest.raises(ValueError, match="概率"):
        multiclass_metrics([0], [[0.2, 0.2, 0.2]], names)


def test_multiclass_error_gallery_uses_mapping_names(tmp_path):
    names = ["Moli", "NachoBot", "other"]
    store = Store(tmp_path / "catalog")
    _catalog(store, names)
    image_id = _add(store, 77, "Moli")
    job_id = "b" * 32
    job_dir = store.root / "jobs" / job_id
    job_dir.mkdir()
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (job_id, job_id, "complete", time.time(), digest({})),
        )
    report = {
        "test_predictions": [
            {"id": image_id, "label": "Moli", "predicted_class": "other", "score": 0.7}
        ]
    }
    (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    gallery = store.images(view="errors", job_id=job_id)
    assert gallery["total"] == 1
    assert gallery["rows"][0]["model_text"].startswith("模型：other")
    assert gallery["rows"][0]["label_text"] == "Moli"


def test_schema2_metadata_is_strict_about_class_count_thresholds_and_order():
    ordered_class_names([f"class-{index}" for index in range(127)] + ["other"])
    with pytest.raises(ValueError, match="128"):
        ordered_class_names([f"class-{index}" for index in range(128)] + ["other"])
    # Re-importing the registry is allowed to pass duplicate entries before union.
    assert ordered_class_names(["Moli"] * 128) == ["Moli"]
    base = {
        "format_version": 2,
        "task": "multiclass",
        "class_names": ["Moli", "other"],
        "score_threshold": 0.6,
        "margin_threshold": 0.15,
    }
    assert validate_multiclass_metadata(base)["class_names"] == ["Moli", "other"]
    for field, value in (("margin_threshold", 1.01), ("score_threshold", float("nan")), ("class_names", ["other", "Moli"])):
        invalid = dict(base, **{field: value})
        with pytest.raises(ValueError):
            validate_multiclass_metadata(invalid)


def _schema2_validation_fixture(tmp_path):
    store = Store(tmp_path / "catalog")
    job_id = "c" * 32
    version = "d" * 32
    metadata = {
        "format_version": 2,
        "task": "multiclass",
        "class_names": ["Moli", "other"],
        "score_threshold": 0.6,
        "margin_threshold": 0.15,
    }
    snapshot = {
        **metadata,
        "input_size": 224,
        "character": "Bot自己",
    }
    snapshot["metadata_sha256"] = metadata_digest({**metadata, "input_size": 224})
    snapshot_sha = digest(snapshot)
    (store.root / "jobs" / job_id).mkdir(parents=True)
    (store.root / "jobs" / job_id / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    audit = {
        **metadata,
        "metadata_sha256": snapshot["metadata_sha256"],
        "snapshot_sha": snapshot_sha,
    }
    model_dir = store.root / "models" / version
    model_dir.mkdir(parents=True)
    audit_path = model_dir / "audit.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    metrics = {**metadata, "metadata_sha256": snapshot["metadata_sha256"], "input_size": 224}
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (job_id, job_id, "complete", time.time(), snapshot_sha),
        )
        db.execute(
            "INSERT INTO models (id,job_id,sha,metrics,created) VALUES (?,?,?,?,?)",
            (version, job_id, "e" * 64, json.dumps(metrics), time.time()),
        )
    model = SimpleNamespace(
        task="multiclass",
        format_version=2,
        class_names=["Moli", "other"],
        score_threshold=0.6,
        margin_threshold=0.15,
        metadata_sha256=snapshot["metadata_sha256"],
        input_size=224,
    )
    return store, version, model, snapshot, audit, audit_path


def test_worker_metadata_accepts_matching_schema2_catalog(tmp_path):
    store, version, model, snapshot, audit, _audit_path = _schema2_validation_fixture(tmp_path)

    result = _validate_model_metadata(store, version, model)

    assert result["task"] == "multiclass"
    assert result["class_names"] == ["Moli", "other"]
    assert snapshot["metadata_sha256"] == audit["metadata_sha256"] == model.metadata_sha256


def test_worker_metadata_rejects_independent_snapshot_input_size_change(tmp_path):
    store, version, model, snapshot, _audit, _audit_path = _schema2_validation_fixture(tmp_path)
    snapshot["input_size"] = 448
    (store.root / "jobs" / ("c" * 32) / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(ValueError, match="图片尺寸"):
        _validate_model_metadata(store, version, model)


def test_worker_metadata_rejects_database_input_size_mismatch(tmp_path):
    store, version, model, _snapshot, _audit, _audit_path = _schema2_validation_fixture(tmp_path)
    record = store.models()[0]
    metrics = json.loads(record["metrics"])
    metrics["input_size"] = 448
    with store.db() as db:
        db.execute("UPDATE models SET metrics=? WHERE id=?", (json.dumps(metrics), version))

    with pytest.raises(ValueError, match="图片尺寸"):
        _validate_model_metadata(store, version, model)


def test_worker_metadata_rejects_full_snapshot_digest_mismatch(tmp_path):
    store, version, model, snapshot, _audit, _audit_path = _schema2_validation_fixture(tmp_path)
    snapshot["character"] = "changed but outside model metadata"
    (store.root / "jobs" / ("c" * 32) / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(ValueError, match="快照SHA"):
        _validate_model_metadata(store, version, model)


def test_worker_metadata_recomputes_frozen_metadata_digest(tmp_path):
    store, version, model, snapshot, audit, audit_path = _schema2_validation_fixture(tmp_path)
    snapshot["metadata_sha256"] = "f" * 64
    snapshot_sha = digest(snapshot)
    (store.root / "jobs" / ("c" * 32) / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    with store.db() as db:
        db.execute("UPDATE jobs SET snapshot_sha=? WHERE id=?", (snapshot_sha, "c" * 32))
    audit["snapshot_sha"] = snapshot_sha
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    with pytest.raises(ValueError, match="元数据摘要"):
        _validate_model_metadata(store, version, model)


def test_worker_metadata_rejects_audit_snapshot_sha_mismatch(tmp_path):
    store, version, model, _snapshot, audit, audit_path = _schema2_validation_fixture(tmp_path)
    audit["snapshot_sha"] = "f" * 64
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    with pytest.raises(ValueError, match="快照SHA"):
        _validate_model_metadata(store, version, model)


def test_worker_metadata_keeps_legacy_binary_catalog_compatible(tmp_path):
    store = Store(tmp_path / "catalog")
    job_id = "1" * 32
    version = "2" * 32
    job_dir = store.root / "jobs" / job_id
    model_dir = store.root / "models" / version
    job_dir.mkdir(parents=True)
    model_dir.mkdir(parents=True)
    (job_dir / "snapshot.json").write_text(json.dumps({"character": "Bot自己"}), encoding="utf-8")
    (model_dir / "audit.json").write_text("{}", encoding="utf-8")
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (job_id, job_id, "complete", time.time(), "stale legacy snapshot hash is unused"),
        )
        db.execute(
            "INSERT INTO models (id,job_id,sha,metrics,created) VALUES (?,?,?,?,?)",
            (version, job_id, "3" * 64, "{}", time.time()),
        )
    model = SimpleNamespace(
        task="binary",
        format_version=1,
        class_names=["other", "self"],
        score_threshold=0.5,
        margin_threshold=0.0,
        metadata_sha256="",
        input_size=320,
    )

    result = _validate_model_metadata(store, version, model)

    assert result["task"] == "binary"


@pytest.mark.skipif(not WORKER_PYTHON, reason="set SELF_RECOGNITION_TEST_WORKER_PYTHON to a Torch worker interpreter")
def test_cpu_multiclass_lifecycle_replay_class_change_and_rollback(tmp_path):
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

    environment = os.environ.copy()
    python_paths = [str(CORE_ROOT)]
    overlay = environment.get("SELF_RECOGNITION_TEST_DEPS", "")
    if overlay:
        python_paths.insert(0, overlay)
    if environment.get("PYTHONPATH"):
        python_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    root = tmp_path / "catalog"
    source = tmp_path / "characters"
    base_classes = ["Moli", "NachoBot", "ShizukuBot", "other"]
    for class_index, label in enumerate(base_classes):
        for seed in range(20):
            _write_group(source / label, class_index * 100 + seed, label)
    imported = _cli(root, "import", "--source", str(source), environment=environment)
    assert imported["imported"] == 80
    assert imported["errors"] == 0

    initial_process = subprocess.Popen(
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
        first_line = initial_process.stdout.readline().strip()
        assert first_line, "multiclass training produced no initial progress output"
        job_id = json.loads(first_line)["job_id"]
        pause = _cli(root, "pause", job_id, environment=environment, timeout=240)
        assert pause["state"] == "paused"
        stdout, stderr = initial_process.communicate(timeout=180)
        assert initial_process.returncode == 0, f"{stdout}\n{stderr}"
        resumed = _cli(
            root,
            "--worker-python",
            str(worker),
            "train",
            "--resume",
            job_id,
            environment=environment,
            timeout=2400,
        )
        assert resumed["job"]["state"] == "complete"
    finally:
        if initial_process.poll() is None:
            initial_process.kill()
        try:
            initial_process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    from plugins.mai_recog_self.backend.model import loaded_model, multiclass_metadata

    first_report = _cli(root, "report", job_id, environment=environment)["report"]
    first_snapshot = json.loads((root / "jobs" / job_id / "snapshot.json").read_text("utf-8"))
    first_metadata = multiclass_metadata(first_snapshot)
    first_sha = metadata_digest(first_metadata)
    assert first_snapshot["metadata_sha256"] == first_sha
    assert first_report["metadata_sha256"] == first_sha
    assert first_report["audit"]["metadata_sha256"] == first_sha
    assert first_report["audit"]["passed"] is True
    assert loaded_model(root / "models" / job_id / "weights.pt").metadata_sha256 == first_sha
    audit_file = json.loads((root / "models" / job_id / "audit.json").read_text("utf-8"))
    assert audit_file["metadata_sha256"] == first_sha
    assert first_report["class_names"] == base_classes
    first_activation = _cli(root, "--worker-python", str(worker), "activate", job_id, environment=environment)
    assert first_activation["status"] == "activated"
    assert first_activation["validation"]["class_names"] == base_classes

    inference = tmp_path / "inference.png"
    inference.write_bytes(_png(9901, "Moli"))
    first_prediction = _cli(
        root,
        "--worker-python",
        str(worker),
        "test",
        str(inference),
        "--timeout",
        "180",
        environment=environment,
        timeout=240,
    )
    assert first_prediction["status"] == "ok"
    assert first_prediction["task"] == "multiclass"
    assert first_prediction["class_names"] == base_classes
    assert set(first_prediction["scores"]) == set(base_classes)
    assert sum(first_prediction["scores"].values()) == pytest.approx(1.0, abs=1e-5)
    assert first_prediction["margin"] == pytest.approx(
        sorted(first_prediction["scores"].values(), reverse=True)[0]
        - sorted(first_prediction["scores"].values(), reverse=True)[1],
        abs=1e-6,
    )

    for class_index, label in enumerate(base_classes):
        for seed in range(20, 22):
            _write_group(source / label, class_index * 100 + seed, label)
    added = _cli(root, "import", "--source", str(source), environment=environment)
    assert added["imported"] == len(base_classes) * 2
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
        environment=environment,
        timeout=2400,
    )
    incremental_id = incremental["job"]["id"]
    incremental_snapshot = json.loads((root / "jobs" / incremental_id / "snapshot.json").read_text("utf-8"))
    incremental_report = _cli(root, "report", incremental_id, environment=environment)["report"]
    expected_training_ids = {row["id"] for row in incremental_snapshot["images"] if row["split"] == "train"}
    assert incremental_snapshot["mode"] == "incremental"
    assert incremental_snapshot["training_policy"] == "full_replay"
    assert set(incremental_report["trained_image_ids"]) == expected_training_ids
    assert set(incremental_report["trained_image_ids"]) & set(first_report["trained_image_ids"])
    assert incremental_report["audit"]["passed"] is True
    assert _cli(root, "--worker-python", str(worker), "activate", incremental_id, environment=environment)["status"] == "activated"

    fourth = "Ami"
    for seed in range(20):
        _write_group(source / fourth, 500 + seed, fourth)
    added_class = _cli(root, "import", "--source", str(source), environment=environment)
    assert added_class["imported"] == 20
    class_change = _cli(
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
        environment=environment,
        timeout=2400,
    )
    changed_id = class_change["job"]["id"]
    changed_snapshot = json.loads((root / "jobs" / changed_id / "snapshot.json").read_text("utf-8"))
    changed_report = _cli(root, "report", changed_id, environment=environment)["report"]
    expected_changed_ids = {row["id"] for row in changed_snapshot["images"] if row["split"] == "train"}
    assert changed_snapshot["mode"] == "fresh"
    assert changed_snapshot["mode_reason"] == "class_set_changed"
    assert changed_snapshot["parent"] == ""
    assert changed_snapshot["initialization_source"] == "imagenet_pretrained"
    assert changed_snapshot["training_policy"] == "full_replay"
    assert set(changed_report["trained_image_ids"]) == expected_changed_ids
    assert changed_report["audit"]["passed"] is True
    changed_names = changed_snapshot["class_names"]
    assert changed_names == ["Ami", "Moli", "NachoBot", "ShizukuBot", "other"]

    assert _cli(root, "--worker-python", str(worker), "activate", changed_id, environment=environment)["status"] == "activated"
    rollback = _cli(root, "--worker-python", str(worker), "rollback", environment=environment)
    assert rollback["version"] == incremental_id
    assert rollback["validation"]["class_names"] == base_classes
    rolled_back_prediction = _cli(
        root,
        "--worker-python",
        str(worker),
        "test",
        str(inference),
        "--timeout",
        "180",
        environment=environment,
        timeout=240,
    )
    assert rolled_back_prediction["loaded_version"] == incremental_id
    assert rolled_back_prediction["class_names"] == base_classes
