"""Compatibility entrypoint for the WebUI setup wizard.

Implementation is intentionally grouped into two coarse modules:
environment/path checks and deployment/configuration.
"""

try:
    from .setup_checks import (
        KNOWN_PORTS,
        ROOT_DIR,
        TEMPLATE_MAP,
        EnvironmentChecker,
        PathVerifier,
    )
    from .setup_deployment import (
        BACKUP_DIR,
        MAX_BACKUPS_PER_FILE,
        BackupManager,
        ConfigInitializer,
        DependencyInstaller,
        NapCatConfigurator,
    )
except ImportError:
    from setup_checks import (
        KNOWN_PORTS,
        ROOT_DIR,
        TEMPLATE_MAP,
        EnvironmentChecker,
        PathVerifier,
    )
    from setup_deployment import (
        BACKUP_DIR,
        MAX_BACKUPS_PER_FILE,
        BackupManager,
        ConfigInitializer,
        DependencyInstaller,
        NapCatConfigurator,
    )

__all__ = [
    "BACKUP_DIR",
    "KNOWN_PORTS",
    "MAX_BACKUPS_PER_FILE",
    "ROOT_DIR",
    "TEMPLATE_MAP",
    "BackupManager",
    "ConfigInitializer",
    "DependencyInstaller",
    "EnvironmentChecker",
    "NapCatConfigurator",
    "PathVerifier",
]
