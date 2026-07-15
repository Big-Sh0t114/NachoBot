"""Startup and shutdown wiring for configured Focus groups."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any

from src.common.logger import get_logger
from src.config.config import global_config

from .coordinator import focus_coordinator
from .message_repository import latest_message_row_id, load_message_range_summary
from .models import (
    ChatKind,
    FocusEventSnapshot,
    FocusGroupDefinition,
    FocusMember,
    RestoredFocusEvent,
    StoredMessageRef,
)
from .scope_policy import ChatScopePolicy
from .storage.models import FocusEventRecord
from .storage.repository import FocusSQLiteStorage


logger = get_logger("focus.bootstrap")


class FocusBootstrap:
    def __init__(self) -> None:
        self._started = False
        self._storage: FocusSQLiteStorage | None = None
        self._registered_group_ids: list[str] = []

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> bool:
        config = global_config.focus
        if getattr(config, "mode", "off") == "observe":
            logger.warning("Focus observe mode is fail-safe no-op; Normal chat routing is preserved")
            return False

        if getattr(config, "mode", "off") == "off":
            logger.info("Focus mode is off")
            return False
        if self._started:
            return True

        storage = FocusSQLiteStorage()
        await storage.migrate()
        focus_coordinator.configure(
            policy=ChatScopePolicy(allow_group_to_private=config.allow_group_to_private),
            handoff_store=storage,
            state_store=storage,
            unread_event_threshold=config.unread_event_threshold,
            unviewed_event_seconds=config.unviewed_event_seconds,
            max_events_per_turn=config.max_events_per_prompt,
            switch_cooldown_seconds=config.switch_cooldown_seconds,
            reservation_ttl_seconds=config.reservation_ttl_seconds,
        )

        from src.chat.message_receive.chat_stream import get_chat_manager

        chat_manager = get_chat_manager()
        saved_states = {record.group_id: record for record in await storage.load_group_states()}

        coordinator_started = False
        try:
            for group_config in config.groups:
                definition, initial_chat_id, membership_hash = self._resolve_group(
                    group_config,
                    chat_manager,
                )
                latest_rows = {
                    member.chat_id: await asyncio.to_thread(
                        latest_message_row_id,
                        member.chat_id,
                    )
                    for member in definition.members
                }
                saved_state = saved_states.get(definition.group_id)
                cursor_records = {record.chat_id: record for record in await storage.load_cursors(definition.group_id)}
                now = time.time()
                if saved_state is not None:
                    active_chat_id = saved_state.active_chat_id
                    epoch = saved_state.epoch
                    if saved_state.membership_hash != membership_hash:
                        migration_mode = config.membership_migration
                        migrated = False
                        if migration_mode != "strict":
                            migrated = await storage.migrate_idle_group_membership(
                                group_id=definition.group_id,
                                expected_epoch=saved_state.epoch,
                                expected_membership_hash=saved_state.membership_hash,
                                new_membership_hash=membership_hash,
                                member_baselines=latest_rows,
                                fallback_active_chat_id=initial_chat_id,
                                allow_removals=migration_mode == "idle_safe",
                            )
                        if not migrated:
                            raise RuntimeError(
                                f"Focus group {definition.group_id!r} membership changed; "
                                f"membership_migration={migration_mode!r} could not migrate live or incompatible state"
                            )
                        old_chat_ids = set(cursor_records)
                        added_chat_ids = sorted(set(latest_rows) - old_chat_ids)
                        removed_chat_ids = sorted(old_chat_ids - set(latest_rows))
                        if active_chat_id not in latest_rows:
                            active_chat_id = initial_chat_id
                        epoch += 1
                        cursor_records = {
                            record.chat_id: record for record in await storage.load_cursors(definition.group_id)
                        }
                        logger.warning(
                            f"Focus group {definition.group_id!r} safely migrated idle membership; "
                            f"mode={migration_mode}, added={added_chat_ids}, removed={removed_chat_ids}, "
                            f"active={active_chat_id!r}, epoch={epoch}"
                        )
                    if active_chat_id is None:
                        active_chat_id = initial_chat_id
                        epoch = max(1, epoch + 1)
                        await storage.save_group_state(
                            definition.group_id,
                            active_chat_id,
                            epoch,
                            membership_hash,
                        )
                    if active_chat_id is not None and active_chat_id not in latest_rows:
                        raise RuntimeError(
                            f"Persisted active chat {active_chat_id!r} is outside Focus group {definition.group_id!r}"
                        )
                    cursors = {
                        chat_id: cursor_records[chat_id].processed_row_id if chat_id in cursor_records else latest_row
                        for chat_id, latest_row in latest_rows.items()
                    }
                else:
                    active_chat_id = initial_chat_id
                    epoch = 1 if active_chat_id is not None else 0
                    cursors = dict(latest_rows)
                    await storage.save_group_state(
                        definition.group_id,
                        active_chat_id,
                        epoch,
                        membership_hash,
                    )

                last_viewed_at = {
                    chat_id: cursor_records[chat_id].last_viewed_at if chat_id in cursor_records else now
                    for chat_id in latest_rows
                }
                for chat_id, cursor in cursors.items():
                    await storage.save_cursor(
                        definition.group_id,
                        chat_id,
                        cursor,
                        last_viewed_at[chat_id],
                    )

                restored_events = await self._restore_events(
                    storage=storage,
                    definition=definition,
                    active_chat_id=active_chat_id,
                    cursors=cursors,
                    latest_rows=latest_rows,
                )
                focus_coordinator.register_group(
                    definition,
                    active_chat_id=active_chat_id,
                    epoch=epoch,
                    cursors=cursors,
                    latest_rows=latest_rows,
                    last_viewed_at=last_viewed_at,
                    restored_events=restored_events,
                    membership_hash=membership_hash,
                )
                self._registered_group_ids.append(definition.group_id)

            from src.chat.heart_flow.heartflow import heartflow

            async def ensure_runtime(chat_id: str) -> None:
                runtime = await heartflow.get_or_create_heartflow_chat(chat_id)
                if runtime is None:
                    raise RuntimeError(f"Cannot prepare Focus runtime for {chat_id}")

            focus_coordinator.set_ensure_runtime_callback(ensure_runtime)
            await focus_coordinator.start()
            coordinator_started = True
            for chat_id in focus_coordinator.pending_runtime_chat_ids():
                await ensure_runtime(chat_id)
            self._storage = storage
            self._started = True
            logger.info(f"Focus started in {config.mode} mode with {len(self._registered_group_ids)} group(s)")
            return True
        except Exception:
            if coordinator_started:
                try:
                    await focus_coordinator.stop()
                except Exception as stop_error:
                    logger.error(f"Focus startup rollback failed: {stop_error}")
            for group_id in reversed(self._registered_group_ids):
                focus_coordinator.unregister_group(group_id)
            self._registered_group_ids.clear()
            raise

    async def begin_shutdown(self) -> None:
        if self._started:
            await focus_coordinator.begin_shutdown()

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            await focus_coordinator.stop()
        finally:
            for group_id in reversed(self._registered_group_ids):
                focus_coordinator.unregister_group(group_id)
            self._registered_group_ids.clear()
            focus_coordinator.set_ensure_runtime_callback(None)
            self._storage = None
            self._started = False
        logger.info("Focus stopped")

    @staticmethod
    async def _restore_events(
        *,
        storage: FocusSQLiteStorage,
        definition: FocusGroupDefinition,
        active_chat_id: str | None,
        cursors: dict[str, int],
        latest_rows: dict[str, int],
    ) -> tuple[RestoredFocusEvent, ...]:
        records = {record.chat_id: record for record in await storage.load_pending_events(definition.group_id)}
        member_ids = {member.chat_id for member in definition.members}
        unknown = set(records) - member_ids
        if unknown:
            raise RuntimeError(
                f"Focus group {definition.group_id!r} has pending events for unknown chats: {sorted(unknown)!r}"
            )

        restored: list[RestoredFocusEvent] = []
        for member in definition.members:
            chat_id = member.chat_id
            record = records.get(chat_id)
            if chat_id == active_chat_id:
                if record is not None:
                    raise RuntimeError(f"Focus pending event {record.event_id!r} targets active chat {chat_id!r}")
                continue

            after_row_id = record.last_row_id if record is not None else cursors[chat_id]
            summary = None
            if latest_rows[chat_id] > after_row_id:
                summary = await asyncio.to_thread(
                    load_message_range_summary,
                    chat_id,
                    after_row_id,
                    latest_rows[chat_id],
                )

            if record is None and summary is None:
                continue
            if record is None:
                assert summary is not None
                snapshot = FocusEventSnapshot(
                    event_id=f"evt_{uuid.uuid4().hex}",
                    revision=summary.unread_count,
                    target_chat_id=chat_id,
                    display_name=member.display_name or chat_id,
                    unread_count=summary.unread_count,
                    first_unread=summary.first_message,
                    last_unread=summary.last_message,
                    is_mentioned=summary.has_mention,
                    is_at=summary.has_at,
                    latest_preview=summary.latest_preview,
                )
                delivered_revision = 0
                visible = False
            else:
                snapshot = FocusBootstrap._event_snapshot(record, member.display_name or chat_id)
                delivered_revision = min(record.last_delivered_revision, record.revision)
                visible = record.visible
                if summary is not None:
                    snapshot = FocusEventSnapshot(
                        event_id=record.event_id,
                        revision=record.revision + summary.unread_count,
                        target_chat_id=chat_id,
                        display_name=member.display_name or chat_id,
                        unread_count=record.unread_count + summary.unread_count,
                        first_unread=snapshot.first_unread,
                        last_unread=summary.last_message,
                        is_mentioned=record.has_mention or summary.has_mention,
                        is_at=record.has_at or summary.has_at,
                        latest_preview=summary.latest_preview,
                    )

            may_emit = bool(
                active_chat_id
                and focus_coordinator.policy.can_emit_event(
                    definition,
                    active_chat_id,
                    chat_id,
                )
            )
            visible = (
                visible
                or may_emit
                and (
                    snapshot.is_mentioned
                    or snapshot.is_at
                    or snapshot.unread_count >= global_config.focus.unread_event_threshold
                )
            )
            event = RestoredFocusEvent(snapshot, delivered_revision, visible)
            if record is None or summary is not None or visible != record.visible:
                await storage.upsert_event(
                    definition.group_id,
                    snapshot,
                    last_delivered_revision=delivered_revision,
                    visible=visible,
                )
            restored.append(event)
        return tuple(restored)

    @staticmethod
    def _event_snapshot(record: FocusEventRecord, display_name: str) -> FocusEventSnapshot:
        return FocusEventSnapshot(
            event_id=record.event_id,
            revision=record.revision,
            target_chat_id=record.chat_id,
            display_name=display_name,
            unread_count=record.unread_count,
            first_unread=StoredMessageRef(
                record.first_row_id,
                record.chat_id,
                f"focus-row-{record.first_row_id}",
                record.created_at,
            ),
            last_unread=StoredMessageRef(
                record.last_row_id,
                record.chat_id,
                f"focus-row-{record.last_row_id}",
                record.updated_at,
            ),
            is_mentioned=record.has_mention,
            is_at=record.has_at,
            latest_preview=record.latest_preview,
        )

    @staticmethod
    def _resolve_group(
        group_config: Any, chat_manager: Any
    ) -> tuple[
        FocusGroupDefinition,
        str | None,
        str,
    ]:
        members: list[FocusMember] = []
        aliases: dict[str, str] = {}
        identity_rows: list[dict[str, Any]] = []

        for member_config in group_config.members:
            is_group = member_config.kind == "group"
            chat_id = chat_manager.get_stream_id(
                member_config.platform,
                str(member_config.external_id),
                is_group=is_group,
            )
            stream = chat_manager.get_stream(chat_id)
            if stream is None:
                raise RuntimeError(
                    f"Focus member {member_config.key!r} has no stored ChatStream "
                    f"({member_config.platform}:{member_config.external_id})"
                )
            actual_kind = ChatKind.GROUP if stream.group_info else ChatKind.PRIVATE
            expected_kind = ChatKind(member_config.kind)
            if actual_kind is not expected_kind:
                raise RuntimeError(
                    f"Focus member {member_config.key!r} kind mismatch: "
                    f"configured={expected_kind.value}, actual={actual_kind.value}"
                )
            display_name = member_config.display_name or FocusBootstrap._stream_name(
                stream,
                member_config.key,
            )
            members.append(
                FocusMember(
                    chat_id=chat_id,
                    kind=expected_kind,
                    display_name=display_name,
                    allow_import=member_config.allow_import,
                    allow_export=member_config.allow_export,
                    platform=str(stream.platform or member_config.platform),
                )
            )
            aliases[member_config.key] = chat_id
            identity_rows.append(
                {
                    "chat_id": chat_id,
                    "kind": expected_kind.value,
                    "allow_import": bool(member_config.allow_import),
                    "allow_export": bool(member_config.allow_export),
                }
            )

        initial_chat_id = aliases.get(group_config.initial_member)
        if initial_chat_id is None:
            initial_chat_id = next(member.chat_id for member in members if member.kind is ChatKind.GROUP)
        definition = FocusGroupDefinition(
            group_id=group_config.id,
            members=tuple(members),
            initial_chat_id=initial_chat_id,
        )
        membership_hash = hashlib.sha256(
            json.dumps(
                identity_rows,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return definition, initial_chat_id, membership_hash

    @staticmethod
    def _stream_name(stream: Any, fallback: str) -> str:
        if stream.group_info is not None:
            return str(getattr(stream.group_info, "group_name", "") or fallback)
        return str(getattr(stream.user_info, "user_nickname", "") or fallback)


focus_bootstrap = FocusBootstrap()
