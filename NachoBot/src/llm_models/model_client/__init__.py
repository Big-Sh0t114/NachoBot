"""Provider client registration and lazy runtime registration helpers."""

from importlib import import_module
from typing import Iterable

from src.config.config import model_config
from .base_client import APIProvider, client_registry


_CLIENT_MODULES = {
    "openai": ".openai_client",
    "gemini": ".gemini_client",
}


def ensure_provider_client_types(providers: Iterable[APIProvider]) -> None:
    """Register configured built-in clients, including types added after startup."""
    for client_type in {provider.client_type for provider in providers}:
        if client_type in client_registry.client_registry:
            continue
        module_name = _CLIENT_MODULES.get(client_type)
        if module_name is not None:
            import_module(module_name, __name__)
        if client_type not in client_registry.client_registry:
            raise KeyError(f"'{client_type}' 类型的 Client 未注册")


for _client_type in {provider.client_type for provider in model_config.api_providers}:
    if module_name := _CLIENT_MODULES.get(_client_type):
        import_module(module_name, __name__)
