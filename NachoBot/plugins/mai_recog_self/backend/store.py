"""SQLite catalog, persistent variant groups and immutable task snapshots."""

from pathlib import Path
from typing import Any
from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
import random
import shutil
import sqlite3
import time
import uuid
from urllib.parse import quote

from .images import decode, fingerprint, similar, thumbnail
from .labels import metadata_digest, ordered_class_names, validate_class_name


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(dump(value).encode()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(dump(value), encoding="utf-8")
    for attempt in range(6):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(.02)


class Store:
    def __init__(self, root: Path, *, initialize: bool = True, readonly: bool = False):
        self.root = root.resolve()
        self.readonly = readonly
        if initialize and readonly:
            raise ValueError("readonly stores cannot be initialized")
        if initialize:
            self.root.mkdir(parents=True, exist_ok=True)
            for name in ("images", "jobs", "models", "uploads"):
                (self.root / name).mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _initialize(self) -> None:
        with self.db() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS images(id TEXT PRIMARY KEY, path TEXT, sha TEXT, decoded TEXT, phash TEXT, aspect REAL, width INTEGER, height INTEGER, group_id TEXT, label TEXT, removed INTEGER DEFAULT 0, reason TEXT DEFAULT '', created REAL);
              CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY, split TEXT);
              CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, request_key TEXT UNIQUE, state TEXT, pid INTEGER, error TEXT, created REAL, snapshot_sha TEXT);
              CREATE UNIQUE INDEX IF NOT EXISTS one_training ON jobs((1)) WHERE state IN ('queued','running','cancelling');
              CREATE UNIQUE INDEX IF NOT EXISTS one_training_v2 ON jobs((1)) WHERE state IN ('queued','running','pausing','cancelling');
              CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY, job_id TEXT, sha TEXT, metrics TEXT, created REAL);
              CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
              CREATE INDEX IF NOT EXISTS images_group ON images(group_id);
            """)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        path = self.root / "catalog.sqlite3"
        if self.readonly:
            if not path.is_file():
                raise FileNotFoundError(path)
            uri = "file:" + quote(path.as_posix(), safe="/:\\") + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=2)
        else:
            db = sqlite3.connect(path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, key: str, default: str = "") -> str:
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        if self.readonly:
            raise ValueError("readonly store")
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def _history_task(self, db: sqlite3.Connection) -> str:
        """Infer the task from all history when an older catalog has no marker."""
        labels = {row[0] for row in db.execute("SELECT DISTINCT label FROM images WHERE label IS NOT NULL")}
        named = labels - {"self", "other", "skip"}
        has_self = "self" in labels
        has_legacy_rows = bool(labels & {"self", "other"})
        snapshot_tasks = set()
        snapshots_exist = False
        for row in db.execute("SELECT id FROM jobs"):
            snapshot_path = self.root / "jobs" / row[0] / "snapshot.json"
            if not snapshot_path.is_file():
                continue
            snapshots_exist = True
            try:
                snapshot = json.loads(snapshot_path.read_text("utf-8"))
            except (OSError, ValueError):
                snapshot_tasks.add("unknown")
                continue
            task = snapshot.get("task")
            if task == "multiclass" or snapshot.get("format_version") == 2:
                snapshot_tasks.add("multiclass")
            else:
                snapshot_tasks.add("binary")
        has_models = bool(db.execute("SELECT 1 FROM models LIMIT 1").fetchone())
        if has_self and named:
            return "mixed"
        if len(snapshot_tasks) > 1 or "unknown" in snapshot_tasks:
            return "mixed"
        if named:
            if snapshot_tasks and snapshot_tasks != {"multiclass"}:
                return "mixed"
            return "multiclass"
        if has_self:
            if snapshot_tasks and snapshot_tasks != {"binary"}:
                return "mixed"
            return "binary"
        if snapshot_tasks:
            return next(iter(snapshot_tasks))
        if has_legacy_rows or has_models or snapshots_exist:
            return "binary"
        return "uninitialized"

    def catalog_task(self, db: sqlite3.Connection | None = None) -> str:
        """Read the persisted task marker, failing closed on conflicting history."""
        if db is None:
            with self.db() as connection:
                return self.catalog_task(connection)
        history = self._history_task(db)
        marked_row = db.execute("SELECT value FROM settings WHERE key='catalog_task'").fetchone()
        marked = marked_row[0] if marked_row else ""
        if marked and marked not in {"binary", "multiclass"}:
            raise ValueError("数据目录任务标记无效，请检查catalog_task设置")
        if marked == "multiclass" and history == "binary":
            labels = {row[0] for row in db.execute("SELECT DISTINCT label FROM images WHERE label IS NOT NULL")}
            has_binary_job = any(
                json.loads(path.read_text("utf-8")).get("task") != "multiclass"
                for row in db.execute("SELECT id FROM jobs")
                for path in [self.root / "jobs" / row[0] / "snapshot.json"]
                if path.is_file()
            )
            if labels <= {"other", "skip"} and not has_binary_job and db.execute("SELECT 1 FROM models LIMIT 1").fetchone() is None:
                history = "uninitialized"
        if marked and history not in {"uninitialized", marked}:
            raise ValueError("数据目录包含冲突的二分类与多类别历史；请使用独立的--data-dir")
        if history == "mixed":
            raise ValueError("数据目录包含冲突的二分类与多类别历史；请使用独立的--data-dir")
        return marked or history

    def registered_class_names(self, db: sqlite3.Connection | None = None) -> list[str]:
        if db is None:
            with self.db() as connection:
                return self.registered_class_names(connection)
        row = db.execute("SELECT value FROM settings WHERE key='class_names'").fetchone()
        if row:
            try:
                names = json.loads(row[0])
                if not isinstance(names, list):
                    raise ValueError
                return ordered_class_names(names)
            except (TypeError, ValueError) as exc:
                raise ValueError("保存的多类别清单无效；请勿手动删除或修改类别元数据") from exc
        # Older multiclass catalogs may have no explicit list; recover every class
        # ever recorded in rows and immutable snapshots.
        names = {row[0] for row in db.execute("SELECT DISTINCT label FROM images WHERE label IS NOT NULL")}
        for row in db.execute("SELECT id FROM jobs"):
            path = self.root / "jobs" / row[0] / "snapshot.json"
            if path.is_file():
                try:
                    snapshot = json.loads(path.read_text("utf-8"))
                    names.update(snapshot.get("class_names", []))
                except (OSError, ValueError, TypeError):
                    continue
        names.discard("self")
        names.discard("skip")
        names.discard(None)
        return ordered_class_names(names) if names else []

    def register_catalog(self, db: sqlite3.Connection, task: str, class_names: list[str] | None = None) -> list[str]:
        existing = self.catalog_task(db)
        if existing not in {"uninitialized", task}:
            raise ValueError("二分类与多类别数据不能混用；请使用新的--data-dir保留历史数据")
        db.execute("INSERT OR REPLACE INTO settings VALUES ('catalog_task',?)", (task,))
        if task == "binary":
            return ["self", "other"]
        current = self.registered_class_names(db) if existing == "multiclass" else []
        union = ordered_class_names([*current, *(class_names or [])])
        db.execute("INSERT OR REPLACE INTO settings VALUES ('class_names',?)", (dump(union),))
        return union

    def valid_training_labels(self, db: sqlite3.Connection | None = None) -> list[str]:
        task = self.catalog_task(db)
        if task == "multiclass":
            return self.registered_class_names(db)
        return ["self", "other"]

    def _reconcile(self, db: sqlite3.Connection, group: str) -> None:
        rows = db.execute(
            "SELECT * FROM images WHERE group_id=? AND removed=0 AND reason NOT IN ('solid','split_conflict') ORDER BY created,id",
            (group,),
        ).fetchall()
        labels = {r["label"] for r in rows if r["label"] not in {None, "skip"}}
        rows = sorted(rows, key=lambda r: (r["label"] in {None, "skip"}, r["created"], r["id"]))
        for index, row in enumerate(rows):
            reason = "label_conflict" if len(labels) > 1 else ("duplicate" if index else "")
            db.execute("UPDATE images SET reason=? WHERE id=?", (reason, row["id"]))

    def ingest(self, path: Path, label: str | None = None) -> dict:
        if label is not None:
            if label not in {"self", "other"}:
                label = validate_class_name(label)
                task = self.catalog_task()
                if task != "multiclass" or label not in self.registered_class_names():
                    raise ValueError("上传类别必须是当前目录已注册的类别")
            elif self.catalog_task() == "multiclass" and label not in self.registered_class_names():
                raise ValueError("多类别目录不能写入旧版self/other标签")
        path = path.resolve()
        if not path.is_relative_to(self.root / "uploads") or path.is_symlink():
            raise ValueError("只能领取本插件上传文件")
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        image = decode(raw)
        fp = fingerprint(image)
        ident = uuid.uuid4().hex
        destination = self.root / "images" / (ident + path.suffix)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id,reason,label FROM images WHERE sha=? OR decoded=? ORDER BY created,id LIMIT 1",
                (sha, fp["decoded"]),
            ).fetchone()
            if existing is not None:
                path.unlink()
                old_label = existing["label"]
                if label is not None and old_label is not None and old_label != "skip" and label != old_label:
                    import_status = "duplicate_label_conflict"
                elif label is not None and old_label is None:
                    import_status = "duplicate_unlabelled"
                else:
                    import_status = "duplicate"
                return {
                    "id": existing["id"],
                    "reason": existing["reason"],
                    "already_exists": True,
                    "import_status": import_status,
                    "stored_label": old_label,
                }
            matches = [
                dict(r)
                for r in db.execute("SELECT * FROM images WHERE reason NOT IN ('solid','split_conflict')")
                if similar(fp, dict(r))
            ]
            groups = {r["group_id"] for r in matches}
            splits = {r[0] for g in groups for r in db.execute("SELECT split FROM groups WHERE id=?", (g,)) if r[0]}
            reason = "solid" if fp["solid"] else ("split_conflict" if len(splits) > 1 else "")
            group = min(groups) if groups and reason != "split_conflict" else ident
            db.execute(
                "INSERT OR IGNORE INTO groups VALUES (?,?)",
                (group, next(iter(splits), None) if reason != "split_conflict" else None),
            )
            if not reason:
                for old in groups - {group}:
                    db.execute("UPDATE images SET group_id=? WHERE group_id=?", (group, old))
                    db.execute("DELETE FROM groups WHERE id=?", (old,))
                if splits:
                    db.execute("UPDATE groups SET split=? WHERE id=?", (next(iter(splits)), group))
            shutil.move(path, destination)
            db.execute(
                "INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (
                    ident,
                    str(destination.relative_to(self.root)),
                    sha,
                    fp["decoded"],
                    fp["phash"],
                    fp["aspect"],
                    fp["width"],
                    fp["height"],
                    group,
                    label,
                    reason,
                    time.time(),
                ),
            )
            self._reconcile(db, group)
        return {"id": ident, "reason": reason}

    def label(self, image_id: str, label: str, remove: bool | None = None) -> dict:
        task = self.catalog_task()
        if label not in {"skip", "unlabelled"}:
            if task == "multiclass":
                label = validate_class_name(label)
                if label not in self.registered_class_names():
                    raise ValueError("只能标注当前多类别目录已注册的类别")
            elif label not in {"self", "other"}:
                raise ValueError("二分类目录仅允许self/other标签；多类别数据请使用新的--data-dir")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT group_id FROM images WHERE id=?", (image_id,)).fetchone()
            if row is None:
                raise ValueError("图片不存在")
            db.execute(
                "UPDATE images SET label=?,removed=COALESCE(?,removed) WHERE id=?",
                (None if label == "unlabelled" else label, int(remove) if remove is not None else None, image_id),
            )
            self._reconcile(db, row[0])
        return {"id": image_id, "label": label, "removed": remove}

    def set_removed(self, image_id: str, removed: bool) -> dict:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT group_id FROM images WHERE id=?", (image_id,)).fetchone()
            if row is None:
                raise ValueError("图片不存在")
            db.execute("UPDATE images SET removed=? WHERE id=?", (int(removed), image_id))
            self._reconcile(db, row[0])
        return {"id": image_id, "removed": removed}

    def batch_update(self, image_ids: str, operation: str) -> dict:
        """Apply the entire selection in one transaction, without partial updates."""
        if operation not in {"self", "other", "remove", "remove_all"}:
            raise ValueError("无效批量操作")
        if operation in {"self", "other"} and self.catalog_task() == "multiclass":
            if operation not in self.registered_class_names():
                raise ValueError("多类别目录只能使用已注册的类别标签")
        ids = list(dict.fromkeys(image_ids.split(","))) if image_ids else []
        if operation != "remove_all" and (not ids or len(ids) > 1000):
            raise ValueError("请先选择图片，每批最多1000张")
        if any(len(i) != 32 or any(c not in "0123456789abcdef" for c in i) for i in ids):
            raise ValueError("无效图片ID")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if operation == "remove_all":
                rows = db.execute("SELECT id,group_id FROM images WHERE removed=0").fetchall()
                db.execute("UPDATE images SET removed=1 WHERE removed=0")
            else:
                placeholders = ",".join("?" for _ in ids)
                rows = db.execute(f"SELECT id,group_id FROM images WHERE id IN ({placeholders})", ids).fetchall()
                if len(rows) != len(ids):
                    raise ValueError("选择中有不存在的图片，请清空选择后重新选择")
                if operation == "remove":
                    db.execute(f"UPDATE images SET removed=1 WHERE id IN ({placeholders})", ids)
                else:
                    if db.execute(f"SELECT 1 FROM images WHERE removed=1 AND id IN ({placeholders}) LIMIT 1", ids).fetchone():
                        raise ValueError("选择中有已删除图片，请先恢复或取消选择")
                    db.execute(f"UPDATE images SET label=? WHERE id IN ({placeholders})", [operation, *ids])
            for group in {row["group_id"] for row in rows}:
                self._reconcile(db, group)
        return {"updated": len(rows), "operation": operation}

    def trained_sources(self, db, version: str) -> list:
        if not version:
            return []
        record = db.execute("SELECT job_id FROM models WHERE id=?", (version,)).fetchone()
        if record is None:
            raise ValueError("模型版本不存在")
        snapshot = json.loads((self.root / "jobs" / record[0] / "snapshot.json").read_text("utf-8"))
        return snapshot.get("trained_sources", [r for r in snapshot["images"] if r["split"] == "train"])

    @staticmethod
    def source_seen(row, sources) -> bool:
        for source in reversed(sources):
            if source["group_id"] == row["group_id"] or source["sha"] == row["sha"] or source["decoded"] == row["decoded"]:
                return source["label"] == row["label"]
        return False

    def images(self, page: int = 1, view: str = "all", job_id: str = "") -> dict:
        if page < 1 or view not in {"all", "eligible", "excluded", "unlabelled", "errors", "removed"}:
            raise ValueError("无效分页")
        labels = self.valid_training_labels()
        where = {
            "all": "i.removed=0",
            "eligible": "i.removed=0 AND i.reason='' AND i.label IN (" + ",".join("?" for _ in labels) + ")",
            "excluded": "(i.removed=1 OR i.reason!='' OR i.label='skip')",
            "unlabelled": "i.label IS NULL",
            "errors": "1",
            "removed": "i.removed=1",
        }[view]
        error_ids = set()
        predictions = {}
        if view == "errors":
            if not job_id:
                return {
                    "rows": [],
                    "gallery": [],
                    "pages": 1,
                    "total": 0,
                    "pagination": {"page": 1, "pages": 1, "total": 0},
                }
            report = self.report(job_id)
            predictions = {r["id"]: r for r in report.get("test_predictions", []) + report.get("validation_predictions", [])}
            error_ids = {
                r["id"]
                for r in report.get("test_predictions", []) + report.get("validation_predictions", [])
                if r.get("predicted", r.get("predicted_class")) != r["label"]
            }
        parameters = labels if view == "eligible" else []
        if view == "errors":
            if error_ids:
                where += " AND i.id IN (" + ",".join("?" for _ in error_ids) + ")"
                parameters = sorted(error_ids)
            else:
                where = "0"
        with self.db() as db:
            task = self.catalog_task(db)
            sources = self.trained_sources(db, self.setting("active"))
            total = db.execute(f"SELECT COUNT(*) FROM images i WHERE {where}", parameters).fetchone()[0]
            page = min(page, max(1, (total + 23) // 24))
            rows = [dict(r) for r in db.execute(
                f"SELECT i.*,g.split FROM images i JOIN groups g ON g.id=i.group_id WHERE {where} ORDER BY i.created DESC LIMIT 24 OFFSET ?",
                [*parameters, (page - 1) * 24],
            )]
        gallery = []
        for r in rows:
            reasons = {"solid": "纯色/近纯色", "duplicate": "同图变体，仅保留一个代表", "label_conflict": "同图身份标签冲突", "split_conflict": "连接了不同历史划分"}
            r["status"] = "已移出训练库" if r["removed"] else reasons.get(r["reason"], "跳过" if r["label"] == "skip" else "可用")
            other_text = "背景 / other" if task == "multiclass" else "不是自己"
            r["label_text"] = {"self": "是自己", "other": other_text, "skip": "跳过"}.get(
                r["label"], r["label"] if task == "multiclass" and r["label"] else "未标注"
            )
            r["trained"] = self.source_seen(r, sources)
            r["training_text"] = "当前模型已训练" if r["trained"] else "验证集" if r["split"] == "val" else "测试集" if r["split"] == "test" else "待训练"
            prediction = predictions.get(r["id"])
            if prediction and task == "multiclass":
                predicted = prediction.get("predicted", prediction.get("predicted_class", "不确定"))
                r["model_text"] = f"模型：{predicted} · 得分 {prediction['score']:.3f} · 冻结真值：{prediction['label']}"
            else:
                r["model_text"] = (f"模型：{'是自己' if prediction['predicted']=='self' else '不是自己'} · 得分 {prediction['score']:.3f} · 冻结真值：{'是自己' if prediction['label']=='self' else '不是自己'}" if prediction else "")
            gallery.append(
                {
                    "thumbnail": thumbnail(self.root / r["path"], self.root / "thumbnails" / (r["sha"] + "-512.jpg")),
                    "title": r["id"],
                    "description": f"{r['label_text']} / {r['status']} / {r['split'] or '未划分'}",
                    "badge": r["label_text"],
                    "subtitle": f"{r['width']}×{r['height']}",
                }
            )
        for row, preview in zip(rows, gallery, strict=True):
            row["thumbnail"] = preview["thumbnail"]
        return {
            "rows": rows,
            "pages": max(1, (total + 23) // 24),
            "total": total,
            "pagination": {"page": page, "pages": max(1, (total + 23) // 24), "total": total},
        }

    def training_readiness(self) -> dict:
        with self.db() as db:
            task = self.catalog_task(db)
            labels = self.registered_class_names(db) if task == "multiclass" else ["self", "other"]
            if task == "multiclass" and "other" not in labels:
                labels = ordered_class_names([*labels, "other"])
            counts = dict.fromkeys(labels, 0)
            placeholders = ",".join("?" for _ in labels)
            for row in db.execute(
                f"SELECT label,COUNT(DISTINCT group_id) AS count FROM images WHERE removed=0 AND reason='' AND label IN ({placeholders}) GROUP BY label",
                labels,
            ):
                counts[row["label"]] = row["count"]
            active = self.setting("active")
            sources = self.trained_sources(db, active)
            candidates = db.execute(
                f"SELECT i.*,g.split FROM images i JOIN groups g ON g.id=i.group_id WHERE i.removed=0 AND i.reason='' AND i.label IN ({placeholders}) AND (g.split='train' OR g.split IS NULL)",
                labels,
            ).fetchall()
            pending = sum(not self.source_seen(r, sources) for r in candidates)
        if task == "multiclass":
            summary = "可训练独立图组：" + " / ".join(f"{name} {count}" for name, count in counts.items()) + "。需至少一个角色类别和背景类别other，每类至少20组，重复变体只算一组。"
        else:
            summary = f"可训练独立图组：是Bot {counts['self']} / 不是Bot {counts['other']}。每类至少20组，重复变体只算一组。"
        return {
            "can_train": (
                bool(counts)
                and min(counts.values()) >= 20
                and (task != "multiclass" or len(counts) >= 2 and "other" in counts)
            ),
            "has_model": bool(active),
            "pending_count": pending,
            "summary": summary,
            "groups": counts,
        }

    def freeze(self, db: sqlite3.Connection, job_id: str, mode: str, device: str, input_size: int = 320) -> dict:
        if type(input_size) is not int or input_size not in {224, 320, 448}:
            raise ValueError("图片尺寸必须为224、320或448")
        if mode not in {"fresh", "incremental"}:
            raise ValueError("训练模式无效")
        task = self.catalog_task(db)
        if task == "mixed":
            raise ValueError("二分类与多类别历史不能混用；请使用独立的--data-dir")
        if task == "multiclass":
            try:
                class_names = ordered_class_names(self.registered_class_names(db), require_other=True)
            except ValueError as exc:
                raise ValueError(f"多类别训练要求至少一个角色类别和背景类别other：{exc}") from exc
        else:
            task = "binary"
            class_names = ["other", "self"]
        placeholders = ",".join("?" for _ in class_names)
        rows = [
            dict(r)
            for r in db.execute(
                f"SELECT i.*,g.split FROM images i JOIN groups g ON g.id=i.group_id WHERE i.removed=0 AND i.reason='' AND i.label IN ({placeholders}) ORDER BY i.id",
                class_names,
            )
        ]
        # A labelled variant can supply the group truth to its unlabelled representative.
        representatives = {r["group_id"]: r for r in rows}
        for r in db.execute("SELECT * FROM images WHERE removed=0 AND reason='' AND label IS NULL"):
            truth = db.execute(
                f"SELECT label FROM images WHERE group_id=? AND label IN ({placeholders}) AND removed=0 LIMIT 1",
                [r["group_id"], *class_names],
            ).fetchone()
            if truth:
                value = dict(r)
                value["label"] = truth[0]
                value["split"] = db.execute("SELECT split FROM groups WHERE id=?", (r["group_id"],)).fetchone()[0]
                representatives[r["group_id"]] = value
        rows = list(representatives.values())
        rng = random.Random(42)
        split_labels = ["self", "other"] if task == "binary" else class_names
        for label in split_labels:
            cohort = [r for r in rows if r["label"] == label]
            if len(cohort) < 20:
                name = ("是Bot" if label == "self" else "不是Bot") if task == "binary" else label
                raise ValueError(f"“{name}”至少需要20个可训练独立图片组，当前{len(cohort)}；请上传或补标该类图片")
            new = sorted((r for r in cohort if not r["split"]), key=lambda r: r["group_id"])
            rng.shuffle(new)
            # Fill cumulative targets while preserving every existing assignment.
            targets = {"test": max(2, round(len(cohort) * 0.15)), "val": max(2, round(len(cohort) * 0.15))}
            targets["train"] = len(cohort) - targets["test"] - targets["val"]
            counts = {s: sum(r["split"] == s for r in cohort) for s in targets}
            for r in new:
                split = max(targets, key=lambda s: targets[s] - counts[s])
                r["split"] = split
                counts[split] += 1
                db.execute("UPDATE groups SET split=? WHERE id=?", (split, r["group_id"]))
            if any(counts[s] < 2 for s in targets):
                raise ValueError(f"{label} 每个划分至少需要2张")
        requested_mode = mode
        parent = self.setting("active") if mode == "incremental" else ""
        parent_record = db.execute("SELECT sha FROM models WHERE id=?", (parent,)).fetchone() if parent else None
        if parent and parent_record is None:
            raise ValueError("当前启用模型的记录不存在")
        mode_reason = "explicit_fresh" if requested_mode == "fresh" else "same_classset_replay"
        parent_classes = None
        if parent and task == "multiclass":
            parent_model = db.execute("SELECT job_id FROM models WHERE id=?", (parent,)).fetchone()
            parent_snapshot_path = self.root / "jobs" / parent_model[0] / "snapshot.json"
            try:
                parent_snapshot = json.loads(parent_snapshot_path.read_text("utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("当前启用多类别模型缺少有效的冻结类别映射") from exc
            if parent_snapshot.get("task") != "multiclass" or parent_snapshot.get("format_version") != 2:
                raise ValueError("当前启用模型不是兼容的多类别模型；请使用独立的--data-dir")
            parent_classes = parent_snapshot.get("class_names")
            if parent_classes != ordered_class_names(parent_classes, require_other=True):
                raise ValueError("当前启用模型的类别映射无效")
            if parent_classes != class_names:
                parent = ""
                parent_record = None
                mode = "fresh"
                mode_reason = "class_set_changed"
            else:
                mode = "incremental"
        elif requested_mode == "incremental" and not parent:
            mode = "fresh"
            mode_reason = "no_active_model"
        else:
            mode = requested_mode
        inherited_sources = self.trained_sources(db, parent)
        if parent and task == "binary":
            rows = [r for r in rows if r["split"] != "train" or not self.source_seen(r, inherited_sources)]
            if not any(r["split"] == "train" for r in rows):
                raise ValueError("没有待训练图片，请新增图片、修改标签或选择从头训练")
        learned = [r for r in rows if r["split"] == "train"]
        if task == "multiclass":
            trained_sources = [{k: r[k] for k in ("id", "label", "sha", "decoded", "group_id")} for r in learned]
        else:
            trained_sources = [{k: r[k] for k in ("id", "label", "sha", "decoded", "group_id")} for r in [*inherited_sources, *learned]]
        held_truth = digest(sorted((r["id"], r["label"]) for r in rows if r["split"] != "train"))
        previous = db.execute(
            "SELECT snapshot_sha FROM jobs WHERE state='complete' ORDER BY created DESC LIMIT 1"
        ).fetchone()
        old_truth = self.setting("last_held_truth")
        character = db.execute("SELECT value FROM settings WHERE key='character'").fetchone()
        snapshot = {
            "job_id": job_id,
            "task": task,
            "format_version": 2 if task == "multiclass" else 1,
            "mode": mode,
            "mode_reason": mode_reason,
            "device": device,
            "input_size": input_size,
            "preprocessing": "resize_before_augment",
            "training_policy": "full_replay" if task == "multiclass" else ("new_only" if parent else "all"),
            "trained_sources": trained_sources,
            "seed": 42,
            "parent": parent,
            "parent_sha": parent_record[0] if parent_record else "",
            "character": character[0] if character else "Bot自己",
            "images": rows,
            "held_truth": held_truth,
            "comparable_to_previous": not old_truth or held_truth == old_truth,
            "historical_test": bool(previous),
            "threshold": 0.5,
            "created": time.time(),
        }
        if task == "multiclass":
            snapshot.update(
                {
                    "class_names": class_names,
                    "score_threshold": 0.6,
                    "margin_threshold": 0.15,
                    "initialization_source": "parent_model" if parent else "imagenet_pretrained",
                }
            )
            snapshot["metadata_sha256"] = metadata_digest(
                {
                    "format_version": snapshot["format_version"],
                    "task": snapshot["task"],
                    "class_names": class_names,
                    "score_threshold": snapshot["score_threshold"],
                    "margin_threshold": snapshot["margin_threshold"],
                    "input_size": snapshot["input_size"],
                }
            )
        return snapshot

    def report(self, job_id: str) -> dict:
        self.job(job_id)  # reject arbitrary paths
        path = self.root / "jobs" / job_id / "report.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else {}

    def job(self, job_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("任务不存在")
        return dict(row)

    def models(self) -> list:
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM models ORDER BY created DESC")]
