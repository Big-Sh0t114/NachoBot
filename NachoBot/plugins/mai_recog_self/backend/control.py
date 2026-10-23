"""Foreground training controller with an OS lock and orphan-safe recovery."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
import json
import os
import subprocess
import time
import uuid

from .paths import CORE_ROOT
from .environment import select_worker_python, worker_environment
from .images import MAX_BYTES
from .labels import ordered_class_names, validate_class_name
from .processes import process_identity, worker_identity, worker_is_same_process
from .store import Store, atomic_json, digest


ACTIVE_STATES = {"queued", "running", "pausing", "cancelling"}
TERMINAL_STATES = {"complete", "failed", "cancelled", "paused", "interrupted"}


class DataLock(AbstractContextManager):
    """A bounded OS lock. Kernel state releases it after controller crashes."""

    def __init__(self, root: Path, timeout: float = 3.0):
        self.root = root
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.handle = (self.root / ".controller.lock").open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise TimeoutError("该数据目录正在被另一个CLI修改或训练，请稍后重试") from None
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return False
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None
        return False


def _job_dir(store: Store, job_id: str) -> Path:
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise ValueError("无效任务编号")
    return store.root / "jobs" / job_id


def _worker_python(store: Store, explicit: str = "", *, persist: bool = False) -> str:
    selected = select_worker_python(store.root, explicit)
    if explicit and persist:
        store.set_setting("worker_python", selected)
    return selected


def _worker_command(python: str, operation: str, root: Path, argument: str = "") -> list[str]:
    command = [
        python,
        "-X",
        "utf8",
        "-u",
        "-m",
        "plugins.mai_recog_self.backend.worker",
        operation,
        str(root.resolve()),
    ]
    if argument:
        command.append(argument)
    return command


def _list_jobs(store: Store, limit: int = 50) -> list[dict]:
    with store.db() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))]
    for row in rows:
        progress_path = store.root / "jobs" / row["id"] / "progress.json"
        try:
            row["progress"] = json.loads(progress_path.read_text("utf-8"))
        except (OSError, ValueError):
            row["progress"] = {"phase": row["state"]}
    return rows


def status(root: Path) -> dict:
    """Read status without initializing tables or changing a running job."""
    db_path = root.resolve() / "catalog.sqlite3"
    if not db_path.is_file():
        return {
            "data_root": str(root.resolve()),
            "active_version": "",
            "jobs": [],
            "models": [],
            "readiness": {"can_train": False, "has_model": False, "pending_count": 0, "groups": {"self": 0, "other": 0}},
        }
    store = Store(root, initialize=False, readonly=True)
    models = store.models()
    return {
        "data_root": str(store.root),
        "active_version": store.setting("active"),
        "previous_version": store.setting("previous"),
        "jobs": _list_jobs(store),
        "models": models,
        "readiness": store.training_readiness(),
    }


def _worker_alive_for_job(store: Store, row: dict) -> bool | None:
    directory = _job_dir(store, row["id"])
    identity = worker_identity(directory / "worker.json")
    identity_alive = worker_is_same_process(identity) if identity else None
    pid = row.get("pid")
    if pid:
        current = process_identity(int(pid))
        if current is None:
            pid_alive = False
        elif current.get("unknown") or current.get("created") is None:
            pid_alive = None
        else:
            # A worker cannot predate the job row. This also recognizes PID reuse.
            pid_alive = float(current["created"]) >= float(row["created"]) - 1.0
        # The DB PID is published by the controller for the newly spawned worker.
        # A previous worker.json may remain during recovery or a crash window; it
        # must never hide a live process under the current DB PID.
        if pid_alive is True or identity_alive is True:
            return True
        if pid_alive is None or identity_alive is None and identity:
            return None
        return False
    if identity:
        return identity_alive
    return None


def _recover_or_reject_live_workers(store: Store) -> None:
    """Never infer that acquiring the controller lock means an old child exited."""
    with store.db() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 100")]
    now = time.time()
    for row in rows:
        directory = _job_dir(store, row["id"])
        identity_path = directory / "worker.json"
        if row["state"] not in ACTIVE_STATES and not identity_path.exists():
            continue
        identity = worker_identity(identity_path)
        alive = _worker_alive_for_job(store, row)
        identity_unreadable = identity_path.exists() and identity is None
        if alive is True or alive is None and (identity is not None or identity_unreadable or row.get("pid")):
            raise RuntimeError(
                f"旧训练worker仍可能存活（job {row['id']}，state={row['state']}）；等待它退出后再重试"
            )
        if row["state"] in ACTIVE_STATES and alive is False:
            with store.db() as db:
                db.execute(
                    "UPDATE jobs SET state='interrupted',error='控制器恢复时确认旧worker已退出；请检查断点后显式恢复' WHERE id=? AND state IN ('queued','running','pausing','cancelling')",
                    (row["id"],),
                )
        elif row["state"] in ACTIVE_STATES and alive is None:
            age = now - float(row["created"])
            lease = directory / "lease"
            lease_age = now - lease.stat().st_mtime if lease.exists() else age
            if age < 120 or lease_age < 120:
                raise RuntimeError(f"job {row['id']} 尚未发布worker身份，稍后重试")
            with store.db() as db:
                db.execute(
                    "UPDATE jobs SET state='interrupted',error='worker身份未发布且租约已过期' WHERE id=? AND state IN ('queued','running','pausing','cancelling')",
                    (row["id"],),
                )


def _mark_interrupted(store: Store, job_id: str, message: str) -> None:
    with store.db() as db:
        db.execute(
            "UPDATE jobs SET state='interrupted',error=? WHERE id=? AND state IN ('queued','running','pausing','cancelling')",
            (message, job_id),
        )


def _spawn(store: Store, job_id: str, python: str) -> subprocess.Popen:
    directory = _job_dir(store, job_id)
    # Each spawn is a new identity generation. Clear the prior worker record and
    # PID before the child can start; the fresh lease prevents recovery from
    # admitting another worker in the publication window after a crash.
    (directory / "worker.json").unlink(missing_ok=True)
    (directory / "lease").touch()
    with store.db() as db:
        db.execute("UPDATE jobs SET pid=NULL WHERE id=? AND state IN ('queued','running','pausing','cancelling')", (job_id,))
    log = (directory / "worker.log").open("ab", buffering=0)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    kwargs = {"start_new_session": True} if os.name != "nt" else {}
    try:
        process = subprocess.Popen(
            _worker_command(python, "train", store.root, job_id),
            cwd=CORE_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            creationflags=flags,
            env=worker_environment(store.root),
            **kwargs,
        )
    finally:
        log.close()
    with store.db() as db:
        db.execute("UPDATE jobs SET pid=? WHERE id=?", (process.pid, job_id))
    return process


def _progress(store: Store, job_id: str) -> dict:
    path = _job_dir(store, job_id) / "progress.json"
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _request_pause(store: Store, job_id: str) -> None:
    job = store.job(job_id)
    if job["state"] not in {"queued", "running"}:
        if job["state"] == "paused":
            return
        raise ValueError(f"当前任务状态 {job['state']} 不可暂停")
    if _progress(store, job_id).get("phase") == "test":
        raise ValueError("最终测试核验无法暂停；继续等待，或运行 cancel 命令取消")
    directory = _job_dir(store, job_id)
    (directory / "pause").touch()
    with store.db() as db:
        db.execute("UPDATE jobs SET state='pausing' WHERE id=? AND state IN ('queued','running')", (job_id,))


def request_pause(root: Path, job_id: str, *, wait: bool = True) -> dict:
    store = Store(root, initialize=False)
    job = store.job(job_id)
    _request_pause(store, job_id)
    if wait:
        deadline = time.monotonic() + 300
        while True:
            job = store.job(job_id)
            if job["state"] == "paused":
                identity = worker_identity(_job_dir(store, job_id) / "worker.json")
                alive = worker_is_same_process(identity) if identity else _worker_alive_for_job(store, job)
                if alive is False:
                    return job
                if alive is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("worker已报告暂停，但无法确认其退出；请勿并行恢复任务")
                elif time.monotonic() >= deadline:
                    raise TimeoutError("checkpoint已写入，但worker仍未退出；稍后确认进程结束后再恢复")
            if job["state"] in {"complete", "failed", "cancelled", "interrupted"}:
                raise RuntimeError(f"任务在确认暂停前进入状态 {job['state']}: {job.get('error', '')}")
            if job["state"] in ACTIVE_STATES:
                alive = _worker_alive_for_job(store, job)
                if alive is False:
                    _mark_interrupted(store, job_id, "请求暂停期间worker已退出但未确认checkpoint")
                    raise RuntimeError("worker已退出但没有确认checkpoint，任务标记为interrupted")
            if time.monotonic() >= deadline:
                raise TimeoutError("等待checkpoint确认超过300秒；任务仍由原controller持有，请检查状态")
            time.sleep(0.25)
    return store.job(job_id)


def request_cancel(root: Path, job_id: str, *, wait: bool = True) -> dict:
    store = Store(root, initialize=False)
    directory = _job_dir(store, job_id)
    job = store.job(job_id)
    if job["state"] == "paused":
        with store.db() as db:
            db.execute("UPDATE jobs SET state='cancelled',error='用户取消暂停任务' WHERE id=? AND state='paused'", (job_id,))
        (directory / "resume.pt").unlink(missing_ok=True)
        (directory / "resume-audit.json").unlink(missing_ok=True)
        return store.job(job_id)
    if job["state"] not in ACTIVE_STATES:
        if job["state"] == "cancelled":
            return job
        raise ValueError(f"当前任务状态 {job['state']} 不可取消")
    (directory / "cancel").touch()
    with store.db() as db:
        db.execute("UPDATE jobs SET state='cancelling' WHERE id=? AND state IN ('queued','running','pausing')", (job_id,))
    if wait:
        deadline = time.monotonic() + 300
        while True:
            job = store.job(job_id)
            if job["state"] in TERMINAL_STATES:
                return job
            if _worker_alive_for_job(store, job) is False:
                _mark_interrupted(store, job_id, "取消等待期间worker已退出但未提交终态")
                raise RuntimeError("worker已退出，任务标记为interrupted")
            if time.monotonic() >= deadline:
                raise TimeoutError("取消请求仍未完成；worker继续受原controller监控")
            time.sleep(0.25)
    return store.job(job_id)


def _monitor(store: Store, job_id: str, process: subprocess.Popen) -> dict:
    previous = None
    while True:
        try:
            job = store.job(job_id)
            progress = _progress(store, job_id)
            signature = (job["state"], json.dumps(progress, sort_keys=True, ensure_ascii=False))
            if signature != previous:
                print(
                    json.dumps(
                        {"job_id": job_id, "state": job["state"], "progress": progress},
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    flush=True,
                )
                previous = signature
            if process.poll() is None:
                lease = _job_dir(store, job_id) / "lease"
                if lease.exists():
                    os.utime(lease, None)
            else:
                process.wait()  # reap the child before releasing the data-root lock
                job = store.job(job_id)
                if job["state"] not in TERMINAL_STATES:
                    _mark_interrupted(store, job_id, f"worker退出但未提交终态，exit={process.returncode}")
                    job = store.job(job_id)
                if job["state"] == "paused":
                    return job
                if job["state"] == "complete":
                    return job
                if job["state"] in {"cancelled", "failed", "interrupted"}:
                    return job
            time.sleep(0.25)
        except KeyboardInterrupt:
            progress = _progress(store, job_id)
            if progress.get("phase") == "test":
                print(
                    json.dumps(
                        {
                            "job_id": job_id,
                            "notice": "最终测试核验无法暂停；继续等待，或在另一个终端运行 cancel " + job_id,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue
            try:
                _request_pause(store, job_id)
                print(
                    json.dumps({"job_id": job_id, "notice": "正在保存checkpoint；等待worker确认暂停并退出"}),
                    flush=True,
                )
            except ValueError as exc:
                print(json.dumps({"job_id": job_id, "notice": str(exc)}, ensure_ascii=False), flush=True)


def _start_new(store: Store, mode: str, device: str, input_size: int, character: str, python: str) -> str:
    job_id = uuid.uuid4().hex
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if character:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('character',?)", (character,))
        snapshot = store.freeze(db, job_id, mode, device, input_size)
        snapshot["character"] = character or snapshot["character"]
        store.root.joinpath("jobs", job_id).mkdir()
        atomic_json(store.root / "jobs" / job_id / "snapshot.json", snapshot)
        atomic_json(store.root / "jobs" / job_id / "progress.json", {"phase": "queued"})
        (store.root / "jobs" / job_id / "lease").touch()
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (job_id, job_id, "queued", time.time(), digest(snapshot)),
        )
    try:
        process = _spawn(store, job_id, python)
    except Exception as exc:
        with store.db() as db:
            db.execute("UPDATE jobs SET state='failed',error=? WHERE id=?", (str(exc), job_id))
        (store.root / "jobs" / job_id / "lease").unlink(missing_ok=True)
        raise
    row = _monitor(store, job_id, process)
    if row["state"] == "failed":
        log = store.root / "jobs" / job_id / "worker.log"
        detail = log.read_text("utf-8", errors="replace")[-4000:] if log.exists() else ""
        raise RuntimeError(f"训练worker失败：{row.get('error') or detail}")
    if row["state"] == "interrupted":
        raise RuntimeError(f"训练worker意外中断：{row.get('error', '')}")
    return job_id


def _resume(store: Store, job_id: str, python: str) -> str:
    row = store.job(job_id)
    if row["state"] != "paused":
        raise ValueError("只能显式恢复已暂停的任务")
    directory = _job_dir(store, job_id)
    snapshot_path = directory / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text("utf-8"))
    if digest(snapshot) != row["snapshot_sha"]:
        raise ValueError("冻结训练快照SHA不符，拒绝恢复")
    checkpoint = directory / "resume.pt"
    audit_path = directory / "resume-audit.json"
    if not checkpoint.is_file() or not audit_path.is_file():
        raise ValueError("已暂停任务缺少可核验断点")
    audit = json.loads(audit_path.read_text("utf-8"))
    if audit.get("snapshot_sha") != row["snapshot_sha"] or audit.get("sha") != __import__("hashlib").sha256(checkpoint.read_bytes()).hexdigest():
        raise ValueError("恢复断点哈希核验失败")
    (directory / "worker.json").unlink(missing_ok=True)
    (directory / "pause").unlink(missing_ok=True)
    (directory / "cancel").unlink(missing_ok=True)
    (directory / "lease").touch()
    atomic_json(directory / "progress.json", {"phase": "queued", "resuming": True})
    with store.db() as db:
        db.execute("UPDATE jobs SET state='queued',pid=NULL,error='' WHERE id=? AND state='paused'", (job_id,))
    process = _spawn(store, job_id, python)
    row = _monitor(store, job_id, process)
    if row["state"] == "failed":
        log = directory / "worker.log"
        detail = log.read_text("utf-8", errors="replace")[-4000:] if log.exists() else ""
        raise RuntimeError(f"训练worker失败：{row.get('error') or detail}")
    if row["state"] != "complete":
        raise RuntimeError(f"恢复任务结束时的状态为 {row['state']}：{row.get('error', '')}")
    return job_id


def train_foreground(
    root: Path,
    *,
    mode: str = "incremental",
    device: str = "cpu",
    input_size: int = 320,
    character: str = "Bot自己",
    resume_job: str = "",
    worker_python: str = "",
) -> dict:
    if mode not in {"fresh", "incremental"} or device not in {"cpu", "cuda"}:
        raise ValueError("训练模式或设备无效")
    if input_size not in {224, 320, 448}:
        raise ValueError("图片尺寸必须为224、320或448")
    root = root.resolve()
    with DataLock(root):
        store = Store(root)
        _recover_or_reject_live_workers(store)
        python = _worker_python(store, worker_python, persist=bool(worker_python))
        job_id = _resume(store, resume_job, python) if resume_job else _start_new(store, mode, device, input_size, character, python)
    return {"job_id": job_id, "job": Store(root, initialize=False, readonly=True).job(job_id)}


def _run_validation(store: Store, version: str, python: str) -> dict:
    command = _worker_command(python, "validate", store.root, version)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            command,
            cwd=CORE_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            creationflags=flags,
            env=worker_environment(store.root),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("CPU模型加载核验超过45秒") from exc
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or f"核验worker exit={result.returncode}")[-4000:])
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise ValueError("核验worker没有返回有效JSON") from exc


def _activate_locked(store: Store, version: str, worker_python: str = "") -> dict:
    _recover_or_reject_live_workers(store)
    if not any(row["id"] == version for row in store.models()):
        raise ValueError("模型版本不存在")
    python = _worker_python(store, worker_python, persist=bool(worker_python))
    result = _run_validation(store, version, python)
    if result.get("status") != "ready" or result.get("version") != version:
        raise ValueError("CPU模型加载核验失败")
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT value FROM settings WHERE key='active'").fetchone()
        if old and old[0] != version:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('previous',?)", (old[0],))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)", (version,))
    return {"status": "activated", "version": version, "validation": result}


def activate(root: Path, version: str, worker_python: str = "") -> dict:
    root = root.resolve()
    with DataLock(root):
        return _activate_locked(Store(root), version, worker_python)


def rollback(root: Path, worker_python: str = "") -> dict:
    root = root.resolve()
    with DataLock(root):
        store = Store(root)
        previous = store.setting("previous")
        if not previous:
            raise ValueError("没有可回退的版本")
        return _activate_locked(store, previous, worker_python)


def _import_sources(store: Store, sources: list[tuple[Path, str]]) -> dict:
    results = {"imported": 0, "duplicates": 0, "label_conflicts": 0, "unlabelled_duplicates": 0, "excluded": 0, "errors": 0}
    files = []
    for folder, label in sources:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            files.append((path, label))
    items = []
    for source, label in files:
        staged = store.root / "uploads" / (uuid.uuid4().hex + source.suffix.lower())
        try:
            with source.open("rb") as incoming, staged.open("xb") as outgoing:
                copied = 0
                while chunk := incoming.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_BYTES:
                        raise ValueError("文件超过20MiB")
                    outgoing.write(chunk)
            result = store.ingest(staged, label)
            status_name = result.get("import_status")
            if status_name == "duplicate_label_conflict":
                results["label_conflicts"] += 1
            elif status_name == "duplicate_unlabelled":
                results["unlabelled_duplicates"] += 1
            elif result.get("already_exists"):
                results["duplicates"] += 1
            elif result.get("reason"):
                results["excluded"] += 1
            else:
                results["imported"] += 1
            items.append({"source": str(source), **result})
        except Exception as exc:
            staged.unlink(missing_ok=True)
            results["errors"] += 1
            items.append({"source": str(source), "error": str(exc)})
    results["files_seen"] = len(files)
    results["items"] = items
    return results


def import_folders(root: Path, self_dir: Path | None = None, other_dir: Path | None = None) -> dict:
    root = root.resolve()
    self_dir = Path(self_dir or root.parent / "raw" / "self")
    other_dir = Path(other_dir or root.parent / "raw" / "other")
    sources = [(self_dir.resolve(), "self"), (other_dir.resolve(), "other")]
    if sources[0][0] == sources[1][0]:
        raise ValueError("self与other图片目录不能相同")
    with DataLock(root):
        store = Store(root)
        _recover_or_reject_live_workers(store)
        with store.db() as db:
            store.register_catalog(db, "binary")
        return _import_sources(store, sources)


def import_named_folders(root: Path, source: Path) -> dict:
    """Import each immediate source child as a persistent multiclass label."""
    root = root.resolve()
    source = Path(source).expanduser()
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"多类别来源目录不存在或是符号链接：{source}")
    source = source.resolve()
    directories = [path for path in sorted(source.iterdir(), key=lambda p: p.name) if path.is_dir() and not path.is_symlink()]
    if not directories:
        raise ValueError("多类别来源目录下需要按角色名建立子目录")
    names = []
    for directory in directories:
        names.append(validate_class_name(directory.name))
    class_names = ordered_class_names(names)
    if len(class_names) != len(names):
        raise ValueError("多类别来源目录包含重复类别名称")
    by_folded = {}
    for name in names:
        folded = name.casefold()
        if folded in by_folded and by_folded[folded] != name:
            raise ValueError(f"类别名称大小写冲突：{by_folded[folded]} / {name}")
        by_folded[folded] = name
    sources = []
    for directory, label in zip(directories, names, strict=True):
        sources.append((directory.resolve(), label))
    with DataLock(root):
        store = Store(root)
        _recover_or_reject_live_workers(store)
        with store.db() as db:
            registry = store.register_catalog(db, "multiclass", class_names)
        results = _import_sources(store, sources)
    results["task"] = "multiclass"
    results["class_names"] = registry
    results["source_root"] = str(source)
    return results


def import_default_root(root: Path, source: Path) -> dict:
    """Import legacy self/other folders or discover named children by layout."""
    source = Path(source).expanduser()
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"默认图片目录不存在或是符号链接：{source}")
    directories = [path for path in source.iterdir() if path.is_dir() and not path.is_symlink()]
    names = {path.name.casefold() for path in directories}
    if directories and names <= {"self", "other"}:
        return import_folders(root, source / "self", source / "other")
    return import_named_folders(root, source)
