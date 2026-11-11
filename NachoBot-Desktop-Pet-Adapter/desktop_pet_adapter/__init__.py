"""Backend-neutral runtime for an interactive desktop pet.

The package initializer deliberately imports no renderer, windowing, or model
binding modules. Concrete rendering backends are selected when a runtime starts.
"""

__version__ = "0.1.0"


def __getattr__(name: str):
    if name in {"AvatarRuntime", "AvatarWebSocketServer"}:
        from importlib import import_module

        module_name = "runtime" if name == "AvatarRuntime" else "server"
        return getattr(import_module(f".{module_name}", __name__), name)
    raise AttributeError(name)


__all__ = ["AvatarRuntime", "AvatarWebSocketServer", "__version__"]
