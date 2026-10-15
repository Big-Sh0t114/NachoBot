"""Thin binding from the shared QQCall runtime to NapCat's current objects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from qqcall_client.qqcall import QQCallConfig, QQCallRuntime
from qqcall_client.qqcall_dependencies import make_dependency_check
from qqcall_client.qqcall_diagnostics import make_qqcall_status_callback

from src.logger import logger


_runtime: QQCallRuntime | None = None


def _connection_open(connection: Any) -> bool:
    if connection is None:
        return False
    closed = getattr(connection, "closed", None)
    if isinstance(closed, bool):
        return not closed
    opened = getattr(connection, "open", None)
    if isinstance(opened, bool):
        return opened
    state = getattr(connection, "state", None)
    if state is not None:
        return str(state).lower() != "closed"
    return True


def create_napcat_qqcall_runtime(
    config: Any,
    *,
    router: Any,
    message_handler: Any,
    platform: str = "qq",
    on_status=None,
    audio_route_verifier=None,
) -> QQCallRuntime:
    """Build the shared client around the already-owned NapCat and Core routes."""

    status_callback = make_qqcall_status_callback(
        adapter="napcat",
        logger=logger,
        downstream=on_status,
    )

    if hasattr(config, "to_runtime_config"):
        runtime_config = config.to_runtime_config()
    else:
        runtime_config = QQCallConfig.from_mapping(config if isinstance(config, Mapping) else {})
    async def get_platform_state() -> dict[str, Any]:
        connection = getattr(message_handler, "server_connection", None)
        account_id = getattr(message_handler, "verified_account_id", None)
        generation = getattr(message_handler, "connection_generation", None)
        account = account_id if isinstance(account_id, str) else ""
        identity = (id(connection), generation, account) if connection is not None and generation else ()
        try:
            core_ready = router.check_connection(platform) is True
        except Exception:
            core_ready = False
        return {
            "connected": bool(connection is not None and account and identity and _connection_open(connection)),
            "core_ready": core_ready,
            "account_id": account,
            "identity": identity,
        }

    async def send_message(message):
        return await router.send_message(message)

    async def resolve_peer_info(peer_user_id: str):
        connection = getattr(message_handler, "server_connection", None)
        generation = getattr(message_handler, "connection_generation", None)
        account = getattr(message_handler, "verified_account_id", None)
        def current():
            return (connection is not None and _connection_open(connection)
                    and message_handler.server_connection is connection
                    and message_handler.connection_generation == generation
                    and account == runtime_config.account_id
                    and message_handler.verified_account_id == account)
        if not current():
            return None
        async def pre_send_check():
            return current()
        from src.send_handler.nc_sending import nc_message_sender
        response = await nc_message_sender.send_message_to_napcat(
            "get_stranger_info", {"user_id": int(peer_user_id)}, timeout_sec=2.0,
            expected_connection=connection, pre_send_check=pre_send_check, identity_check=current,
        )
        if (not current() or not isinstance(response, Mapping) or response.get("status") != "ok"
                or response.get("retcode", 0) != 0):
            return None
        return response.get("data")

    async def send_custom(message_type_name: str, message: dict[str, Any]):
        return await router.send_custom_message(
            platform=platform,
            message_type_name=message_type_name,
            message=message,
        )

    async def admit_peer(peer_user_id: str) -> bool:
        try:
            peer_uin = int(peer_user_id)
        except (TypeError, ValueError):
            return False
        # Use the adapter's existing private whitelist/global-ban policy with
        # its normal checks enabled; QQCall cannot opt out of admission rules.
        return await message_handler.check_allow_to_chat(peer_uin, None)

    async def admit_group(peer_user_id: str, group_id: str) -> bool:
        try:
            peer_uin = int(peer_user_id)
            group_uin = int(group_id)
        except (TypeError, ValueError):
            return False
        return await message_handler.check_allow_to_chat(peer_uin, group_uin)

    async def send_busy_private(peer_user_id: str, text: str, runtime_preflight) -> bool:
        return await send_call_notice(peer_user_id, None, text, runtime_preflight)

    async def send_call_notice(peer_user_id: str, group_id: str | None, text: str, runtime_preflight) -> bool:
        try:
            peer_uin = int(peer_user_id)
            group_uin = int(group_id) if group_id is not None else None
        except (TypeError, ValueError):
            return False
        connection = getattr(message_handler, "server_connection", None)
        generation = getattr(message_handler, "connection_generation", None)
        account = getattr(message_handler, "verified_account_id", None)
        if (
            connection is None
            or not _connection_open(connection)
            or not isinstance(account, str)
            or not account
            or generation is None
        ):
            return False
        expected_identity = (id(connection), generation, account)

        def is_current() -> bool:
            current_connection = getattr(message_handler, "server_connection", None)
            current_account = getattr(message_handler, "verified_account_id", None)
            current_generation = getattr(message_handler, "connection_generation", None)
            return (
                current_connection is connection
                and _connection_open(current_connection)
                and current_generation == generation
                and current_account == account
                and expected_identity == (id(current_connection), current_generation, current_account)
            )

        async def pre_send_check() -> bool:
            if not is_current() or not await message_handler.check_allow_to_chat(peer_uin, group_uin):
                return False
            if await runtime_preflight() is not True:
                return False
            return is_current() and await message_handler.check_allow_to_chat(peer_uin, group_uin)

        if not is_current() or not await message_handler.check_allow_to_chat(peer_uin, group_uin):
            return False
        try:
            from src.send_handler.nc_sending import nc_message_sender

            response = await nc_message_sender.send_message_to_napcat(
                "send_private_msg" if group_uin is None else "send_group_msg",
                {("user_id" if group_uin is None else "group_id"): peer_uin if group_uin is None else group_uin,
                 "message": [{"type": "text", "data": {"text": text}}]},
                timeout_sec=12.0,
                expected_connection=connection,
                pre_send_check=pre_send_check,
                identity_check=is_current,
            )
        except Exception:
            return False
        if not is_current() or not await message_handler.check_allow_to_chat(peer_uin, group_uin):
            return False
        return bool(
            isinstance(response, Mapping)
            and response.get("status") == "ok"
            and type(response.get("retcode", 0)) is int
            and response.get("retcode", 0) in (0, 1)
        )

    return QQCallRuntime(
        runtime_config,
        platform,
        send_message,
        send_custom,
        get_platform_state,
        admit_peer,
        send_busy_private=send_busy_private,
        admit_group=admit_group,
        dependency_check=make_dependency_check(router, platform, runtime_config.universal_api),
        send_call_notice=send_call_notice,
        on_status=status_callback,
        audio_route_verifier=audio_route_verifier,
        resolve_peer_info=resolve_peer_info,
    )


def configure_qqcall_runtime(config: Any, *, router: Any, message_handler: Any, platform: str = "qq") -> QQCallRuntime:
    global _runtime
    _runtime = create_napcat_qqcall_runtime(
        config,
        router=router,
        message_handler=message_handler,
        platform=platform,
    )
    return _runtime


def get_qqcall_runtime() -> QQCallRuntime | None:
    return _runtime


def clear_qqcall_runtime(runtime: QQCallRuntime | None = None) -> None:
    global _runtime
    if runtime is None or _runtime is runtime:
        _runtime = None


__all__ = [
    "clear_qqcall_runtime",
    "configure_qqcall_runtime",
    "create_napcat_qqcall_runtime",
    "get_qqcall_runtime",
]
