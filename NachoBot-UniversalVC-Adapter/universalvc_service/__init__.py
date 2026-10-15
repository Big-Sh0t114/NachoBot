"""UniversalVC service package.

The package initializer intentionally imports no native audio or ML modules.
Use :mod:`universalvc_service.client` from lightweight callers and import the
engine only inside the service process.
"""

__version__ = "1.0"

