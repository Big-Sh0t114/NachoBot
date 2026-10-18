"""Heavy training and inference worker. This module is launched in a child process."""

from pathlib import Path
import argparse
import base64
import hashlib
import json
import os
import sys
import time
import traceback

from .images import judgment
from .labels import metadata_digest, validate_multiclass_metadata
from .store import Store, atomic_json, digest


def _identity() -> dict:
    import psutil

    process = psutil.Process(os.getpid())
    return {"pid": process.pid, "created": process.create_time()}


def _version_artifact(store: Store, version: str) -> tuple[Path, str]:
    if not version or any(c not in "0123456789abcdef" for c in version) or len(version) != 32:
        raise ValueError("无效模型版本")
    record = next((row for row in store.models() if row["id"] == version), None)
    if record is None:
        raise ValueError("模型版本不存在")
    path = store.root / "models" / version / "weights.pt"
    audit_path = path.parent / "audit.json"
    audit = json.loads(audit_path.read_text("utf-8"))
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if not audit.get("passed") or actual != record["sha"] or audit.get("sha256") != actual:
        raise ValueError("模型哈希或审计核验失败")
    return path, actual


def _validate_model_metadata(store: Store, version: str, model) -> dict:
    record = next((row for row in store.models() if row["id"] == version), None)
    if record is None:
        raise ValueError("模型版本不存在")
    snapshot_path = store.root / "jobs" / record["job_id"] / "snapshot.json"
    audit_path = store.root / "models" / version / "audit.json"
    try:
        snapshot = json.loads(snapshot_path.read_text("utf-8"))
        audit = json.loads(audit_path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("模型任务快照或审计元数据无效") from exc
    if model.task == "multiclass":
        try:
            snapshot_metadata = validate_multiclass_metadata(
                {
                    "format_version": snapshot.get("format_version"),
                    "task": snapshot.get("task"),
                    "class_names": snapshot.get("class_names"),
                    "score_threshold": snapshot.get("score_threshold"),
                    "margin_threshold": snapshot.get("margin_threshold"),
                }
            )
        except ValueError as exc:
            raise ValueError("多类别冻结快照元数据无效") from exc
        snapshot_input_size = snapshot.get("input_size")
        model_input_size = getattr(model, "input_size", None)
        if any(type(size) is not int or size not in {224, 320, 448} for size in (snapshot_input_size, model_input_size)):
            raise ValueError("多类别模型图片尺寸元数据无效")
        try:
            record_metrics = json.loads(record["metrics"])
        except (TypeError, ValueError) as exc:
            raise ValueError("模型记录缺少有效指标") from exc
        metrics_input_size = record_metrics.get("input_size")
        if type(metrics_input_size) is not int or metrics_input_size not in {224, 320, 448}:
            raise ValueError("多类别模型记录图片尺寸无效")
        if model_input_size != snapshot_input_size or model_input_size != metrics_input_size:
            raise ValueError("多类别权重、快照和数据库记录的图片尺寸不一致")
        try:
            job = store.job(record["job_id"])
        except ValueError as exc:
            raise ValueError("多类别任务记录不存在") from exc
        snapshot_sha = digest(snapshot)
        if snapshot_sha != job["snapshot_sha"] or audit.get("snapshot_sha") != snapshot_sha:
            raise ValueError("多类别任务快照SHA与任务/审计记录不一致")
        expected_metadata_sha = metadata_digest({**snapshot_metadata, "input_size": snapshot_input_size})
        if snapshot.get("metadata_sha256") != expected_metadata_sha:
            raise ValueError("多类别冻结快照元数据摘要不匹配")
        if getattr(model, "metadata_sha256", None) != expected_metadata_sha:
            raise ValueError("多类别权重元数据摘要与冻结快照不一致")
        expected = {
            "task": "multiclass",
            "format_version": 2,
            "class_names": snapshot_metadata["class_names"],
            "score_threshold": model.score_threshold,
            "margin_threshold": model.margin_threshold,
            "metadata_sha256": expected_metadata_sha,
        }
        if getattr(model, "format_version", None) != expected["format_version"]:
            raise ValueError("多类别权重格式版本不符")
        if getattr(model, "class_names", None) != expected["class_names"]:
            raise ValueError("多类别权重类别映射与冻结快照不一致")
        for key, value in expected.items():
            if snapshot.get(key) != value or audit.get(key) != value:
                raise ValueError(f"模型类别元数据与快照/审计记录不符：{key}")
        for key, value in expected.items():
            if record_metrics.get(key) != value:
                raise ValueError(f"模型数据库记录与权重映射不符：{key}")
        if record_metrics.get("input_size") != model_input_size:
            raise ValueError("多类别模型记录图片尺寸与权重不一致")
    elif snapshot.get("task", "binary") != "binary" or audit.get("task", "binary") != "binary":
        raise ValueError("二分类权重与任务快照或审计记录类型不一致")
    return {
        "task": model.task,
        "format_version": model.format_version,
        "class_names": list(model.class_names),
        "score_threshold": model.score_threshold,
        "margin_threshold": model.margin_threshold,
        "metadata_sha256": model.metadata_sha256,
    }


def train_job(root: Path, job_id: str) -> None:
    store = Store(root, initialize=False)
    row = store.job(job_id)
    directory = store.root / "jobs" / job_id
    snapshot = json.loads((directory / "snapshot.json").read_text("utf-8"))
    if digest(snapshot) != row["snapshot_sha"]:
        raise ValueError("训练快照SHA不符")
    atomic_json(directory / "worker.json", {**_identity(), "started": time.time()})

    from .model import TrainingPaused, train

    def progress(value: dict) -> None:
        atomic_json(directory / "progress.json", value)

    def cancelled() -> bool:
        lease = directory / "lease"
        return (directory / "cancel").exists() or not lease.exists() or time.time() - lease.stat().st_mtime > 120

    try:
        with store.db() as db:
            db.execute(
                "UPDATE jobs SET state=CASE WHEN state='queued' THEN 'running' ELSE state END,pid=? WHERE id=?",
                (os.getpid(), job_id),
            )
        if cancelled():
            raise InterruptedError("训练租约已取消或过期")
        progress({"phase": "preparing"})
        report = train(
            store.root,
            snapshot,
            progress,
            cancelled,
            paused=lambda: (directory / "pause").exists(),
        )
        report["total_job_seconds"] = time.time() - snapshot["created"]
        if cancelled():
            raise InterruptedError("训练已取消")
        destination = store.root / "models" / job_id
        destination.mkdir(exist_ok=False)
        (directory / "selected.pt").replace(destination / "weights.pt")
        sha = hashlib.sha256((destination / "weights.pt").read_bytes()).hexdigest()
        atomic_json(directory / "report.json", report)
        audit = {**report["audit"], "sha256": sha, "snapshot_sha": digest(snapshot)}
        if report.get("task") == "multiclass":
            audit.update(
                {
                    key: report[key]
                    for key in ("task", "format_version", "class_names", "score_threshold", "margin_threshold", "metadata_sha256")
                }
            )
        atomic_json(destination / "audit.json", audit)
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO models (id,job_id,sha,metrics,created) VALUES (?,?,?,?,?)",
                (
                    job_id,
                    job_id,
                    sha,
                    json.dumps(
                        {
                            "validation": report["selection"]["validation"],
                            "test": report["test"],
                            "input_size": report["input_size"],
                            **(
                                {
                                    key: report[key]
                                    for key in ("task", "format_version", "class_names", "score_threshold", "margin_threshold", "metadata_sha256")
                                }
                                if report.get("task") == "multiclass"
                                else {}
                            ),
                        }
                    ),
                    time.time(),
                ),
            )
            db.execute("UPDATE jobs SET state='complete',pid=NULL,error='' WHERE id=?", (job_id,))
            db.execute("INSERT OR REPLACE INTO settings VALUES ('last_held_truth',?)", (snapshot["held_truth"],))
        progress({"phase": "complete", "selected_epoch": report["selection"]["epoch"], "seconds": report["total_seconds"]})
        (directory / "resume.pt").unlink(missing_ok=True)
        (directory / "resume-audit.json").unlink(missing_ok=True)
    except TrainingPaused:
        with store.db() as db:
            db.execute("UPDATE jobs SET state='paused',pid=NULL,error='' WHERE id=?", (job_id,))
        value = json.loads((directory / "progress.json").read_text("utf-8"))
        progress({**value, "phase": "paused"})
    except BaseException as exc:
        state = "cancelled" if isinstance(exc, InterruptedError) else "failed"
        with store.db() as db:
            db.execute("UPDATE jobs SET state=?,pid=NULL,error=? WHERE id=?", (state, str(exc), job_id))
        progress({"phase": state, "error": str(exc)})
        traceback.print_exc(file=sys.stderr)
        raise
    finally:
        (directory / "lease").unlink(missing_ok=True)


