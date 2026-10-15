"""Thin binding from the shared QQCall runtime to SnowLuma's current socket."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from qqcall_client.qqcall import QQCallConfig, QQCallRuntime
from qqcall_client.qqcall_dependencies import make_dependency_check
from qqcall_client.qqcall_diagnostics import make_qqcall_status_callback

from .logger import logger


def create_snowluma_qqcall_runtime(
    config: Any,
    *,
    router: Any,
    client: Any,
    admission_check,
    platform: str = "qq",
    on_status=None,
    audio_route_verifier=None,
) -> QQCallRuntime:
    status_callback = make_qqcall_status_callback(
        adapter="snowluma",
        logger=logger,
        downstream=on_status,
    )
    if isinstance(config, QQCallConfig):
        runtime_config = config
    else:
        runtime_config = QQCallConfig.from_mapping(config if isinstance(config, Mapping) else {})
    async def get_platform_state() -> dict[str, Any]:
        snapshot = client.connection_snapshot()
        if snapshot is None:
            generation = getattr(client, "connection_generation", None)
            account = getattr(client, "connected_account_id", "")
            identity = (None, generation, account) if generation is not None else ()
            return {"connected": False, "account_id": account if isinstance(account, str) else "", "identity": identity}
        websocket, generation, account = snapshot
        try:
            core_ready = router.check_connection(platform) is True
        except Exception:
            core_ready = False
        current = client.connection_is_current(snapshot)
        return {
            "connected": bool(current and isinstance(account, str) and account),
            "core_ready": core_ready,
            "account_id": account,
            "identity": (id(websocket), generation, account),
        }

    async def send_message(message):
        return await router.send_message(message)

    async def resolve_peer_info(peer_user_id: str):
        snapshot = client.connection_snapshot()
        if (snapshot is None or snapshot[2] != runtime_config.account_id
                or not client.connection_is_current(snapshot)):
            return None
        response = await client.call_action(
            "get_stranger_info", {"user_id": int(peer_user_id)}, timeout_sec=2.0,
            expected_connection=snapshot,
            pre_send_check=lambda: client.connection_is_current(snapshot),
        )
        if (not client.connection_is_current(snapshot) or not isinstance(response, Mapping)
                or response.get("status") != "ok" or response.get("retcode", 0) != 0):
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
        allowed, _reason = await admission_check(peer_uin, None)
        return allowed is True

    async def admit_group(peer_user_id: str, group_id: str) -> bool:
        try:
            peer_uin = int(peer_user_id)
            group_uin = int(group_id)
        except (TypeError, ValueError):
            return False
        allowed, _reason = await admission_check(peer_uin, group_uin)
        return allowed is True

    async def send_busy_private(peer_user_id: str, text: str, runtime_preflight) -> bool:
        return await send_call_notice(peer_user_id, None, text, runtime_preflight)

    async def send_call_notice(peer_user_id: str, group_id: str | None, text: str, runtime_preflight) -> bool:
        try:
            peer_uin = int(peer_user_id)
            group_uin = int(group_id) if group_id is not None else None
        except (TypeError, ValueError):
            return False
        snapshot = client.connection_snapshot()
        if (
            snapshot is None
            or len(snapshot) < 3
            or snapshot[2] != runtime_config.account_id
            or not client.connection_is_current(snapshot)
        ):
            return False

        async def pre_send_check() -> bool:
            async def admitted():
                return await admit_peer(peer_user_id) if group_id is None else await admit_group(peer_user_id, group_id)
            if not client.connection_is_current(snapshot) or not await admitted():
                return False
            if await runtime_preflight() is not True:
                return False
            return client.connection_is_current(snapshot) and await admitted()

        if not (await admit_peer(peer_user_id) if group_id is None else await admit_group(peer_user_id, group_id)):
            return False
        try:
            response = await client.call_action(
                "send_private_msg" if group_uin is None else "send_group_msg",
                {("user_id" if group_uin is None else "group_id"): peer_uin if group_uin is None else group_uin,
                 "message": [{"type": "text", "data": {"text": text}}]},
                timeout_sec=12.0,
                expected_connection=snapshot,
                pre_send_check=pre_send_check,
            )
        except Exception:
            return False
        if not client.connection_is_current(snapshot) or not (await admit_peer(peer_user_id) if group_id is None else await admit_group(peer_user_id, group_id)):
            return False
        if not isinstance(response, Mapping) or response.get("status") != "ok":
            return False
        retcode = response.get("retcode", 0)
        return type(retcode) is int and retcode in {0, 1}

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


__all__ = ["create_snowluma_qqcall_runtime"]
