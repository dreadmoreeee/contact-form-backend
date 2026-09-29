"""contact-form-backend: a self-hosted contact form endpoint with CAPTCHA-free spam protection."""

from .config import AppConfig, ConfigError, FieldSpec, FormConfig, load_config, loads_config
from .validation import normalize_email, validate

__version__ = "0.1.0"

__all__ = [
    "AppConfig",
    "ConfigError",
    "FieldSpec",
    "FormConfig",
    "create_app",
    "load_config",
    "loads_config",
    "normalize_email",
    "validate",
]


def create_app(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Build the FastAPI app (imported lazily so the config tools work without FastAPI)."""
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
