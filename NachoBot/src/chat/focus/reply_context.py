"""Typed Replyer boundary for Focus handoff acquisition and lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Iterable, Protocol

from src.common.logger import get_logger

from .handoff_store import HandoffStore
from .models import FocusHandoff, FocusLease


logger = get_logger("focus_reply_context")


class ReplyContextMode(str, Enum):
    NONE = "none"
    ACQUIRE = "acquire"
    REUSE = "reuse"


class ReplyContextPurpose(str, Enum):
    PRIMARY_REPLY = "primary_reply"
    FILE_EDIT = "file_edit"
    TOOL_FOLLOWUP = "tool_followup"


@dataclass(frozen=True, slots=True)
class ReplyContextRef:
    provider: str
    handoff_id: str
    reservation_id: str
    target_chat_id: str
    focus_group_id: str
    focus_epoch: int
    revision: int
    cycle_id: str
    lease_expires_at: float


@dataclass(frozen=True, slots=True)
class ReplyContextRequest:
    mode: ReplyContextMode = ReplyContextMode.NONE
    purpose: ReplyContextPurpose = ReplyContextPurpose.PRIMARY_REPLY
    target_chat_id: str = ""
    lease: FocusLease | None = None
    cycle_id: str = ""
    provider: str = "focus"
    reuse_refs: tuple[ReplyContextRef, ...] = ()
    max_prompt_tokens: int = 512

    def __post_init__(self) -> None:
        if self.mode is ReplyContextMode.NONE:
            return
        if not self.target_chat_id:
            raise ValueError("ReplyContextRequest target_chat_id is required")
        if self.lease is None:
            raise ValueError("ReplyContextRequest lease is required")
        if self.lease.chat_id != self.target_chat_id:
            raise ValueError("ReplyContextRequest target must match its Focus lease")
        if self.lease.epoch <= 0:
            raise ValueError("ReplyContextRequest Focus epoch must be positive")
        if not self.cycle_id:
            raise ValueError("ReplyContextRequest cycle_id is required")
        if self.max_prompt_tokens < 128 or self.max_prompt_tokens > 768:
            raise ValueError("ReplyContextRequest max_prompt_tokens must be within 128..768")
        if self.mode is ReplyContextMode.REUSE and not self.reuse_refs:
            raise ValueError("REUSE requires at least one ReplyContextRef")


@dataclass(frozen=True, slots=True)
class ReplyPromptContext:
    target_chat_id: str
    focus_epoch: int
    focus_handoff_block: str = ""
    context_refs: tuple[ReplyContextRef, ...] = ()
    injection_detected: bool = False
    estimated_tokens: int = 0
    digest: str = ""

    @classmethod
    def empty(cls, target_chat_id: str = "") -> "ReplyPromptContext":
        return cls(target_chat_id=target_chat_id, focus_epoch=0)


@dataclass(frozen=True, slots=True)
class ReplyContextMaterial:
    handoffs: tuple[FocusHandoff, ...]
    refs: tuple[ReplyContextRef, ...]


class ReplyContextProvider(Protocol):
    name: str

    async def acquire(self, request: ReplyContextRequest) -> ReplyContextMaterial | None: ...

    async def reuse(self, request: ReplyContextRequest) -> ReplyContextMaterial | None: ...

    async def validate_context(self, request: ReplyContextRequest, material: ReplyContextMaterial) -> bool: ...

    async def release(self, refs: tuple[ReplyContextRef, ...], reason: str) -> None: ...

    async def acknowledge(self, refs: tuple[ReplyContextRef, ...], delivery_id: str) -> bool: ...


class ReplyContextError(RuntimeError):
    pass


class ReplyContextUnavailableError(ReplyContextError):
    pass


class ReplyContextValidationError(ReplyContextError):
    pass


_providers: dict[str, ReplyContextProvider] = {}


def acquire_reply_context_request(
    lease: FocusLease,
    cycle_id: str,
    *,
    purpose: ReplyContextPurpose = ReplyContextPurpose.PRIMARY_REPLY,
    max_prompt_tokens: int = 512,
) -> ReplyContextRequest:
    """Build an ACQUIRE request whose target can only come from the server lease."""

    return ReplyContextRequest(
        mode=ReplyContextMode.ACQUIRE,
        purpose=purpose,
        target_chat_id=lease.chat_id,
        lease=lease,
        cycle_id=cycle_id,
        max_prompt_tokens=max_prompt_tokens,
    )



def register_reply_context_provider(provider: ReplyContextProvider) -> None:
    if not provider.name:
        raise ValueError("Reply context provider name cannot be empty")
    _providers[provider.name] = provider


def unregister_reply_context_provider(name: str, provider: ReplyContextProvider | None = None) -> None:
    current = _providers.get(name)
    if current is not None and (provider is None or current is provider):
        _providers.pop(name, None)


async def assemble_reply_context(
    request: ReplyContextRequest | None,
    *,
    target_chat_id: str,
) -> ReplyPromptContext:
    """Acquire, authorize and render a Focus handoff for the exact Replyer target."""

    if request is None or request.mode is ReplyContextMode.NONE:
        return ReplyPromptContext.empty(target_chat_id)
    if request.target_chat_id != target_chat_id:
        raise ReplyContextValidationError(
            f"Reply context target {request.target_chat_id!r} does not match Replyer target {target_chat_id!r}"
        )
    if request.lease is None or request.lease.chat_id != target_chat_id:
        raise ReplyContextValidationError("Reply context has no valid target Focus lease")

    provider = _providers.get(request.provider)
    if provider is None:
        raise ReplyContextUnavailableError(f"Reply context provider {request.provider!r} is not registered")

    material = await (provider.reuse(request) if request.mode is ReplyContextMode.REUSE else provider.acquire(request))
    if material is None:
        return ReplyPromptContext.empty(target_chat_id)

    try:
        if not await provider.validate_context(request, material) or not _material_matches_request(request, material):
            raise ReplyContextValidationError("Focus handoff is stale, unauthorized, or bound to another target")

        from .prompt_renderer import render_focus_handoffs

        rendered = render_focus_handoffs(material.handoffs, max_tokens=request.max_prompt_tokens)
        return ReplyPromptContext(
            target_chat_id=target_chat_id,
            focus_epoch=request.lease.epoch,
            focus_handoff_block=rendered.block,
            context_refs=material.refs,
            injection_detected=rendered.injection_detected,
            estimated_tokens=rendered.estimated_tokens,
            digest=rendered.digest,
        )
    except BaseException:
        # A cancellation between reservation and generation must not hold the
        # handoff until its reservation TTL expires.
        await asyncio.shield(provider.release(material.refs, "assembly_failed"))
        raise


async def release_reply_context(refs: Iterable[ReplyContextRef], reason: str) -> None:
    grouped: dict[str, list[ReplyContextRef]] = defaultdict(list)
    for ref in refs:
        grouped[ref.provider].append(ref)
    for provider_name, provider_refs in grouped.items():
        provider = _providers.get(provider_name)
        if provider is None:
            logger.warning(f"无法释放 ReplyContext：provider {provider_name!r} 未注册")
            continue
        try:
            await provider.release(tuple(provider_refs), reason)
        except Exception as exc:
            logger.warning(f"释放 ReplyContext 失败: provider={provider_name}, reason={reason}, error={exc}")


async def acknowledge_reply_context(refs: Iterable[ReplyContextRef], delivery_id: str) -> bool:
    """Acknowledge only after the caller has a real delivery receipt."""

    if not delivery_id:
        raise ValueError("ReplyContext acknowledgement requires a delivery_id")
    grouped: dict[str, list[ReplyContextRef]] = defaultdict(list)
    for ref in refs:
        grouped[ref.provider].append(ref)
    acknowledged = True
    for provider_name, provider_refs in grouped.items():
        provider = _providers.get(provider_name)
        if provider is None or not await provider.acknowledge(tuple(provider_refs), delivery_id):
            acknowledged = False
    return acknowledged


LeaseValidator = Callable[[FocusLease], bool | Awaitable[bool]]
ScopeAuthorizer = Callable[[FocusHandoff, FocusLease], bool | Awaitable[bool]]


@dataclass(slots=True)
class _Reservation:
    ref: ReplyContextRef
    handoff: FocusHandoff


class StoreBackedReplyContextProvider:
    """HandoffStore adapter with cycle-scoped, expiring reservations.

    ``lease_validator`` must check the coordinator's current group/chat/epoch.
    ``scope_authorizer`` must re-run the current Focus import/export policy;
    this is where explicitly permitted group-to-private transfers are allowed.
    """

    name = "focus"

    def __init__(
        self,
        store: HandoffStore,
        *,
        lease_validator: LeaseValidator,
        scope_authorizer: ScopeAuthorizer,
        reservation_ttl_seconds: float = 120.0,
    ) -> None:
        if reservation_ttl_seconds <= 0:
            raise ValueError("reservation_ttl_seconds must be positive")
        self._store = store
        self._lease_validator = lease_validator
        self._scope_authorizer = scope_authorizer
        self._reservation_ttl_seconds = reservation_ttl_seconds
        self._reservations: dict[tuple[str, str], _Reservation] = {}
        self._completed_cycles: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, request: ReplyContextRequest) -> ReplyContextMaterial | None:
        lease = _required_lease(request)
        if not await _resolve_bool(self._lease_validator(lease)):
            return None
        handoffs = await self._store.get_active(lease.group_id, request.target_chat_id, lease.epoch)
        for handoff in reversed(handoffs):
            if await _resolve_bool(self._scope_authorizer(handoff, lease)):
                material = await self._reserve(request, handoff)
                if material is not None:
                    return material
        return None

    async def reuse(self, request: ReplyContextRequest) -> ReplyContextMaterial | None:
        lease = _required_lease(request)
        if not await _resolve_bool(self._lease_validator(lease)):
            return None
        if any(ref.provider != self.name or ref.cycle_id != request.cycle_id for ref in request.reuse_refs):
            return None

        handoffs: list[FocusHandoff] = []
        refs: list[ReplyContextRef] = []
        now = time.time()
        async with self._lock:
            self._drop_expired(now)
            for requested_ref in request.reuse_refs:
                reservation = self._reservations.get((requested_ref.handoff_id, requested_ref.cycle_id))
                if reservation is None or reservation.ref.reservation_id != requested_ref.reservation_id:
                    return None
                handoffs.append(reservation.handoff)
                refs.append(reservation.ref)
        for handoff in handoffs:
            if not await _resolve_bool(self._scope_authorizer(handoff, lease)):
                return None
        return ReplyContextMaterial(handoffs=tuple(handoffs), refs=tuple(refs))

    async def validate_context(self, request: ReplyContextRequest, material: ReplyContextMaterial) -> bool:
        lease = _required_lease(request)
        if not await _resolve_bool(self._lease_validator(lease)):
            return False
        for handoff in material.handoffs:
            current = await self._store.get(handoff.handoff_id)
            if current is None or current != handoff:
                return False
            if not await _resolve_bool(self._scope_authorizer(handoff, lease)):
                return False
        return True

    async def release(self, refs: tuple[ReplyContextRef, ...], reason: str) -> None:
        del reason  # Kept in the protocol for persistent audit implementations.
        async with self._lock:
            for ref in refs:
                key = (ref.handoff_id, ref.cycle_id)
                current = self._reservations.get(key)
                if current is not None and current.ref.reservation_id == ref.reservation_id:
                    self._reservations.pop(key, None)

    async def acknowledge(self, refs: tuple[ReplyContextRef, ...], delivery_id: str) -> bool:
        results = []
        for ref in refs:
            results.append(await self._store.acknowledge(ref.handoff_id, ref.cycle_id, delivery_id))
        async with self._lock:
            now = time.time()
            self._drop_expired(now)
            for ref, acknowledged in zip(refs, results, strict=True):
                if not acknowledged:
                    continue
                key = (ref.handoff_id, ref.cycle_id)
                reservation = self._reservations.get(key)
                expires_at = (
                    reservation.handoff.expires_at
                    if reservation is not None and reservation.ref.reservation_id == ref.reservation_id
                    else ref.lease_expires_at
                )
                self._completed_cycles[key] = max(self._completed_cycles.get(key, 0.0), expires_at)
        await self.release(refs, "delivered")
        return bool(results) and all(results)

    async def _reserve(self, request: ReplyContextRequest, handoff: FocusHandoff) -> ReplyContextMaterial | None:
        now = time.time()
        key = (handoff.handoff_id, request.cycle_id)
        async with self._lock:
            self._drop_expired(now)
            if key in self._completed_cycles:
                return None
            existing = self._reservations.get(key)
            if existing is not None:
                return ReplyContextMaterial(handoffs=(existing.handoff,), refs=(existing.ref,))
            ref = ReplyContextRef(
                provider=self.name,
                handoff_id=handoff.handoff_id,
                reservation_id=uuid.uuid4().hex,
                target_chat_id=handoff.target_chat_id,
                focus_group_id=handoff.group_id,
                focus_epoch=handoff.target_epoch,
                revision=handoff.revision,
                cycle_id=request.cycle_id,
                lease_expires_at=min(handoff.expires_at, now + self._reservation_ttl_seconds),
            )
            reservation = _Reservation(ref=ref, handoff=handoff)
            self._reservations[key] = reservation
            return ReplyContextMaterial(handoffs=(handoff,), refs=(ref,))

    def _drop_expired(self, now: float) -> None:
        for key, reservation in tuple(self._reservations.items()):
            if reservation.ref.lease_expires_at <= now:
                self._reservations.pop(key, None)
        for key, expires_at in tuple(self._completed_cycles.items()):
            if expires_at <= now:
                self._completed_cycles.pop(key, None)


def _required_lease(request: ReplyContextRequest) -> FocusLease:
    if request.lease is None:
        raise ReplyContextValidationError("Focus lease is required")
    return request.lease


def _material_matches_request(request: ReplyContextRequest, material: ReplyContextMaterial) -> bool:
    lease = _required_lease(request)
    if not material.handoffs or not material.refs or len(material.handoffs) != len(material.refs):
        return False
    for handoff, ref in zip(material.handoffs, material.refs, strict=True):
        if (
            handoff.handoff_id != ref.handoff_id
            or handoff.group_id != lease.group_id
            or handoff.target_chat_id != request.target_chat_id
            or handoff.target_epoch != lease.epoch
            or handoff.revision != ref.revision
            or ref.provider != request.provider
            or ref.target_chat_id != request.target_chat_id
            or ref.focus_group_id != lease.group_id
            or ref.focus_epoch != lease.epoch
            or ref.cycle_id != request.cycle_id
            or ref.lease_expires_at <= time.time()
        ):
            return False
    return True


async def _resolve_bool(value: bool | Awaitable[bool]) -> bool:
    if inspect.isawaitable(value):
        return bool(await value)
    return bool(value)
