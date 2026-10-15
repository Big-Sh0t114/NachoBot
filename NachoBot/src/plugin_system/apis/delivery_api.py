"""Plugin-owned outbound transports, selected by an opaque delivery target.

Core owns dispatch and failure semantics; plugins own protocol validation and
receipts. Unregistered routes remain reserved until process exit so an in-flight
reply cannot silently fall back to ordinary transport after plugin unload.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.chat.runtime_capabilities import additional_config_from_message


class DeliveryRejected(RuntimeError):
    """Delivery is rejected or uncertain; replaying audio is unsafe."""

    fallback_allowed = False


@dataclass(frozen=True)
class _Provider:
    send: Callable[[Any, Any], Awaitable[bool]]
    enabled: Callable[[], bool]
    require_audio_success: bool


_providers: dict[tuple[str, str], _Provider | None] = {}


def register_delivery_provider(
    platform: str, kind: str, send, *, enabled, require_audio_success: bool = True,
) -> None:
    """Register an enabled plugin's route; repeated identical registration is safe."""
    if not isinstance(platform, str) or not platform or not isinstance(kind, str) or not kind:
        raise ValueError("Delivery route requires a platform and kind")
    if not callable(send) or not callable(enabled) or type(require_audio_success) is not bool:
        raise ValueError("Invalid delivery provider")
    provider = _Provider(send, enabled, require_audio_success)
    key = (platform, kind)
    if _providers.get(key) != provider:
        _providers[key] = provider


def unregister_delivery_provider(platform: str, kind: str) -> None:
    """Revoke a route without permitting legacy transport fallback."""
    _providers[(platform, kind)] = None


def _route(message) -> tuple[str, str] | None:
    target = additional_config_from_message(message).get("delivery_target")
    platform = getattr(getattr(message, "message_info", None), "platform", None)
    kind = target.get("kind") if isinstance(target, Mapping) else None
    if isinstance(platform, str) and isinstance(kind, str):
        return platform, kind
    return None


def requires_audio_success(message) -> bool:
    key = _route(message)
    if key not in _providers:
        return False
    provider = _providers[key]
    return provider is None or provider.require_audio_success


async def send_message(api, message) -> bool:
    key = _route(message)
    if key not in _providers:
        return (await api.send_message(message)) is not False
    provider = _providers[key]
    if provider is None:
        raise DeliveryRejected("delivery_provider_unavailable")
    try:
        if provider.enabled() is not True:
            raise DeliveryRejected("delivery_provider_disabled")
        result = await provider.send(api, message)
        if _providers.get(key) is not provider or provider.enabled() is not True:
            raise DeliveryRejected("delivery_provider_changed")
        if result is not True:
            raise DeliveryRejected("delivery_not_confirmed")
        return True
    except DeliveryRejected:
        raise
    except Exception:
        # Provider failures must not turn into buffered retries after delivery
        # may already have begun. Cancellation propagates unchanged.
        raise DeliveryRejected("delivery_provider_failed") from None
