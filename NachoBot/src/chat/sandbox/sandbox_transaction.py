"""Crash-recoverable, all-or-rollback sandbox file transactions.

The manager's in-process transaction lock is the only writer coordination used
here. Deployments must keep one process as the owner of a sandbox storage root.
Immutable pre/post images remain in ``.transactions`` for outbox reconciliation.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

_JOURNAL = "journal.json"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HANDOFF_FIELDS = (
    "handoff_id",
    "idempotency_key",
    "stream_id",
    "platform",
    "group_id",
    "actor_id",
    "source_message_id",
    "candidate_fingerprint",
    "candidate_query",
    "file_edit_query",
    "created_at",
    "acknowledgement_fingerprint",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_directory(path: Path) -> None:
    """Best-effort directory metadata flush on platforms that expose it."""

    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except (OSError, AttributeError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_immutable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise RuntimeError("transaction image already exists with different bytes")
        return
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _write_journal(transaction_dir: Path, record: Mapping[str, Any]) -> None:
    journal = transaction_dir / _JOURNAL
    temporary = transaction_dir / f".{_JOURNAL}.{uuid.uuid4().hex}.tmp"
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal)
        _fsync_directory(transaction_dir)
    finally:
        temporary.unlink(missing_ok=True)


def _relative_attachment(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"images/[0-9]{4}\.(?:pre|post)", text):
        raise RuntimeError("invalid transaction attachment path")
    return text


def _handoff_record(handoff: object) -> dict[str, Any]:
    if not dataclasses.is_dataclass(handoff):
        raise RuntimeError("transaction requires a typed handoff")
    value = dataclasses.asdict(handoff)
    if tuple(value) != _HANDOFF_FIELDS:
        raise RuntimeError("handoff fields do not match transaction schema")
    if any(not isinstance(value[key], str) for key in _HANDOFF_FIELDS if key not in {"group_id", "created_at"}):
        raise RuntimeError("handoff identity fields must be strings")
    if value["group_id"] is not None and not isinstance(value["group_id"], str):
        raise RuntimeError("handoff group_id is invalid")
    if not isinstance(value["created_at"], (int, float)):
        raise RuntimeError("handoff creation time is invalid")
    return value


def _scope_record(scope: object) -> dict[str, Any]:
    return {
        "key": str(scope.key),
        "read_root": str(Path(scope.read_root).resolve(strict=False)),
        "write_root": str(Path(scope.write_root).resolve(strict=False)),
        "group_root": str(Path(scope.group_root).resolve(strict=False)) if scope.group_root is not None else None,
        "group_id": scope.group_id,
        "actor_id": str(scope.actor_id),
        "platform": str(scope.platform),
        "stream_id": str(scope.stream_id),
    }


def _transaction_dir(manager: object, handoff_id: str) -> Path:
    from src.chat.sandbox.sandbox_manager import validate_identifier

    relative = validate_identifier(handoff_id, "handoff_id")
    root = manager.storage_root / ".transactions"
    manager._check_storage_path(root)
    root.mkdir(parents=True, exist_ok=True)
    manager._check_storage_path(root)
    result = root / relative
    manager._check_storage_path(result)
    return result


def _load_record(manager: object, handoff_id: str, *, verify_images: bool = True) -> Optional[dict[str, Any]]:
    transaction_dir = _transaction_dir(manager, handoff_id)
    journal = transaction_dir / _JOURNAL
    manager._check_storage_path(journal)
    if not journal.is_file():
        return None
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("transaction journal is unreadable") from exc
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise RuntimeError("transaction journal schema is invalid")
    if record.get("status") not in {"prepared", "applying", "committed", "rolled_back"}:
        raise RuntimeError("transaction journal state is invalid")
    handoff = record.get("handoff")
    if not isinstance(handoff, dict) or handoff.get("handoff_id") != handoff_id:
        raise RuntimeError("transaction journal handoff binding is invalid")
    transaction_id = record.get("transaction_id")
    try:
        uuid.UUID(str(transaction_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("transaction id is invalid") from exc
    if set(handoff) != set(_HANDOFF_FIELDS):
        raise RuntimeError("transaction handoff manifest is invalid")
    scope = record.get("scope")
    if not isinstance(scope, dict) or set(scope) != {
        "key", "read_root", "write_root", "group_root", "group_id", "actor_id", "platform", "stream_id"
    }:
        raise RuntimeError("transaction scope manifest is invalid")
    outputs = record.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise RuntimeError("transaction output manifest is invalid")
    for output in outputs:
        if not isinstance(output, dict) or set(output) != {
            "path", "old_exists", "old_sha256", "backup_path", "new_sha256", "postimage_path"
        }:
            raise RuntimeError("transaction output entry is invalid")
        from src.chat.sandbox.sandbox_manager import validate_relative_path

        relative = validate_relative_path(output["path"])
        if relative != output["path"]:
            raise RuntimeError("transaction output path is not canonical")
        if not isinstance(output["old_exists"], bool):
            raise RuntimeError("transaction preimage state is invalid")
        new_hash = output.get("new_sha256")
        old_hash = output.get("old_sha256")
        if not isinstance(new_hash, str) or not _DIGEST.fullmatch(new_hash):
            raise RuntimeError("transaction postimage hash is invalid")
        if output["old_exists"]:
            if not isinstance(old_hash, str) or not _DIGEST.fullmatch(old_hash):
                raise RuntimeError("transaction preimage hash is invalid")
        elif old_hash is not None or output.get("backup_path") is not None:
            raise RuntimeError("transaction absent preimage manifest is invalid")
        for key, digest_key in (("postimage_path", "new_sha256"), ("backup_path", "old_sha256")):
            attachment = output.get(key)
            if attachment is None and key == "backup_path" and not output["old_exists"]:
                continue
            attachment = _relative_attachment(attachment)
            expected_suffix = ".post" if key == "postimage_path" else ".pre"
            if not attachment.endswith(expected_suffix):
                raise RuntimeError("transaction image kind is invalid")
            image = transaction_dir / Path(*PurePosixPath(attachment).parts)
            manager._check_storage_path(image)
            transaction_resolved = transaction_dir.resolve(strict=False)
            image_resolved = image.resolve(strict=False)
            try:
                image_resolved.relative_to(transaction_resolved)
            except ValueError as exc:
                raise RuntimeError("transaction image escapes its journal directory") from exc
            if verify_images and (not image.is_file() or _sha256(image.read_bytes()) != output[digest_key]):
                raise RuntimeError(f"transaction {digest_key} image is missing or corrupt")
    source_revision = record.get("source_revision")
    if not isinstance(source_revision, str) or not source_revision:
        raise RuntimeError("transaction source revision is invalid")
    return record


def _validated_scope(manager: object, record: Mapping[str, Any]) -> object:
    """Re-resolve scope from server identity; never trust journal roots alone."""

    handoff = record["handoff"]
    scope_record = record["scope"]
    try:
        from src.chat.sandbox.sandbox_handoff import SandboxEditHandoff

        typed_handoff = SandboxEditHandoff(**handoff)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("transaction handoff cannot be reconstructed") from exc
    if typed_handoff.handoff_id != record["handoff"]["handoff_id"] or not typed_handoff.binding_is_valid():
        raise RuntimeError("transaction handoff binding is invalid")
    scope = manager.get_scope(
        stream_id=handoff["stream_id"],
        platform=handoff["platform"],
        group_id=handoff["group_id"],
        actor_id=handoff["actor_id"],
    )
    if _scope_record(scope) != scope_record:
        raise RuntimeError("transaction scope identity no longer resolves to the recorded roots")
    expected_key = (
        f"group:{handoff['platform']}:{handoff['group_id']}"
        if handoff["group_id"]
        else f"private:{handoff['stream_id']}"
    )
    if scope.key != expected_key:
        raise RuntimeError("transaction scope key does not match handoff")
    return scope


def _target_for(manager: object, scope: object, relative: str) -> Path:
    target = scope.path_for_write(relative)
    manager._check_storage_path(target)
    return target


def _current_hash(manager: object, target: Path) -> Optional[str]:
    manager._check_storage_path(target)
    if not target.exists():
        return None
    if not target.is_file():
        raise RuntimeError("transaction target is not a regular file")
    return _sha256(target.read_bytes())


def _replace_from_image(manager: object, image: Path, target: Path) -> None:
    manager._check_storage_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    manager._check_storage_path(target.parent)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.txn")
    try:
        with image.open("rb") as source, temporary.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=64 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        manager._check_storage_path(temporary)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _rollback(manager: object, record: dict[str, Any], transaction_dir: Path, scope: object) -> list[str]:
    conflicts: list[str] = []
    for output in reversed(record["outputs"]):
        relative = output["path"]
        try:
            target = _target_for(manager, scope, relative)
            current_hash = _current_hash(manager, target)
        except (OSError, ValueError, RuntimeError) as exc:
            conflicts.append(f"{relative}: {type(exc).__name__}")
            continue
        old_hash = output["old_sha256"]
        if (output["old_exists"] and current_hash == old_hash) or (not output["old_exists"] and current_hash is None):
            continue
        if current_hash != output["new_sha256"]:
            conflicts.append(relative)
            continue
        try:
            if output["old_exists"]:
                backup = transaction_dir / Path(*PurePosixPath(output["backup_path"]).parts)
                _replace_from_image(manager, backup, target)
            else:
                target.unlink()
                _fsync_directory(target.parent)
        except (OSError, ValueError, RuntimeError) as exc:
            conflicts.append(f"{relative}: rollback_{type(exc).__name__}")
    record["recovery_conflicts"] = conflicts
    if not conflicts:
        record["status"] = "rolled_back"
    else:
        # Keep the durable state recoverable and block any future mutation.
        record["status"] = "applying"
    _write_journal(transaction_dir, record)
    return conflicts


def _recover_one(manager: object, handoff_id: str) -> Optional[dict[str, Any]]:
    record = _load_record(manager, handoff_id)
    if record is None or record["status"] in {"committed", "rolled_back"}:
        return record
    scope = _validated_scope(manager, record)
    with manager.mutation_lock(scope):
        # A prepared journal has no permission to have touched targets; the
        # same image guard handles it safely alongside interrupted apply.
        transaction_dir = _transaction_dir(manager, handoff_id)
        _rollback(manager, record, transaction_dir, scope)
    return record


def recover_transactions(manager: object) -> tuple[str, ...]:
    conflicts: list[str] = []
    with manager._transaction_lock:
        root = manager.storage_root / ".transactions"
        manager._check_storage_path(root)
        if not root.exists():
            return ()
        if not root.is_dir():
            raise RuntimeError("transaction root is not a directory")
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            manager._check_storage_path(child)
            if not child.is_dir():
                continue
            journal = child / _JOURNAL
            manager._check_storage_path(journal)
            if not journal.exists():
                # No atomic prepared marker means no target could have been
                # touched. Remove incomplete image preparation safely.
                shutil.rmtree(child)
                continue
            record = _load_record(manager, child.name, verify_images=False)
            if record is not None and record["status"] in {"committed", "rolled_back"}:
                continue
            record = _recover_one(manager, child.name)
            if record is not None and record.get("status") == "applying":
                conflicts.append(child.name)
    return tuple(conflicts)


def transaction_record(manager: object, handoff_id: str) -> Optional[dict[str, Any]]:
    with manager._transaction_lock:
        record = _load_record(manager, handoff_id)
        if record is None:
            return None
        _validated_scope(manager, record)
        transaction_dir = _transaction_dir(manager, handoff_id)
        result = dict(record)
        outputs = []
        for output in record["outputs"]:
            copied = dict(output)
            copied["postimage_path"] = str((transaction_dir / Path(*PurePosixPath(output["postimage_path"]).parts)).resolve())
            if output["backup_path"] is not None:
                copied["backup_path"] = str((transaction_dir / Path(*PurePosixPath(output["backup_path"]).parts)).resolve())
            outputs.append(copied)
        result["outputs"] = outputs
        return result


def commit_staged(
    manager: object,
    scope: object,
    handoff: object,
    staging_dir: Path,
    paths: object,
    source_revision: str,
) -> tuple[str, ...]:
    from src.chat.sandbox.sandbox_manager import MAX_TEXT_BYTES, SandboxPathError, validate_relative_path

    handoff_data = _handoff_record(handoff)
    handoff_id = handoff_data["handoff_id"]
    if not scope.key or not isinstance(source_revision, str) or not source_revision:
        raise ValueError("transaction scope or source revision is invalid")
    if isinstance(paths, (str, bytes)):
        selected = [paths]
    else:
        selected = list(paths)  # type: ignore[arg-type]
    normalized = tuple(dict.fromkeys(validate_relative_path(item) for item in selected))
    if not normalized:
        raise ValueError("transaction requires at least one path")

    manager._check_storage_path(staging_dir)
    manager._check_storage_path(scope.write_root)
    with manager._transaction_lock:
        conflicts = recover_transactions(manager)
        if conflicts:
            for conflict_id in conflicts:
                conflicted = _load_record(manager, conflict_id)
                if conflicted and conflicted.get("scope", {}).get("key") == scope.key:
                    raise RuntimeError("sandbox mutation blocked by unresolved transaction conflict")

        existing = _load_record(manager, handoff_id)
        if existing is not None:
            if existing["handoff"] != handoff_data or existing["scope"] != _scope_record(scope):
                raise RuntimeError("handoff already has a different transaction binding")
            if existing["status"] == "committed":
                return tuple(item["path"] for item in existing["outputs"])
            raise RuntimeError(f"handoff transaction is already {existing['status']}")

        transaction_dir = _transaction_dir(manager, handoff_id)
        if transaction_dir.exists() and any(transaction_dir.iterdir()):
            raise RuntimeError("handoff transaction directory contains unjournaled data")
        transaction_dir.mkdir(parents=True, exist_ok=True)
        manager._check_storage_path(transaction_dir)
        transaction_id = str(uuid.uuid4())
        outputs: list[dict[str, Any]] = []
        with manager.mutation_lock(scope):
            scope.ensure()
            if scope.revision() != source_revision:
                raise RuntimeError("sandbox revision changed before commit")
            for index, relative in enumerate(normalized):
                staged = staging_dir / Path(relative)
                manager._check_storage_path(staged)
                if not staged.is_file() or manager_path_is_reparse(staged):
                    raise RuntimeError(f"staged path missing: {relative}")
                new_data = staged.read_bytes()
                if len(new_data) > MAX_TEXT_BYTES:
                    raise ValueError(f"staged path exceeds text limit: {relative}")
                target = _target_for(manager, scope, relative)
                old_exists = target.exists()
                if old_exists and (not target.is_file() or manager_path_is_reparse(target)):
                    raise SandboxPathError(f"unsafe transaction target: {relative}")
                old_data = target.read_bytes() if old_exists else None
                prefix = f"images/{index:04d}"
                postimage_rel = f"{prefix}.post"
                backup_rel = f"{prefix}.pre" if old_exists else None
                _write_immutable(transaction_dir / postimage_rel, new_data)
                manager._check_storage_path(transaction_dir / postimage_rel)
                if old_data is not None:
                    _write_immutable(transaction_dir / backup_rel, old_data)
                    manager._check_storage_path(transaction_dir / backup_rel)
                outputs.append(
                    {
                        "path": relative,
                        "old_exists": old_exists,
                        "old_sha256": _sha256(old_data) if old_data is not None else None,
                        "backup_path": backup_rel,
                        "new_sha256": _sha256(new_data),
                        "postimage_path": postimage_rel,
                    }
                )
            if scope.revision() != source_revision:
                raise RuntimeError("sandbox revision changed while preparing commit")
            record: dict[str, Any] = {
                "schema_version": 1,
                "transaction_id": transaction_id,
                "handoff": handoff_data,
                "scope": _scope_record(scope),
                "source_revision": source_revision,
                "status": "prepared",
                "outputs": outputs,
                "recovery_conflicts": [],
            }
            _write_journal(transaction_dir, record)
            record["status"] = "applying"
            _write_journal(transaction_dir, record)
            try:
                for output in outputs:
                    target = _target_for(manager, scope, output["path"])
                    postimage = transaction_dir / Path(*PurePosixPath(output["postimage_path"]).parts)
                    _replace_from_image(manager, postimage, target)
                record["status"] = "committed"
                record["recovery_conflicts"] = []
                _write_journal(transaction_dir, record)
            except Exception as commit_error:
                try:
                    conflicts = _rollback(manager, record, transaction_dir, scope)
                except BaseException:
                    # Keep the applying journal and immutable images for a
                    # later process to resume idempotent rollback.
                    raise
                if conflicts:
                    raise RuntimeError("sandbox commit failed and rollback found conflicting target bytes") from commit_error
                manager.cleanup_staging(staging_dir)
                raise
        return normalized


def manager_path_is_reparse(path: Path) -> bool:
    from src.chat.sandbox.sandbox_manager import _is_reparse_or_symlink

    return _is_reparse_or_symlink(path)


__all__ = ["commit_staged", "recover_transactions", "transaction_record"]