def serve(root: Path) -> None:
    import torch

    from .model import infer, infer_scores, loaded_model, multiclass_decision

    torch.set_num_threads(min(4, torch.get_num_threads()))
    store = Store(root, initialize=False, readonly=True)
    current = ""
    model = None
    initial_version = store.setting("active")
    if initial_version:
        try:
            path, _ = _version_artifact(store, initial_version)
            model = loaded_model(path)
            _validate_model_metadata(store, initial_version, model)
            current = initial_version
            print(
                json.dumps(
                    {"status": "ready", "version": current, "desired_version": initial_version, "loaded_version": current},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "status": "model_error",
                        "error": str(exc),
                        "desired_version": initial_version,
                        "loaded_version": "",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    else:
        print(json.dumps({"status": "no_model", "desired_version": "", "loaded_version": ""}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            version = request.get("version") or store.setting("active")
            if not version:
                result = {"status": "no_model", "desired_version": "", "loaded_version": current}
            else:
                if version != current:
                    path, _ = _version_artifact(store, version)
                    candidate = loaded_model(path)  # retain the old model if a candidate fails to load
                    _validate_model_metadata(store, version, candidate)
                    model = candidate
                    current = version
                if request.get("operation") == "load":
                    result = {
                        "status": "ready",
                        "version": current,
                        "input_size": model.input_size,
                        "desired_version": version,
                        "loaded_version": current,
                    }
                else:
                    raw = base64.b64decode(request["image"], validate=True)
                    if model.task == "multiclass":
                        scores = infer_scores(model, raw)
                        decision = multiclass_decision(
                            scores,
                            model.class_names,
                            model.score_threshold,
                            model.margin_threshold,
                        )
                        result = {
                            "status": "ok",
                            "task": "multiclass",
                            "format_version": 2,
                            "class_names": list(model.class_names),
                            "scores": {name: float(score) for name, score in zip(model.class_names, scores, strict=True)},
                            **decision,
                            "score_is_calibrated_probability": False,
                            "version": current,
                            "input_size": model.input_size,
                            "desired_version": version,
                            "loaded_version": current,
                        }
                    else:
                        score = infer(model, raw)
                        result = {
                            "status": "ok",
                            "character": store.setting("character", "Bot自己"),
                            "version": current,
                            "input_size": model.input_size,
                            "score": score,
                            "judgment": judgment(score),
                            "score_is_calibrated_probability": False,
                            "desired_version": version,
                            "loaded_version": current,
                        }
        except Exception as exc:
            result = {
                "status": "model_error",
                "error": str(exc),
                "desired_version": locals().get("version", ""),
                "loaded_version": current,
            }
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)


def validate(root: Path, version: str) -> dict:
    import torch

    from .model import loaded_model

    torch.set_num_threads(min(4, torch.get_num_threads()))
    store = Store(root, initialize=False, readonly=True)
    path, sha = _version_artifact(store, version)
    model = loaded_model(path)
    metadata = _validate_model_metadata(store, version, model)
    return {"status": "ready", "version": version, "sha256": sha, "input_size": model.input_size, **metadata}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["train", "serve", "validate"])
    parser.add_argument("root", type=Path)
    parser.add_argument("argument", nargs="?")
    args = parser.parse_args()
    if args.operation == "train":
        if not args.argument:
            parser.error("train requires a job id")
        train_job(args.root, args.argument)
    elif args.operation == "validate":
        if not args.argument:
            parser.error("validate requires a model version")
        print(json.dumps(validate(args.root, args.argument), ensure_ascii=False), flush=True)
    else:
        serve(args.root)


if __name__ == "__main__":
    main()
