"""Startup and shutdown wiring for configured Focus groups."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from src.common.logger import get_logger
from src.config.config import global_config

from .coordinator import focus_coordinator
from .message_repository import latest_message_row_id
from .models import ChatKind, FocusGroupDefinition, FocusMember
from .scope_policy import ChatScopePolicy
from .storage.models import FocusStartupGroupState
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
        coordinator_started = False
        try:
            resolved_groups: list[tuple[FocusGroupDefinition, str | None, str, dict[str, int]]] = []
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
                resolved_groups.append((definition, initial_chat_id, membership_hash, latest_rows))

            reset_result = await storage.reset_for_startup(
                tuple(
                    FocusStartupGroupState(
                        group_id=definition.group_id,
                        initial_chat_id=initial_chat_id,
                        membership_hash=membership_hash,
                        member_baselines=latest_rows,
                    )
                    for definition, initial_chat_id, membership_hash, latest_rows in resolved_groups
                )
            )
            retired_count = (
                reset_result.pending_events_expired
                + reset_result.active_handoffs_expired
                + reset_result.reservations_released
            )
            log = logger.warning if retired_count else logger.info
            log(
                "Focus startup reset previous runtime state; "
                f"pending_events={reset_result.pending_events_expired}, "
                f"active_handoffs={reset_result.active_handoffs_expired}, "
                f"reservations={reset_result.reservations_released}"
            )

            now = time.time()
            for definition, active_chat_id, membership_hash, latest_rows in resolved_groups:
                epoch = 1 if active_chat_id is not None else 0
                cursors = dict(latest_rows)
                last_viewed_at = {chat_id: now for chat_id in latest_rows}
                focus_coordinator.register_group(
                    definition,
                    active_chat_id=active_chat_id,
                    epoch=epoch,
                    cursors=cursors,
                    latest_rows=latest_rows,
                    last_viewed_at=last_viewed_at,
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
            for chat_id in focus_coordinator.active_runtime_chat_ids():
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
                    planner_bypass=member_config.planner_bypass,
                )
            )
            aliases[member_config.key] = chat_id
            identity_rows.append(
                {
                    "chat_id": chat_id,
                    "kind": expected_kind.value,
                    "allow_import": bool(member_config.allow_import),
                    "allow_export": bool(member_config.allow_export),
                    "planner_bypass": bool(member_config.planner_bypass),
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
