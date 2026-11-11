"""Pure policy primitives for request-scoped plugin tool visibility.

This module deliberately has no application imports.  It keeps the policy
snapshot that authorizes a request separate from executor state and from the
result cache's serialized payloads.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator, Mapping
from weakref import ref


class ToolPolicyUnavailable(RuntimeError):
    """The host could not provide an authoritative live tool policy."""


def filter_tool_classes(
    tool_classes: Mapping[str, type],
    disabled_names: set[str] | frozenset[str] | tuple[str, ...] | list[str],
    include_prefix: str | None = None,
    exclude_prefix: str | None = None,
) -> dict[str, type]:
    """Return a defensive, exact-name view of classes allowed for this chat."""
    if not isinstance(tool_classes, Mapping):
        raise TypeError("tool class registry must be a mapping")
    if not isinstance(disabled_names, (set, frozenset, tuple, list)):
        raise TypeError("disabled tool names must be a collection")
    if any(type(name) is not str for name in disabled_names):
        raise TypeError("disabled tool names must be strings")
    disabled = set(disabled_names)
    allowed: dict[str, type] = {}
    for name, tool_class in tool_classes.items():
        if type(name) is not str or not name or not isinstance(tool_class, type):
            continue
        if name in disabled:
            continue
        if include_prefix and not name.startswith(include_prefix):
            continue
        if exclude_prefix and name.startswith(exclude_prefix):
            continue
        allowed[name] = tool_class
    return allowed


def is_registered_instance(
    instance: Any,
    tool_name: str,
    allowed_classes: Mapping[str, type],
    expected_class: type | None = None,
) -> bool:
    """Require the instance's exact type to be the live registered class."""
    registered_class = allowed_classes.get(tool_name)
    if registered_class is None or instance is None:
        return False
    if expected_class is not None and registered_class is not expected_class:
        return False
    return type(instance) is registered_class


def is_valid_cached_tool_bundle(
    bundle: Any,
    allowed_classes: Mapping[str, type],
) -> bool:
    """Accept only complete-looking, non-error bundles for currently allowed names."""
    if type(bundle) is not list or not bundle:
        return False
    for item in bundle:
        if type(item) is not dict:
            return False
        name = item.get("tool_name")
        if type(name) is not str or name not in allowed_classes:
            return False
        if item.get("type") == "tool_error":
            return False
    return True


def is_tool_cacheable(tool_class: Any) -> bool:
    """Require an exact author opt-in on the concrete tool class."""
    try:
        return isinstance(tool_class, type) and tool_class.__dict__.get("cacheable") is True
    except Exception:
        return False


def all_catalog_cacheable(tool_classes: Mapping[str, type]) -> bool:
    """A nonempty advertised catalog is cacheable only when every class opts in."""
    try:
        if not isinstance(tool_classes, Mapping):
            return False
        classes = tuple(tool_classes.values())
    except Exception:
        return False
    return bool(classes) and all(is_tool_cacheable(tool_class) for tool_class in classes)


class ImmutableToolCacheScope:
    """Persistent cache metadata that weakly records exact catalog identity."""

    __slots__ = ("_name", "_classes", "_tokens", "_cache_eligible")

    def __init__(
        self,
        name: str,
        tool_classes: Mapping[str, type],
        tool_tokens: Mapping[str, str] | None = None,
        *,
        cache_eligible: bool | None = None,
    ) -> None:
        if type(name) is not str:
            raise TypeError("cache scope name must be a string")
        if cache_eligible is not None and type(cache_eligible) is not bool:
            raise TypeError("cache eligibility must be a boolean or None")
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_cache_eligible", cache_eligible)
        classes = (
            (tool_name, ref(tool_class))
            for tool_name, tool_class in tool_classes.items()
            if type(tool_name) is str and isinstance(tool_class, type)
        )
        object.__setattr__(self, "_classes", tuple(sorted(classes, key=lambda item: item[0])))
        if tool_tokens is None:
            tokens = None
        else:
            tokens = tuple(
                sorted(
                    (
                        tool_name,
                        token,
                    )
                    for tool_name, token in tool_tokens.items()
                    if type(tool_name) is str and type(token) is str and tool_name in tool_classes
                )
            )
        object.__setattr__(self, "_tokens", tokens)

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(self, name):
            raise AttributeError("cache scope metadata is immutable")
        object.__setattr__(self, name, value)

    @property
    def name(self) -> str:
        return self._name

    @property
    def cache_eligible(self) -> bool | None:
        return self._cache_eligible

    def matches(
        self,
        name: str,
        tool_classes: Mapping[str, type],
        tool_tokens: Mapping[str, str] | None = None,
        *,
        cache_eligible: bool | None = None,
    ) -> bool:
        if (
            self.name != name
            or self.cache_eligible is not cache_eligible
            or len(self._classes) != len(tool_classes)
        ):
            return False
        for tool_name, class_ref in self._classes:
            referenced_class = class_ref()
            if referenced_class is None or tool_name not in tool_classes:
                return False
            if referenced_class is not tool_classes[tool_name]:
                return False
        if self._tokens is None:
            return tool_tokens is None
        if tool_tokens is None or len(self._tokens) != len(tool_tokens):
            return False
        return all(tool_tokens.get(tool_name) == token for tool_name, token in self._tokens)

    def same_as(self, other: Any) -> bool:
        if (
            not isinstance(other, ImmutableToolCacheScope)
            or self.name != other.name
            or self.cache_eligible is not other.cache_eligible
        ):
            return False
        if len(self._classes) != len(other._classes):
            return False
        for (left_name, left_ref), (right_name, right_ref) in zip(self._classes, other._classes, strict=True):
            left_class = left_ref()
            right_class = right_ref()
            if left_class is None or right_class is None:
                return False
            if left_name != right_name or left_class is not right_class:
                return False
        return self._tokens == other._tokens


