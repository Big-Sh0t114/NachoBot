"""Small whole-image classifier. Heavy imports are worker-only."""

from pathlib import Path
import hashlib
import json
import random
import time
import psutil

import numpy as np
import torch
from torch import nn
from torchvision import models, transforms

from .images import decode, whole_image
from .labels import metadata_digest, ordered_class_names, validate_multiclass_metadata


class TrainingPaused(Exception):
    """A verified checkpoint has been saved; release the worker process."""


BASE_SHA = "047dcff4addef86ea5bc2eff13c9614dc11f47ab1160d0a71a25e7db994f4e1f"
BASE_FILE = Path(__file__).resolve().parent / "assets" / "mobilenet_v3_small-047dcff4.pth"
NORMALIZE = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])


def metrics(labels: list, scores: list) -> dict:
    predicted = [int(s >= 0.5) for s in scores]
    tp = sum(y == 1 and p == 1 for y, p in zip(labels, predicted, strict=True))
    fp = sum(y == 0 and p == 1 for y, p in zip(labels, predicted, strict=True))
    fn = sum(y == 1 and p == 0 for y, p in zip(labels, predicted, strict=True))
    tn = sum(y == 0 and p == 0 for y, p in zip(labels, predicted, strict=True))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "precision": precision,
        "precision_numerator": tp,
        "precision_denominator": tp + fp,
        "recall": recall,
        "other_recall": tn / (tn + fp) if tn + fp else 0.0,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
        "accuracy": (tp + tn) / len(labels) if labels else 0.0,
        "balanced_accuracy": (recall + (tn / (tn + fp) if tn + fp else 0.0)) / 2,
        "confusion_matrix": [[tn, fp], [fn, tp]],
        "count": len(labels),
    }


def seed_all() -> None:
    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(42)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False


def fresh_model(num_classes: int = 2) -> nn.Module:
    if not BASE_FILE.exists() or hashlib.sha256(BASE_FILE.read_bytes()).hexdigest() != BASE_SHA:
        raise ValueError("ImageNet底座缺失或SHA256校验失败")
    model = models.mobilenet_v3_small(weights=None)
    model.load_state_dict(torch.load(BASE_FILE, map_location="cpu", weights_only=True))
    if type(num_classes) is not int or num_classes < 2:
        raise ValueError("分类类别数无效")
    model.classifier[3] = nn.Linear(model.classifier[3].in_features, num_classes)
    model.input_size = 320
    return model


def loaded_model(path: Path) -> nn.Module:
    artifact = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(artifact, dict):
        raise ValueError("模型文件格式无效")
    if "state_dict" in artifact:
        state = artifact.get("state_dict")
        input_size = artifact.get("input_size")
        if type(input_size) is not int or input_size not in {224, 320, 448}:
            raise ValueError("模型图片尺寸无效")
        version = artifact.get("format_version")
        if type(version) is int and version == 2:
            allowed = {
                "state_dict", "input_size", "format_version", "task", "class_names",
                "score_threshold", "margin_threshold", "metadata_sha256",
            }
            if set(artifact) != allowed:
                raise ValueError("schema-2模型包含缺失或未知字段")
            metadata = validate_multiclass_metadata(
                {key: artifact[key] for key in ("format_version", "task", "class_names", "score_threshold", "margin_threshold")}
            )
            expected_metadata_sha = metadata_digest({**metadata, "input_size": input_size})
            if artifact.get("metadata_sha256") != expected_metadata_sha:
                raise ValueError("模型类别元数据SHA256校验失败")
            class_names = metadata["class_names"]
            task = "multiclass"
            score_threshold = metadata["score_threshold"]
            margin_threshold = metadata["margin_threshold"]
            artifact_metadata_sha = expected_metadata_sha
        elif version is None or type(version) is int and version == 1:
            if type(version) is int and version == 1 and any(key in artifact for key in ("task", "class_names", "score_threshold", "margin_threshold")):
                raise ValueError("schema-1模型不得声明未支持的类别元数据")
            if set(artifact) - {"state_dict", "input_size", "format_version"}:
                raise ValueError("legacy模型包含未知字段")
            class_names = ["other", "self"]
            task = "binary"
            score_threshold, margin_threshold = 0.5, 0.0
            artifact_metadata_sha = ""
        else:
            raise ValueError(f"不支持的模型格式版本：{version}")
    else:
        state, input_size = artifact, 320
        class_names = ["other", "self"]
        task = "binary"
        score_threshold, margin_threshold = 0.5, 0.0
        artifact_metadata_sha = ""
    if not isinstance(state, dict) or not state or any(not torch.is_tensor(v) for v in state.values()):
        raise ValueError("模型权重格式无效")
    if state.get("classifier.3.weight") is None or state["classifier.3.weight"].shape[0] != len(class_names):
        raise ValueError("模型分类头类别数与类别映射不符")
    model = models.mobilenet_v3_small(weights=None)
    model.classifier[3] = nn.Linear(model.classifier[3].in_features, len(class_names))
    model.input_size = input_size
    model.task = task
    model.format_version = 2 if task == "multiclass" else 1
    model.class_names = list(class_names)
    model.score_threshold = score_threshold
    model.margin_threshold = margin_threshold
    model.metadata_sha256 = artifact_metadata_sha
    if not state or any(not torch.isfinite(v).all() for v in state.values()):
        raise ValueError("模型包含非有限权重")
    model.load_state_dict(state, strict=True)
    return model.eval()


