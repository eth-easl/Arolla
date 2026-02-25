"""Configuration package."""

try:
    from .schema import *  # noqa: F401,F403
    from .loader import ConfigLoader
except ModuleNotFoundError:  # pragma: no cover - optional dependency fallback
    ConfigLoader = None

__all__ = ["ConfigLoader"]
