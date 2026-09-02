"""Compatibility entrypoint for the WebUI setup wizard.

Implementation is intentionally grouped into two coarse modules:
environment/path checks and deployment/configuration.
"""

try:
    from .setup_checks import (
        DEFAULT_PORTS,
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
    from .setup_bilibili_login import (
        BilibiliLoginCleanupError,
        BilibiliLoginManager,
        BilibiliLoginNotReady,
        BilibiliLoginProcessError,
        bilibili_login_manager,
    )
except ImportError:
    from setup_checks import (
        DEFAULT_PORTS,
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
    from setup_bilibili_login import (
        BilibiliLoginCleanupError,
        BilibiliLoginManager,
        BilibiliLoginNotReady,
        BilibiliLoginProcessError,
        bilibili_login_manager,
    )

# Backward-compatible public alias. These are defaults only; runtime checks
# resolve configured ports through EnvironmentChecker._configured_ports().
KNOWN_PORTS = DEFAULT_PORTS

__all__ = [
    "BACKUP_DIR",
    "DEFAULT_PORTS",
    "KNOWN_PORTS",
    "MAX_BACKUPS_PER_FILE",
    "ROOT_DIR",
    "TEMPLATE_MAP",
    "BackupManager",
    "BilibiliLoginCleanupError",
    "BilibiliLoginManager",
    "BilibiliLoginNotReady",
    "BilibiliLoginProcessError",
    "ConfigInitializer",
    "DependencyInstaller",
    "EnvironmentChecker",
    "NapCatConfigurator",
    "PathVerifier",
    "bilibili_login_manager",
]
