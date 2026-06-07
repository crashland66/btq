from __future__ import annotations


class VaultRepositoryError(Exception):
    """Raised when a vault repository operation cannot be completed safely."""


class NotFoundError(VaultRepositoryError):
    """Raised when a domain entity cannot be resolved."""


class AmbiguousResolutionError(VaultRepositoryError):
    """Raised when an alias or query resolves to multiple entities."""


class ValidationError(VaultRepositoryError):
    """Raised when indexed vault metadata is internally inconsistent."""