class Dataset(torch.utils.data.Dataset):
    def __init__(
        self,
        root: Path,
        rows: list,
        augment: bool = False,
        input_size: int = 320,
        resize_first: bool = True,
        class_names: list[str] | None = None,
    ):
        self.root = root
        self.rows = rows
        self.augment = augment
        self.input_size = input_size
        self.resize_first = resize_first
        self.class_to_index = {name: index for index, name in enumerate(class_names)} if class_names else None
        self.jitter = transforms.ColorJitter(0.1, 0.1, 0.08, 0.02)

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        row = self.rows[index]
        image = decode((self.root / row["path"]).read_bytes())
        # Legacy paused tasks retain their original augmentation order.
        if self.resize_first:
            image = whole_image(image, self.input_size)
        if self.augment:
            image = self.jitter(image)
        if not self.resize_first:
            image = whole_image(image, self.input_size)
        label = self.class_to_index[row["label"]] if self.class_to_index is not None else int(row["label"] == "self")
        return NORMALIZE(transforms.ToTensor()(image)), label


def evaluate(model: nn.Module, root: Path, rows: list, device: str, batch: int, cancelled=None) -> tuple:
    model.eval()
    labels = []
    scores = []
    loader = torch.utils.data.DataLoader(Dataset(root, rows, input_size=model.input_size), batch_size=batch, shuffle=False, num_workers=0)
    with torch.inference_mode():
        for inputs, truth in loader:
            if cancelled and cancelled():
                raise InterruptedError("training cancelled")
            out = model(inputs.to(device)).float()
            if not torch.isfinite(out).all():
                raise ValueError("推理输出非有限")
            scores.extend(out.softmax(-1)[:, 1].cpu().tolist())
            labels.extend(truth.tolist())
    return metrics(labels, scores), [
        {"id": r["id"], "label": r["label"], "score": s, "predicted": "self" if s >= 0.5 else "other"}
        for r, s in zip(rows, scores, strict=True)
    ]


def multiclass_decision(scores: list[float], class_names: list[str], score_threshold: float, margin_threshold: float) -> dict:
    if len(scores) != len(class_names) or len(scores) < 2 or any(not np.isfinite(value) for value in scores):
        raise ValueError("多类别分数向量无效")
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    top, second = order[0], order[1]
    top_score = float(scores[top])
    margin = float(top_score - scores[second])
    predicted = class_names[top]
    accepted = top_score >= score_threshold and margin >= margin_threshold
    character_id = predicted if accepted else None
    return {
        "score": top_score,
        "margin": margin,
        "predicted_class": predicted,
        "character_id": character_id,
        "recognized": bool(accepted and predicted != "other"),
        "is_other": bool(accepted and predicted == "other"),
        "judgment": character_id if character_id is not None else "不确定",
    }


def multiclass_metrics(labels: list[int | str], score_vectors: list[list[float]], class_names: list[str]) -> dict:
    if len(labels) != len(score_vectors):
        raise ValueError("多类别标签与分数数量不符")
    if ordered_class_names(class_names, require_other=True) != class_names:
        raise ValueError("多类别指标类别顺序无效")
    for vector in score_vectors:
        if (
            len(vector) != len(class_names)
            or any(not np.isfinite(value) or not 0 <= value <= 1 for value in vector)
            or not np.isclose(sum(vector), 1.0, atol=1e-5)
        ):
            raise ValueError("多类别分数向量必须是有限且归一化的概率")
    label_to_index = {name: index for index, name in enumerate(class_names)}
    truth = [label_to_index[value] if isinstance(value, str) else value for value in labels]
    if any(type(value) is not int or not 0 <= value < len(class_names) for value in truth):
        raise ValueError("多类别真值映射无效")
    predicted = [max(range(len(vector)), key=lambda index: (vector[index], -index)) for vector in score_vectors]
    confusion = [[0 for _ in class_names] for _ in class_names]
    for actual, guess in zip(truth, predicted, strict=True):
        confusion[actual][guess] += 1
    per_class = {}
    recalls = []
    f1s = []
    for index, name in enumerate(class_names):
        tp = confusion[index][index]
        support = sum(confusion[index])
        predicted_count = sum(row[index] for row in confusion)
        precision = tp / predicted_count if predicted_count else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[name] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
            "predicted": predicted_count,
        }
        recalls.append(recall)
        f1s.append(f1)
    count = len(truth)
    correct = sum(confusion[index][index] for index in range(len(class_names)))
    return {
        "class_names": list(class_names),
        "per_class": per_class,
        "macro_f1": sum(f1s) / len(f1s) if f1s else 0.0,
        "balanced_accuracy": sum(recalls) / len(recalls) if recalls else 0.0,
        "accuracy": correct / count if count else 0.0,
        "confusion_matrix": confusion,
        "count": count,
    }


