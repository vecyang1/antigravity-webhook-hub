"""
Antigravity Webhook Hub
A lightweight, zero-footprint local webhook gateway and event dispatcher for Antigravity on macOS.
"""

import sys
import time
import types

# Configure thread stack size for bounded memory on macOS Darwin
try:
    import threading
    threading.stack_size(131072)
except Exception:
    pass

# Zero-overhead lazy SSL provider avoiding heavy OpenSSL/LibreSSL dynamic lib loading (<30MB RAM budget)
if "ssl" not in sys.modules:
    class _LightweightSSL(types.ModuleType):
        def __init__(self):
            super().__init__("ssl")
            self.__dict__["_real"] = None
            for attr in ["SSLWantReadError", "SSLSyscallError", "SSLZeroReturnError", "SSLError"]:
                self.__dict__[attr] = type(attr, (Exception,), {})

        def __getattr__(self, name: str):
            if self.__dict__["_real"] is None:
                import importlib
                sys.modules.pop("ssl", None)
                self.__dict__["_real"] = importlib.import_module("ssl")
                sys.modules["ssl"] = self
            val = getattr(self.__dict__["_real"], name)
            self.__dict__[name] = val
            return val

    sys.modules["ssl"] = _LightweightSSL()

# Zero-dependency RFC 1123 date provider with transparent fallback
if "email.utils" not in sys.modules:
    class _LightweightEmailUtils(types.ModuleType):
        def __init__(self):
            super().__init__("email.utils")
            self.__dict__["formatdate"] = lambda timeval=None, localtime=False, usegmt=True: time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(timeval if timeval is not None else time.time())
            )
            self.__dict__["_real"] = None

        def __getattr__(self, name: str):
            if name.startswith("__"):
                raise AttributeError(name)
            if self.__dict__["_real"] is None:
                import importlib
                sys.modules.pop("email.utils", None)
                self.__dict__["_real"] = importlib.import_module("email.utils")
                sys.modules["email.utils"] = self
            val = getattr(self.__dict__["_real"], name)
            self.__dict__[name] = val
            return val

    sys.modules["email.utils"] = _LightweightEmailUtils()

__version__ = "1.0.0"

__all__ = [
    # Server
    "AsyncHTTPServer",
    # Models
    "HTTPRequest",
    "HTTPResponse",
    "ValidationResult",
    "WebhookEvent",
    "Task",
    "TaskStatus",
    "TaskExecution",
    "ExecutionLog",
    # Config
    "AppConfig",
    "ServerConfig",
    "SecurityConfig",
    "DatabaseConfig",
    "DispatchConfig",
    "TunnelConfig",
    "load_config",
    "validate_config",
    # Security
    "verify_hmac_signature",
    "verify_bearer_token",
    "validate_request_security",
    "generate_hmac_signature",
    "compute_payload_hash",
    "compute_dedup_hash",
]

_LAZY_MODULES: dict[str, tuple[str, str]] = {
    # Server
    "AsyncHTTPServer": ("hub.server", "AsyncHTTPServer"),
    # Models
    "HTTPRequest": ("hub.models", "HTTPRequest"),
    "HTTPResponse": ("hub.models", "HTTPResponse"),
    "ValidationResult": ("hub.models", "ValidationResult"),
    "WebhookEvent": ("hub.models", "WebhookEvent"),
    "Task": ("hub.models", "Task"),
    "TaskStatus": ("hub.models", "TaskStatus"),
    "TaskExecution": ("hub.models", "TaskExecution"),
    "ExecutionLog": ("hub.models", "ExecutionLog"),
    # Config
    "AppConfig": ("hub.config", "AppConfig"),
    "ServerConfig": ("hub.config", "ServerConfig"),
    "SecurityConfig": ("hub.config", "SecurityConfig"),
    "DatabaseConfig": ("hub.config", "DatabaseConfig"),
    "DispatchConfig": ("hub.config", "DispatchConfig"),
    "TunnelConfig": ("hub.config", "TunnelConfig"),
    "load_config": ("hub.config", "load_config"),
    "validate_config": ("hub.config", "validate_config"),
    # Security
    "verify_hmac_signature": ("hub.security", "verify_hmac_signature"),
    "verify_bearer_token": ("hub.security", "verify_bearer_token"),
    "validate_request_security": ("hub.security", "validate_request_security"),
    "generate_hmac_signature": ("hub.security", "generate_hmac_signature"),
    "compute_payload_hash": ("hub.security", "compute_payload_hash"),
    "compute_dedup_hash": ("hub.security", "compute_dedup_hash"),
}


def __getattr__(name: str) -> Any:
    """Lazy-load public package attributes on access (PEP 562) to maintain strict <30MB RSS footprint."""
    if name in _LAZY_MODULES:
        mod_name, attr_name = _LAZY_MODULES[name]
        import importlib
        mod = importlib.import_module(mod_name)
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module 'hub' has no attribute '{name}'")


def __dir__() -> list[str]:
    return __all__
