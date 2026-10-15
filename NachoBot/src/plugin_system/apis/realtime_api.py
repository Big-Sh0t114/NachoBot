"""Plugin-facing registration for provider-owned realtime ingress proofs."""

from __future__ import annotations

from src.chat.realtime_ingress import (
    RealtimeIngressManager,
    RealtimeLifecycleChange,
    RealtimeProvider,
    RealtimeSession,
    realtime_ingress_manager,
)


def register_realtime_provider(
    name: str,
    verifier,
    *,
    lifecycle_verifier=None,
    enabled=lambda: True,
) -> None:
    """Register the plugin's proof callbacks with Core's generic lease manager."""

    realtime_ingress_manager.register_provider(
        RealtimeProvider(
            name=name,
            verify=verifier,
            lifecycle=lifecycle_verifier,
            enabled=enabled,
        )
    )


def unregister_realtime_provider(name: str) -> None:
    realtime_ingress_manager.unregister_provider(name)


__all__ = [
    "RealtimeIngressManager",
    "RealtimeLifecycleChange",
    "RealtimeProvider",
    "RealtimeSession",
    "register_realtime_provider",
    "unregister_realtime_provider",
]