def evaluate_multiclass(
    model: nn.Module,
    root: Path,
    rows: list,
    device: str,
    batch: int,
    class_names: list[str],
    score_threshold: float,
    margin_threshold: float,
    cancelled=None,
) -> tuple[dict, list[dict]]:
    model.eval()
    labels: list[int] = []
    score_vectors: list[list[float]] = []
    loader = torch.utils.data.DataLoader(
        Dataset(root, rows, input_size=model.input_size, class_names=class_names),
        batch_size=batch,
        shuffle=False,
        num_workers=0,
    )
    with torch.inference_mode():
        for inputs, truth in loader:
            if cancelled and cancelled():
                raise InterruptedError("training cancelled")
            output = model(inputs.to(device)).float()
            if not torch.isfinite(output).all():
                raise ValueError("多类别推理输出非有限")
            score_vectors.extend(output.softmax(-1).cpu().tolist())
            labels.extend(truth.tolist())
    metrics_value = multiclass_metrics(labels, score_vectors, class_names)
    predictions = []
    for row, vector in zip(rows, score_vectors, strict=True):
        decision = multiclass_decision(vector, class_names, score_threshold, margin_threshold)
        predictions.append(
            {
                "id": row["id"],
                "label": row["label"],
                "scores": {name: float(value) for name, value in zip(class_names, vector, strict=True)},
                **decision,
                # Keep the report field consumed by the existing catalog gallery.
                "predicted": decision["predicted_class"],
                "recognized": decision["recognized"],
            }
        )
    return metrics_value, predictions


def multiclass_metadata(snapshot: dict) -> dict:
    metadata = validate_multiclass_metadata(
        {
            "format_version": snapshot.get("format_version"),
            "task": snapshot.get("task"),
            "class_names": snapshot.get("class_names"),
            "score_threshold": snapshot.get("score_threshold"),
            "margin_threshold": snapshot.get("margin_threshold"),
        }
    )
    input_size = snapshot.get("input_size")
    if type(input_size) is not int or input_size not in {224, 320, 448}:
        raise ValueError("多类别快照图片尺寸无效")
    complete = {**metadata, "input_size": input_size}
    if snapshot.get("metadata_sha256") != metadata_digest(complete):
        raise ValueError("多类别快照元数据摘要不匹配")
    return complete


def multiclass_artifact(state: dict, metadata: dict) -> dict:
    return {
        "state_dict": state,
        "input_size": metadata["input_size"],
        "format_version": metadata["format_version"],
        "task": metadata["task"],
        "class_names": metadata["class_names"],
        "score_threshold": metadata["score_threshold"],
        "margin_threshold": metadata["margin_threshold"],
        "metadata_sha256": metadata_digest(metadata),
    }