class ToolExecutionScope:
    """Active request authority; class references are cleared on retirement."""

    __slots__ = ("owner_ref", "active", "classes", "tokens")

    def __init__(
        self,
        owner: Any,
        tool_classes: Mapping[str, type],
        tool_tokens: Mapping[str, str] | None = None,
    ) -> None:
        self.owner_ref = ref(owner)
        self.active = True
        self.classes = dict(tool_classes)
        self.tokens = None if tool_tokens is None else dict(tool_tokens)

    def narrow(
        self,
        live_classes: Mapping[str, type],
        live_tokens: Mapping[str, str] | None = None,
    ) -> None:
        if not self.active:
            self.classes.clear()
            if self.tokens is not None:
                self.tokens.clear()
            return
        retained = {}
        for name, expected in self.classes.items():
            if live_classes.get(name) is not expected:
                continue
            if self.tokens is not None:
                if live_tokens is None or live_tokens.get(name) != self.tokens.get(name):
                    continue
            retained[name] = expected
        self.classes = retained
        if self.tokens is not None:
            self.tokens = {name: self.tokens[name] for name in retained if name in self.tokens}

    def narrow_tool(self, name: str, live_class: type | None, live_token: str | None = None) -> None:
        """Monotonically withdraw one tool after an immediate live check."""
        if not self.active or self.classes.get(name) is not live_class:
            self.classes.pop(name, None)
            if self.tokens is not None:
                self.tokens.pop(name, None)
            return
        if self.tokens is not None and self.tokens.get(name) != live_token:
            self.classes.pop(name, None)
            self.tokens.pop(name, None)

    def allows(self, name: str, tool_class: type, token: str | None = None) -> bool:
        if not self.active or self.classes.get(name) is not tool_class:
            return False
        return self.tokens is None or self.tokens.get(name) == token

    def retire(self) -> None:
        self.active = False
        self.classes.clear()
        if self.tokens is not None:
            self.tokens.clear()


_scope_stack: ContextVar[tuple[ToolExecutionScope, ...]] = ContextVar(
    "plugin_tool_execution_scope_stack", default=()
)


def current_tool_execution_scope(owner: Any) -> ToolExecutionScope | None:
    """Find the nearest scope for owner, even when that scope has retired."""
    for scope in reversed(_scope_stack.get()):
        if scope.owner_ref() is owner:
            return scope
    return None


@contextmanager
def bind_tool_execution_scope(
    owner: Any,
    tool_classes: Mapping[str, type],
    tool_tokens: Mapping[str, str] | None = None,
) -> Iterator[ToolExecutionScope]:
    """Always create a fresh owner scope and retire it at the lexical boundary."""
    scope = ToolExecutionScope(owner, tool_classes, tool_tokens)
    prior = _scope_stack.get()
    token: Token[tuple[ToolExecutionScope, ...]] = _scope_stack.set(prior + (scope,))
    try:
        yield scope
    finally:
        scope.retire()
        _scope_stack.reset(token)


@contextmanager
def borrow_or_bind_tool_execution_scope(
    owner: Any,
    tool_classes: Mapping[str, type],
    tool_tokens: Mapping[str, str] | None = None,
) -> Iterator[tuple[ToolExecutionScope, bool]]:
    """Borrow the nearest owner scope, or create a standalone scope.

    The returned boolean reports whether this caller owns and must retire the
    new scope. Borrowers never retire a scope created by an enclosing request.
    A nearest retired scope remains a denial boundary for inherited tasks.
    """
    existing = current_tool_execution_scope(owner)
    if existing is not None:
        if existing.active:
            existing.narrow(tool_classes, tool_tokens)
        yield existing, False
        return

    scope = ToolExecutionScope(owner, tool_classes, tool_tokens)
    prior = _scope_stack.get()
    token: Token[tuple[ToolExecutionScope, ...]] = _scope_stack.set(prior + (scope,))
    try:
        yield scope, True
    finally:
        scope.retire()
        _scope_stack.reset(token)
