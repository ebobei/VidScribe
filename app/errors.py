"""Project-level exceptions for VidScribe."""


class VidScribeError(Exception):
    """Base exception for expected VidScribe errors."""


class ConfigError(VidScribeError):
    """Raised when a YAML config or CLI override is invalid."""


class PathPreparationError(VidScribeError):
    """Raised when output directories or files cannot be prepared."""