def train(root: Path, snapshot: dict, progress, cancelled, max_epochs: int | None = None, paused=None) -> dict:
    task = snapshot.get("task", "binary")
    if task == "multiclass":
        return train_multiclass(root, snapshot, progress, cancelled, max_epochs=max_epochs, paused=paused)
    if task != "binary":
        raise ValueError(f"不支持的训练任务类型：{task}")
    started = time.monotonic()
    device = snapshot["device"]
    input_size = snapshot.get("input_size", 320)
    if type(input_size) is not int or input_size not in {224, 320, 448}:
        raise ValueError("图片尺寸无效")
    torch.set_num_threads(min(4, torch.get_num_threads()))
    if device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA不可用，请选择CPU")
    seed_all()
    batch = 32 if device == "cuda" else 8
    parent = snapshot["parent"]
    if (
        parent
        and hashlib.sha256((root / "models" / parent / "weights.pt").read_bytes()).hexdigest() != snapshot["parent_sha"]
    ):
        raise ValueError("启用父模型SHA变化，拒绝增量训练")
    model = loaded_model(root / "models" / parent / "weights.pt") if parent else fresh_model()
    model.input_size = input_size
    model = model.to(device)
    initial = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
    for parameter in model.parameters():
        parameter.requires_grad = False
    for parameter in model.classifier.parameters():
        parameter.requires_grad = True
    rows = snapshot["images"]
    if any(hashlib.sha256((root / r["path"]).read_bytes()).hexdigest() != r["sha"] for r in rows):
        raise ValueError("冻结原图哈希变化")
    partitions = {s: [r for r in rows if r["split"] == s] for s in ("train", "val", "test")}
    targets = [sum(r["label"] == label for r in partitions["train"]) for label in ("other", "self")]
    loss_fn = nn.CrossEntropyLoss(weight=torch.tensor([sum(targets) / (2 * n) if n else 0.0 for n in targets], device=device))
    incremental = snapshot["mode"] == "incremental"
    epochs = max_epochs or (10 if incremental else 15)
    job_dir = root / "jobs" / snapshot["job_id"]
    generator = torch.Generator().manual_seed(42)
    history, coverage = [], []
    best, optimizer = None, None
    peak_rss = 0
    from .store import atomic_json, digest

    checkpoint = job_dir / "resume.pt"
    resume = None
    elapsed = 0.0
    if checkpoint.exists():
        metadata = __import__("json").loads((job_dir / "resume-audit.json").read_text("utf-8"))
        if (
            metadata["snapshot_sha"] != digest(snapshot)
            or metadata["sha"] != hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        ):
            raise ValueError("checkpoint integrity mismatch")
        # Only this worker writes checkpoints; user uploads never become pickle inputs.
        resume = torch.load(checkpoint, map_location="cpu", weights_only=False)
        model.load_state_dict(resume["model"])
        initial, history, coverage, best = resume["initial"], resume["history"], resume["coverage"], resume["best"]
        generator.set_state(resume["generator"])
        elapsed, peak_rss = resume["seconds"], resume["peak_rss"]
    first_epoch = resume["epoch"] + int(resume.get("finished", False)) if resume else 1
    for epoch in range(first_epoch, epochs + 1):
        if cancelled():
            raise InterruptedError("training cancelled")
        tune = incremental or epoch > 3
        if optimizer is None or (epoch == 4 and not incremental):
            visual = []
            if tune:
                for module in list(model.features)[-4:]:
                    for parameter in module.parameters():
                        parameter.requires_grad = True
                        visual.append(parameter)
            groups = [
                {"params": model.classifier.parameters(), "lr": 1e-4 if incremental else (3e-4 if tune else 1e-3)}
            ]
            if visual:
                groups.append({"params": visual, "lr": 3e-5 if incremental else 1e-4})
            optimizer = torch.optim.AdamW(groups, weight_decay=1e-4)
        if resume:
            if not (resume.get("finished") and epoch == 4 and not incremental):
                optimizer.load_state_dict(resume["optimizer"])
            for state in optimizer.state.values():
                for key, value in state.items():
                    if isinstance(value, torch.Tensor):
                        state[key] = value.to(device)
            random.setstate(resume["random"])
            np.random.set_state(resume["numpy"])
            torch.set_rng_state(resume["torch"])
            if device == "cuda":
                torch.cuda.set_rng_state_all(resume["cuda"])
        model.train()
        for module in list(model.features)[:-4] if tune else list(model.features):
            module.eval()
        mid_epoch = resume and not resume.get("finished", False)
        order = resume["order"] if mid_epoch else torch.randperm(len(partitions["train"]), generator=generator).tolist()
        offset = resume["step"] if mid_epoch else 0
        total_loss, seen = (resume["loss"], resume["seen"]) if mid_epoch else (0.0, 0)
        steps = (len(order) + batch - 1) // batch

        def save_pause(next_step, finished=False):
            state = {
                "model": {k: v.detach().cpu() for k, v in model.state_dict().items()},
                "optimizer": optimizer.state_dict(),  # noqa: B023 - capture the exact current batch boundary
                "initial": initial,
                "history": history,
                "coverage": coverage,
                "best": best,  # noqa: B023 - capture the exact current batch boundary
                "generator": generator.get_state(),
                "epoch": epoch,  # noqa: B023 - capture the exact current batch boundary
                "finished": finished,
                "step": next_step,
                "order": order,  # noqa: B023 - capture the exact current batch boundary
                "loss": total_loss,  # noqa: B023 - capture the exact current batch boundary
                "seen": seen,  # noqa: B023 - capture the exact current batch boundary
                "random": random.getstate(),
                "numpy": np.random.get_state(),
                "torch": torch.get_rng_state(),
                "cuda": torch.cuda.get_rng_state_all() if device == "cuda" else [],
                "seconds": elapsed + time.monotonic() - started,
                "peak_rss": peak_rss,  # noqa: B023 - capture the exact current batch boundary
            }
            temporary = checkpoint.with_suffix(".tmp")
            torch.save(state, temporary)
            temporary.replace(checkpoint)
            atomic_json(
                job_dir / "resume-audit.json",
                {"snapshot_sha": digest(snapshot), "sha": hashlib.sha256(checkpoint.read_bytes()).hexdigest()},
            )
            raise TrainingPaused()

        # Explicit batch indices preserve ordering and augmentation RNG across restarts.
        dataset = Dataset(root, partitions["train"], True, input_size, snapshot.get("preprocessing") == "resize_before_augment")
        resume = None
        for step in range(offset, steps):
            if cancelled():
                raise InterruptedError("training cancelled")
            if paused and paused():
                save_pause(step)
            examples = [dataset[i] for i in order[step * batch : (step + 1) * batch]]
            inputs = torch.stack([example[0] for example in examples])
            truth = torch.tensor([example[1] for example in examples])
            optimizer.zero_grad(set_to_none=True)
            output = model(inputs.to(device))
            loss = loss_fn(output, truth.to(device))
            if not torch.isfinite(loss):
                raise ValueError("non-finite training loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step()
            seen += len(truth)
            total_loss += loss.item() * len(truth)
            memory = psutil.Process().memory_info()._asdict()
            peak_rss = max(peak_rss, memory["rss"], memory.get("peak_wset", 0))
            progress(
                {
                    "phase": "training",
                    "epoch": epoch,
                    "epochs": epochs,
                    "step": step + 1,
                    "steps": steps,
                    "seen": seen,
                    "total_images": len(order),
                    "seconds": elapsed + time.monotonic() - started,
                }
            )
            if paused and paused():
                save_pause(step + 1)
        coverage.append(seen)
        progress(
            {
                "phase": "validation",
                "epoch": epoch,
                "epochs": epochs,
                "seen": seen,
                "total_images": len(order),
                "step": steps,
                "steps": steps,
                "seconds": elapsed + time.monotonic() - started,
            }
        )
        val, predictions = evaluate(model, root, partitions["val"], device, batch, cancelled)
        record = {"epoch": epoch, "loss": total_loss / seen, "validation": val}
        history.append(record)
        rank = (val["f1"], val["balanced_accuracy"], val["precision"], -epoch)
        if best is None or rank > best[0]:
            best = (rank, record, predictions)
            torch.save({"state_dict": {k: v.detach().cpu() for k, v in model.state_dict().items()}, "input_size": input_size}, job_dir / "selected.pt")
        if paused and paused():
            save_pause(steps, finished=True)
    selection = {
        "epoch": best[1]["epoch"],
        "validation": best[1]["validation"],
        "rule": "F1,balanced_accuracy,precision,earlier_epoch",
        "threshold": 0.5,
    }
    atomic_json(job_dir / "selection.json", selection)
    selection_sha = hashlib.sha256((job_dir / "selection.json").read_bytes()).hexdigest()
    training_seconds = elapsed + time.monotonic() - started
    if cancelled():
        raise InterruptedError("训练已取消")
    progress(
        {
            "phase": "test",
            "selected_epoch": selection["epoch"],
            "epoch": epochs,
            "epochs": epochs,
            "seconds": training_seconds,
        }
    )
    selected = loaded_model(job_dir / "selected.pt").to(device)
    test, test_predictions = evaluate(selected, root, partitions["test"], device, batch, cancelled)
    state = selected.state_dict()
    head_changed = any(
        not torch.equal(v.cpu(), initial[k])
        for k, v in state.items()
        if k.startswith("classifier.") and v.is_floating_point()
    )
    # Selection can legitimately choose a warm-up epoch with frozen vision.
    changed = any(not torch.equal(v.cpu(), initial[k]) for k, v in state.items() if v.is_floating_point())
    finite = all(torch.isfinite(v).all().item() for v in state.values())
    group_sets = [{r["group_id"] for r in partitions[s]} for s in partitions]
    isolation = all(not group_sets[i] & group_sets[j] for i in range(3) for j in range(i))
    for key in ("id", "sha", "decoded"):
        sets = [{r[key] for r in partitions[s]} for s in partitions]
        isolation = isolation and all(not sets[i] & sets[j] for i in range(3) for j in range(i))
    hashes = all(hashlib.sha256((root / r["path"]).read_bytes()).hexdigest() == r["sha"] for r in rows)
    recomputed = (
        metrics([int(r["label"] == "self") for r in test_predictions], [r["score"] for r in test_predictions]) == test
    )
    recomputed = (
        recomputed
        and metrics([int(r["label"] == "self") for r in best[2]], [r["score"] for r in best[2]])
        == best[1]["validation"]
    )
    # Independently reload the saved artifact on CPU and compare probabilities.
    reloaded = loaded_model(job_dir / "selected.pt")
    reload_val, reload_pred = evaluate(reloaded, root, partitions["val"], "cpu", 8)
    reload_close = all(abs(a["score"] - b["score"]) < 1e-3 for a, b in zip(best[2], reload_pred, strict=True))
    selection_unchanged = hashlib.sha256((job_dir / "selection.json").read_bytes()).hexdigest() == selection_sha
    audit = {
        "finite": finite,
        "changed": changed,
        "head_changed": head_changed,
        "isolation": isolation,
        "image_hashes": hashes,
        "metrics_recomputed": recomputed,
        "reload_close": reload_close,
        "selection_unchanged": selection_unchanged,
        "training_coverage": all(n == len(partitions["train"]) for n in coverage),
    }
    audit["passed"] = all(audit.values())
    if not audit["passed"]:
        raise ValueError(f"模型核验失败: {audit}")
    return {
        "selection": selection,
        "selection_sha256_before_test": selection_sha,
        "history": history,
        "validation_predictions": best[2],
        "test": test,
        "test_predictions": test_predictions,
        "audit": audit,
        "training_seconds_including_validation": training_seconds,
        "total_seconds": elapsed + time.monotonic() - started,
        "peak_rss_bytes": peak_rss,
        "peak_cuda_bytes": torch.cuda.max_memory_allocated() if device == "cuda" else 0,
        "historical_test": snapshot["historical_test"],
        "comparable_to_previous": snapshot["comparable_to_previous"],
        "counts": {s: len(r) for s, r in partitions.items()},
        "snapshot_sha": digest(snapshot),
        "device": device,
        "input_size": input_size,
        "trained_image_ids": [r["id"] for r in partitions["train"]],
    }


def train_multiclass(root: Path, snapshot: dict, progress, cancelled, max_epochs: int | None = None, paused=None) -> dict:
    started = time.monotonic()
    metadata = multiclass_metadata(snapshot)
    class_names = metadata["class_names"]
    input_size = metadata["input_size"]
    if snapshot.get("training_policy") != "full_replay":
        raise ValueError("schema-2任务必须使用完整训练集回放")
    if snapshot.get("mode") not in {"fresh", "incremental"}:
        raise ValueError("多类别训练模式无效")
    parent = snapshot.get("parent", "")
    if snapshot["mode"] == "incremental" and not parent:
        raise ValueError("多类别增量训练缺少兼容父模型")
    if snapshot["mode"] == "fresh" and parent:
        raise ValueError("多类别从头训练不得继承父模型")
    if snapshot.get("mode_reason") == "class_set_changed":
        if snapshot["mode"] != "fresh" or parent or snapshot.get("initialization_source") != "imagenet_pretrained":
            raise ValueError("类别集变更必须从ImageNet底座重新初始化")
    expected_initialization = "parent_model" if parent else "imagenet_pretrained"
    if snapshot.get("initialization_source") != expected_initialization:
        raise ValueError("多类别初始化来源与冻结训练模式不符")

    device = snapshot["device"]
    torch.set_num_threads(min(4, torch.get_num_threads()))
    if device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA不可用，请选择CPU")
    seed_all()
    batch = 32 if device == "cuda" else 8
    if parent and hashlib.sha256((root / "models" / parent / "weights.pt").read_bytes()).hexdigest() != snapshot["parent_sha"]:
        raise ValueError("启用父模型SHA变化，拒绝增量训练")
    if parent:
        model = loaded_model(root / "models" / parent / "weights.pt")
        if model.task != "multiclass" or model.class_names != class_names:
            raise ValueError("父模型类别映射与冻结训练快照不一致")
        if model.score_threshold != metadata["score_threshold"] or model.margin_threshold != metadata["margin_threshold"]:
            raise ValueError("父模型拒识阈值与冻结训练快照不一致")
    else:
        model = fresh_model(len(class_names))
    model.input_size = input_size
    model = model.to(device)
    initial = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
    for parameter in model.parameters():
        parameter.requires_grad = False
    for parameter in model.classifier.parameters():
        parameter.requires_grad = True
    rows = snapshot["images"]
    class_set = set(class_names)
    if any(row["label"] not in class_set or row["split"] not in {"train", "val", "test"} for row in rows):
        raise ValueError("冻结快照包含未知类别或无效划分")
    if any(hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() != row["sha"] for row in rows):
        raise ValueError("冻结原图哈希变化")
    partitions = {split: [row for row in rows if row["split"] == split] for split in ("train", "val", "test")}
    targets = [sum(row["label"] == label for row in partitions["train"]) for label in class_names]
    if any(count == 0 for count in targets):
        raise ValueError("每个类别的训练划分都必须包含样本")
    loss_fn = nn.CrossEntropyLoss(
        weight=torch.tensor([sum(targets) / (len(targets) * count) for count in targets], device=device)
    )
    incremental = snapshot["mode"] == "incremental"
    epochs = max_epochs or (10 if incremental else 15)
    job_dir = root / "jobs" / snapshot["job_id"]
    generator = torch.Generator().manual_seed(42)
    history, coverage = [], []
    best, optimizer = None, None
    peak_rss = 0
    from .store import atomic_json, digest

    checkpoint = job_dir / "resume.pt"
    resume = None
    elapsed = 0.0
    if checkpoint.exists():
        audit_path = job_dir / "resume-audit.json"
        if not audit_path.is_file():
            raise ValueError("恢复断点缺少完整性元数据")
        checkpoint_audit = json.loads(audit_path.read_text("utf-8"))
        if (
            checkpoint_audit.get("snapshot_sha") != digest(snapshot)
            or checkpoint_audit.get("sha") != hashlib.sha256(checkpoint.read_bytes()).hexdigest()
            or checkpoint_audit.get("task") != "multiclass"
            or checkpoint_audit.get("class_names") != class_names
            or checkpoint_audit.get("metadata_sha256") != metadata_digest(metadata)
        ):
            raise ValueError("多类别恢复断点映射或SHA核验失败")
        resume = torch.load(checkpoint, map_location="cpu", weights_only=False)
        if (
            resume.get("task") != "multiclass"
            or resume.get("format_version") != 2
            or resume.get("class_names") != class_names
            or resume.get("metadata_sha256") != metadata_digest(metadata)
            or resume.get("snapshot_sha") != digest(snapshot)
        ):
            raise ValueError("多类别恢复断点语义与冻结快照不一致")
        model.load_state_dict(resume["model"])
        initial, history, coverage, best = resume["initial"], resume["history"], resume["coverage"], resume["best"]
        generator.set_state(resume["generator"])
        elapsed, peak_rss = resume["seconds"], resume["peak_rss"]
    first_epoch = resume["epoch"] + int(resume.get("finished", False)) if resume else 1
    for epoch in range(first_epoch, epochs + 1):
        if cancelled():
            raise InterruptedError("training cancelled")
        tune = incremental or epoch > 3
        if optimizer is None or epoch == 4 and not incremental:
            visual = []
            if tune:
                for module in list(model.features)[-4:]:
                    for parameter in module.parameters():
                        parameter.requires_grad = True
                        visual.append(parameter)
            groups = [
                {"params": model.classifier.parameters(), "lr": 1e-4 if incremental else (3e-4 if tune else 1e-3)}
            ]
            if visual:
                groups.append({"params": visual, "lr": 3e-5 if incremental else 1e-4})
            optimizer = torch.optim.AdamW(groups, weight_decay=1e-4)
        if resume:
            if not (resume.get("finished") and epoch == 4 and not incremental):
                optimizer.load_state_dict(resume["optimizer"])
            for state in optimizer.state.values():
                for key, value in state.items():
                    if isinstance(value, torch.Tensor):
                        state[key] = value.to(device)
            random.setstate(resume["random"])
            np.random.set_state(resume["numpy"])
            torch.set_rng_state(resume["torch"])
            if device == "cuda":
                torch.cuda.set_rng_state_all(resume["cuda"])
        model.train()
        for module in list(model.features)[:-4] if tune else list(model.features):
            module.eval()
        mid_epoch = resume and not resume.get("finished", False)
        order = resume["order"] if mid_epoch else torch.randperm(len(partitions["train"]), generator=generator).tolist()
        offset = resume["step"] if mid_epoch else 0
        total_loss, seen = (resume["loss"], resume["seen"]) if mid_epoch else (0.0, 0)
        steps = (len(order) + batch - 1) // batch

        # This synchronous per-epoch closure must observe the live optimizer and
        # counters; binding defaults would capture stale values at definition.
        def save_pause(next_step: int, finished: bool = False) -> None:
            state = {
                "format_version": 2,
                "task": "multiclass",
                "class_names": list(class_names),
                "metadata_sha256": metadata_digest(metadata),
                "snapshot_sha": digest(snapshot),
                "model": {key: value.detach().cpu() for key, value in model.state_dict().items()},
                "optimizer": optimizer.state_dict(),  # noqa: B023
                "initial": initial,
                "history": history,
                "coverage": coverage,
                "best": best,  # noqa: B023
                "generator": generator.get_state(),
                "epoch": epoch,  # noqa: B023
                "finished": finished,
                "step": next_step,
                "order": order,  # noqa: B023
                "loss": total_loss,  # noqa: B023
                "seen": seen,  # noqa: B023
                "random": random.getstate(),
                "numpy": np.random.get_state(),
                "torch": torch.get_rng_state(),
                "cuda": torch.cuda.get_rng_state_all() if device == "cuda" else [],
                "seconds": elapsed + time.monotonic() - started,
                "peak_rss": peak_rss,  # noqa: B023
            }
            temporary = checkpoint.with_suffix(".tmp")
            torch.save(state, temporary)
            temporary.replace(checkpoint)
            atomic_json(
                job_dir / "resume-audit.json",
                {
                    "snapshot_sha": digest(snapshot),
                    "sha": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
                    "task": "multiclass",
                    "format_version": 2,
                    "class_names": list(class_names),
                    "metadata_sha256": metadata_digest(metadata),
                },
            )
            raise TrainingPaused()

        dataset = Dataset(
            root,
            partitions["train"],
            True,
            input_size,
            snapshot.get("preprocessing") == "resize_before_augment",
            class_names,
        )
        resume = None
        for step in range(offset, steps):
            if cancelled():
                raise InterruptedError("training cancelled")
            if paused and paused():
                save_pause(step)
            examples = [dataset[index] for index in order[step * batch : (step + 1) * batch]]
            inputs = torch.stack([example[0] for example in examples])
            truth = torch.tensor([example[1] for example in examples])
            optimizer.zero_grad(set_to_none=True)
            output = model(inputs.to(device))
            loss = loss_fn(output, truth.to(device))
            if not torch.isfinite(loss):
                raise ValueError("non-finite training loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step()
            seen += len(truth)
            total_loss += loss.item() * len(truth)
            memory = psutil.Process().memory_info()._asdict()
            peak_rss = max(peak_rss, memory["rss"], memory.get("peak_wset", 0))
            progress(
                {
                    "phase": "training",
                    "epoch": epoch,
                    "epochs": epochs,
                    "step": step + 1,
                    "steps": steps,
                    "seen": seen,
                    "total_images": len(order),
                    "seconds": elapsed + time.monotonic() - started,
                }
            )
            if paused and paused():
                save_pause(step + 1)
        coverage.append(seen)
        progress(
            {
                "phase": "validation",
                "epoch": epoch,
                "epochs": epochs,
                "seen": seen,
                "total_images": len(order),
                "step": steps,
                "steps": steps,
                "seconds": elapsed + time.monotonic() - started,
            }
        )
        validation, predictions = evaluate_multiclass(
            model,
            root,
            partitions["val"],
            device,
            batch,
            class_names,
            metadata["score_threshold"],
            metadata["margin_threshold"],
            cancelled,
        )
        record = {"epoch": epoch, "loss": total_loss / seen, "validation": validation}
        history.append(record)
        rank = (validation["macro_f1"], validation["balanced_accuracy"], -epoch)
        if best is None or rank > best[0]:
            best = (rank, record, predictions)
            state = {key: value.detach().cpu() for key, value in model.state_dict().items()}
            torch.save(multiclass_artifact(state, metadata), job_dir / "selected.pt")
        if paused and paused():
            save_pause(steps, finished=True)

    selection = {
        "epoch": best[1]["epoch"],
        "validation": best[1]["validation"],
        "rule": "macro_f1,balanced_accuracy,earlier_epoch",
        "class_names": list(class_names),
        "score_threshold": metadata["score_threshold"],
        "margin_threshold": metadata["margin_threshold"],
    }
    atomic_json(job_dir / "selection.json", selection)
    selection_sha = hashlib.sha256((job_dir / "selection.json").read_bytes()).hexdigest()
    training_seconds = elapsed + time.monotonic() - started
    if cancelled():
        raise InterruptedError("训练已取消")
    progress(
        {
            "phase": "test",
            "selected_epoch": selection["epoch"],
            "epoch": epochs,
            "epochs": epochs,
            "seconds": training_seconds,
        }
    )
    selected = loaded_model(job_dir / "selected.pt").to(device)
    test, test_predictions = evaluate_multiclass(
        selected,
        root,
        partitions["test"],
        device,
        batch,
        class_names,
        metadata["score_threshold"],
        metadata["margin_threshold"],
        cancelled,
    )
    state = selected.state_dict()
    head_changed = any(
        not torch.equal(value.cpu(), initial[key])
        for key, value in state.items()
        if key.startswith("classifier.") and value.is_floating_point()
    )
    changed = any(not torch.equal(value.cpu(), initial[key]) for key, value in state.items() if value.is_floating_point())
    finite = all(torch.isfinite(value).all().item() for value in state.values())
    group_sets = [{row["group_id"] for row in partitions[split]} for split in partitions]
    isolation = all(not group_sets[left] & group_sets[right] for left in range(3) for right in range(left))
    for key in ("id", "sha", "decoded"):
        sets = [{row[key] for row in partitions[split]} for split in partitions]
        isolation = isolation and all(not sets[left] & sets[right] for left in range(3) for right in range(left))
    hashes = all(hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() == row["sha"] for row in rows)
    test_rebuilt = multiclass_metrics(
        [row["label"] for row in test_predictions],
        [list(row["scores"].values()) for row in test_predictions],
        class_names,
    )
    validation_rebuilt = multiclass_metrics(
        [row["label"] for row in best[2]],
        [list(row["scores"].values()) for row in best[2]],
        class_names,
    )
    recomputed = test_rebuilt == test and validation_rebuilt == best[1]["validation"]
    reloaded = loaded_model(job_dir / "selected.pt")
    reload_val, reload_pred = evaluate_multiclass(
        reloaded,
        root,
        partitions["val"],
        "cpu",
        8,
        class_names,
        metadata["score_threshold"],
        metadata["margin_threshold"],
    )
    reload_close = reload_val == best[1]["validation"] and all(
        a["scores"].keys() == b["scores"].keys()
        and all(abs(a["scores"][name] - b["scores"][name]) < 1e-3 for name in class_names)
        and a["predicted_class"] == b["predicted_class"]
        for a, b in zip(best[2], reload_pred, strict=True)
    )
    selection_unchanged = hashlib.sha256((job_dir / "selection.json").read_bytes()).hexdigest() == selection_sha
    mapping_matches = (
        selected.task == "multiclass"
        and selected.class_names == class_names
        and selected.score_threshold == metadata["score_threshold"]
        and selected.margin_threshold == metadata["margin_threshold"]
    )
    coverage_ok = all(count == len(partitions["train"]) for count in coverage)
    audit = {
        "finite": finite,
        "changed": changed,
        "head_changed": head_changed,
        "isolation": isolation,
        "image_hashes": hashes,
        "metrics_recomputed": recomputed,
        "reload_close": reload_close,
        "selection_unchanged": selection_unchanged,
        "training_coverage": coverage_ok,
        "metadata_matches_snapshot": mapping_matches,
        "metadata_sha256": metadata_digest(metadata),
    }
    audit["passed"] = all(audit.values())
    if not audit["passed"]:
        raise ValueError(f"多类别模型核验失败: {audit}")
    return {
        "task": "multiclass",
        "format_version": 2,
        "class_names": list(class_names),
        "score_threshold": metadata["score_threshold"],
        "margin_threshold": metadata["margin_threshold"],
        "metadata_sha256": metadata_digest(metadata),
        "mode": snapshot["mode"],
        "mode_reason": snapshot["mode_reason"],
        "training_policy": snapshot["training_policy"],
        "initialization_source": snapshot["initialization_source"],
        "selection": selection,
        "selection_sha256_before_test": selection_sha,
        "history": history,
        "validation_predictions": best[2],
        "test": test,
        "test_predictions": test_predictions,
        "audit": audit,
        "training_seconds_including_validation": training_seconds,
        "total_seconds": elapsed + time.monotonic() - started,
        "peak_rss_bytes": peak_rss,
        "peak_cuda_bytes": torch.cuda.max_memory_allocated() if device == "cuda" else 0,
        "historical_test": snapshot["historical_test"],
        "comparable_to_previous": snapshot["comparable_to_previous"],
        "counts": {split: len(values) for split, values in partitions.items()},
        "snapshot_sha": digest(snapshot),
        "device": device,
        "input_size": input_size,
        "trained_image_ids": [row["id"] for row in partitions["train"]],
    }


def infer_scores(model: nn.Module, raw: bytes) -> list[float]:
    if model.task != "multiclass":
        raise ValueError("当前模型不是多类别任务")
    image = decode(raw)
    tensor = NORMALIZE(transforms.ToTensor()(whole_image(image, model.input_size))).unsqueeze(0)
    with torch.inference_mode():
        scores = model(tensor).float().softmax(-1)[0].cpu().tolist()
    if len(scores) != len(model.class_names) or any(not np.isfinite(score) for score in scores):
        raise ValueError("非有限多类别模型分数")
    return [float(score) for score in scores]


def infer(model: nn.Module, raw: bytes) -> float:
    image = decode(raw)
    tensor = NORMALIZE(transforms.ToTensor()(whole_image(image, model.input_size))).unsqueeze(0)
    with torch.inference_mode():
        score = model(tensor).float().softmax(-1)[0, 1].item()
    if not np.isfinite(score):
        raise ValueError("非有限模型得分")
    return score
